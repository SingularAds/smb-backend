"""Booking Automation — Steps 1 & 2.

Step 1 – Confirmation & Reminders:
  • send_booking_confirmation()        → called immediately after a booking is created
  • run_reminder_sweep()               → scheduled every 30 min; checks for bookings
                                         24 h and 2 h ahead and sends reminders

Step 2 – Cancellation Notice & Visit Confirmation:
  • send_cancellation_notice()         → called immediately when a booking is cancelled
  • run_visit_confirmation_sweep()     → scheduled every 30 min; 2 hours after a
                                         booking's scheduled time, asks the customer
                                         "Did you visit? Reply YES or NO"
                                         The reply is handled in the WhatsApp webhook
                                         by visit_service.handle_visit_reply().

Default assumption: visited = True.
  A booking is only marked visited=False when:
    a) The customer replies NO to the confirmation message.
    b) The booking is cancelled (by customer or owner).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import pytz

from app import firestore as db
from app.services.automation.whatsapp_notifier import send_to_customer, send_to_owner
from app.services.tz_utils import biz_tz as _biz_tz, parse_dt as _parse_dt_tz, fmt_datetime as _fmt_datetime_tz

logger = logging.getLogger(__name__)

# How soon after reminder threshold we consider it "already sent"
_REMINDER_WINDOW_MINUTES = 25  # run every 30 min, match within 25-min window


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _fmt_dt(dt: datetime) -> str:
    if not hasattr(dt, "strftime"):
        return str(dt)
    return f"{dt.day} {dt.strftime('%b %Y')} {dt.strftime('%H:%M')}"


# ── Referral booking confirmation addendum (i18n) ─────────────────────────────
# Appended to the standard booking confirmation when the customer is a referred
# friend with an active first-visit discount.  {pct} = discount %, {code} = 4-digit verify code.
_REFERRAL_BOOKING_ADDENDUM: dict[str, str] = {
    "en": (
        "\n\n🎟️ *Your referral discount:* {pct}% off your first visit!\n"
        "Your verify code: *{code}*\n"
        "Please show this code at the salon when you arrive."
    ),
    "pt": (
        "\n\n🎟️ *Seu desconto de indicação:* {pct}% na sua primeira visita!\n"
        "Seu código de verificação: *{code}*\n"
        "Apresente este código no salão quando chegar."
    ),
    "es": (
        "\n\n🎟️ *Tu descuento por referido:* {pct}% en tu primera visita!\n"
        "Tu código de verificación: *{code}*\n"
        "Muestra este código en el salón cuando llegues."
    ),
}


# ── Step 1a: Booking confirmation ─────────────────────────────────────────────

async def send_booking_confirmation(booking: dict, business: dict) -> None:
    """Send an immediate confirmation WhatsApp to the customer after booking."""
    customer_phone = booking.get("customerPhone", "")
    if not customer_phone:
        return

    customer_name = booking.get("customerName") or "there"
    service = booking.get("serviceName") or booking.get("service") or "your appointment"
    biz_name = business.get("name") or "us"
    booking_dt = _parse_dt_tz(booking.get("datetime") or booking.get("date"))
    time_str = _fmt_datetime_tz(booking_dt, business) if booking_dt else "the scheduled time"

    msg = (
        f"✅ *Booking confirmed!*\n\n"
        f"Hi {customer_name}! Your booking is confirmed with *{biz_name}*.\n\n"
        f"📋 *Service:* {service}\n"
        f"📅 *When:* {time_str}\n"
        f"🆔 *Reference:* {booking.get('id', '')}\n\n"
        f"See you soon! 😊"
    )

    # If this customer is a referred friend with an active first-visit discount,
    # append their discount percentage and verify code.
    biz_id = business.get("id", "")
    if biz_id:
        try:
            customer = db.get_customer_by_phone(biz_id, customer_phone)
            if (
                customer
                and customer.get("pendingDiscountReason") == "referral_first_visit"
                and not customer.get("pendingDiscountUsed")
            ):
                referral = db.get_referral_by_referee(biz_id, customer_phone)
                if referral and referral.get("verifyCode"):
                    pct = int(customer.get("pendingDiscount") or 10)
                    verify_code = referral["verifyCode"]
                    lang = (business.get("primaryLanguage") or "en")[:2].lower()
                    template = _REFERRAL_BOOKING_ADDENDUM.get(lang) or _REFERRAL_BOOKING_ADDENDUM["en"]
                    msg += template.format(pct=pct, code=verify_code)
        except Exception as exc:
            logger.warning(
                "[AUTOMATION:CONFIRMATION] Failed to fetch referral info for %s: %s",
                customer_phone, exc,
            )

    logger.info("[AUTOMATION:CONFIRMATION] booking=%s customer=%s", booking.get('id'), customer_phone)
    await send_to_customer(business, customer_phone, msg)


# ── Step 1b: Reminder sweep (scheduled every 30 min) ─────────────────────────

async def run_reminder_sweep() -> None:
    """Check all active businesses for bookings needing 24h or 2h reminders."""
    now = _now()
    logger.info("[AUTOMATION:REMINDER_SWEEP] starting at %s", now.isoformat())
    businesses = db.list_active_businesses()
    total_sent = 0

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id:
            continue
        # Only businesses with WhatsApp linked
        if not business.get("waSessionId"):
            continue

        try:
            bookings = db.list_bookings(biz_id, limit=200)
            for b in bookings:
                if b.get("status") not in ("confirmed", "pending"):
                    continue

                booking_dt = _parse_dt(b.get("datetime") or b.get("date"))
                if not booking_dt:
                    continue

                customer_phone = b.get("customerPhone", "")
                if not customer_phone:
                    continue

                delta = (booking_dt - now).total_seconds() / 60  # minutes until booking

                sent_reminders: list = b.get("remindersSent", [])

                # 24-hour reminder: between 23h35 and 24h00 ahead
                if 23 * 60 + 35 <= delta <= 24 * 60 and "24h" not in sent_reminders:
                    await _send_reminder(business, b, "24h", booking_dt)
                    db.update_booking(b["id"], {"remindersSent": sent_reminders + ["24h"]}, biz_id)
                    total_sent += 1

                # 2-hour reminder: between 1h35 and 2h00 ahead
                elif 60 + 35 <= delta <= 2 * 60 and "2h" not in sent_reminders:
                    await _send_reminder(business, b, "2h", booking_dt)
                    db.update_booking(b["id"], {"remindersSent": sent_reminders + ["2h"]}, biz_id)
                    total_sent += 1

        except Exception as exc:
            logger.exception("[Automation] Reminder sweep failed for business %s: %s", biz_id, exc)
            logger.error("[AUTOMATION:REMINDER_SWEEP] error for biz %s: %s", biz_id, exc)

    logger.info("[AUTOMATION:REMINDER_SWEEP] done — %d reminders sent across %d businesses", total_sent, len(businesses))


async def _send_reminder(business: dict, booking: dict, label: str, booking_dt: datetime) -> None:
    customer_name = booking.get("customerName") or "there"
    service = booking.get("serviceName") or booking.get("service") or "your appointment"
    biz_name = business.get("name") or "us"
    customer_phone = booking.get("customerPhone", "")
    
    # Convert booking_dt to business timezone for time display
    tz = _biz_tz(business)
    booking_dt_local = booking_dt.astimezone(tz)
    time_display = booking_dt_local.strftime("%H:%M")
    date_display = f"{booking_dt_local.day} {booking_dt_local.strftime('%b %Y')}"

    if label == "24h":
        msg = (
            f"⏰ *Reminder — tomorrow!*\n\n"
            f"Hi {customer_name}! Just a reminder that your appointment at *{biz_name}* is "
            f"*tomorrow* at *{time_display}*.\n\n"
            f"📋 Service: {service}\n"
            f"📅 Date: {date_display}\n\n"
            f"See you then! 😊"
        )
    else:
        msg = (
            f"⏰ *Reminder — in 2 hours!*\n\n"
            f"Hi {customer_name}! Your appointment at *{biz_name}* is in just *2 hours* "
            f"at *{time_display}*.\n\n"
            f"📋 Service: {service}\n\n"
            f"Looking forward to seeing you! 😊"
        )

    logger.info("[AUTOMATION:REMINDER] %s booking=%s customer=%s", label, booking.get('id'), customer_phone)
    await send_to_customer(business, customer_phone, msg)


# ── Step 2a: Cancellation notice ──────────────────────────────────────────────

async def send_cancellation_notice(booking: dict, business: dict) -> None:
    """Notify the customer that their booking has been cancelled."""
    customer_phone = booking.get("customerPhone", "")
    if not customer_phone:
        return

    customer_name = booking.get("customerName") or "there"
    service = booking.get("serviceName") or booking.get("service") or "your appointment"
    biz_name = business.get("name") or "us"

    msg = (
        f"❌ *Booking cancelled*\n\n"
        f"Hi {customer_name}, your booking for *{service}* at *{biz_name}* has been cancelled.\n\n"
        f"Would you like to reschedule? Just reply here and we'll sort it for you! 📅"
    )
    logger.info("[AUTOMATION:CANCELLATION] booking=%s customer=%s", booking.get('id'), customer_phone)
    await send_to_customer(business, customer_phone, msg)


# ── Step 2a-II: Reschedule notice ────────────────────────────────────────────

async def send_reschedule_notice(booking: dict, business: dict, old_datetime: str, new_datetime: str) -> None:
    """Notify the customer that their booking has been rescheduled."""
    customer_phone = booking.get("customerPhone", "")
    if not customer_phone:
        return

    customer_name = booking.get("customerName") or "there"
    service = booking.get("serviceName") or booking.get("service") or "your appointment"
    biz_name = business.get("name") or "us"

    msg = (
        f"🔄 *Booking rescheduled!*\n\n"
        f"Hi {customer_name}, your {service} at *{biz_name}* has been moved.\n\n"
        f"❌ Old time: {old_datetime}\n"
        f"✅ New time: {new_datetime}\n\n"
        f"See you soon! 😊"
    )
    logger.info("[AUTOMATION:RESCHEDULE] booking=%s customer=%s", booking.get('id'), customer_phone)
    await send_to_customer(business, customer_phone, msg)


# ── Step 2b: Visit confirmation sweep (scheduled every 30 min) ───────────────

_VISIT_CONFIRMATION_DELAY_MINUTES = 120  # 2 hours after booking time


async def run_visit_confirmation_sweep() -> None:
    """2 hours after each booking's scheduled time, ask the customer if they visited.

    Sends: "Did you visit us? Reply YES or NO"
    Only applies to bookings with status=confirmed/pending that haven't had a
    confirmation message sent yet.

    The YES / NO reply is handled by visit_service.handle_visit_reply() inside
    the WhatsApp webhook pipeline (before AI routing).

    Also sends the owner a referral verification request if the customer is a
    referred friend with a still-pending referral.
    """
    now = _now()
    logger.info("[VISIT_SWEEP] starting at %s", now.isoformat())
    businesses = db.list_active_businesses()
    total_sent = 0

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id or not business.get("waSessionId"):
            continue

        try:
            bookings = db.list_bookings(biz_id, limit=200)
            for b in bookings:
                if b.get("status") not in ("confirmed", "pending"):
                    continue

                booking_dt = _parse_dt(b.get("datetime") or b.get("date"))
                if not booking_dt:
                    continue

                minutes_ago = (now - booking_dt).total_seconds() / 60
                if minutes_ago < _VISIT_CONFIRMATION_DELAY_MINUTES:
                    continue  # Too soon

                customer_phone = b.get("customerPhone", "")
                if not customer_phone:
                    continue

                # ── Customer visit confirmation (existing behaviour) ───────────
                if not b.get("visitConfirmationSent"):
                    try:
                        await _send_visit_confirmation(business, b)
                        db.update_booking(
                            b["id"],
                            {
                                "visitConfirmationSent": True,
                                "visitConfirmationSentAt": now.isoformat(),
                            },
                            biz_id,
                        )
                        total_sent += 1
                        logger.info(
                            "[VISIT_SWEEP] Confirmation sent booking=%s customer=%s biz=%s",
                            b["id"], customer_phone, biz_id,
                        )
                    except Exception as exc:
                        logger.exception(
                            "[VISIT_SWEEP] Failed to send confirmation for booking %s: %s",
                            b.get("id"), exc,
                        )

                # ── Owner referral visit confirmation (new) ───────────────────
                if not b.get("referralOwnerConfirmationSent"):
                    try:
                        await _maybe_send_owner_referral_confirmation(business, b)
                    except Exception as exc:
                        logger.exception(
                            "[VISIT_SWEEP] Owner referral confirmation error for booking %s: %s",
                            b.get("id"), exc,
                        )

        except Exception as exc:
            logger.exception("[VISIT_SWEEP] error for biz %s: %s", biz_id, exc)

    logger.info("[VISIT_SWEEP] done — %d confirmations sent", total_sent)


async def _send_visit_confirmation(business: dict, booking: dict) -> None:
    """Send the "Did you visit?" message to the customer."""
    customer_name = booking.get("customerName") or "there"
    service = booking.get("serviceName") or booking.get("service") or "your appointment"
    biz_name = business.get("name") or "us"
    customer_phone = booking.get("customerPhone", "")

    msg = (
        f"👋 *Hi {customer_name}!*\n\n"
        f"We hope you enjoyed your *{service}* visit at *{biz_name}* today!\n\n"
        f"*Did you visit us?* Please reply:\n"
        f"✅ *YES* — if you came in\n"
        f"❌ *NO* — if you couldn't make it"
    )
    logger.info(
        "[VISIT_SWEEP] sending confirmation for booking=%s customer=%s",
        booking.get("id"), customer_phone,
    )
    await send_to_customer(business, customer_phone, msg)


async def _maybe_send_owner_referral_confirmation(business: dict, booking: dict) -> None:
    """If the booking's customer is a referred friend with a pending referral, ask the
    owner to confirm whether they showed up and used their discount.

    Saves a referralOwnerConfirmations record (expires 24 h) and marks the booking
    with referralOwnerConfirmationSent so this only fires once per booking.
    """
    biz_id = business.get("id", "")
    booking_id = booking.get("id", "")
    customer_phone = booking.get("customerPhone", "")
    if not biz_id or not booking_id or not customer_phone:
        return

    customer = db.get_customer_by_phone(biz_id, customer_phone)
    if not customer or not customer.get("referredBy"):
        return  # Not a referred friend

    referral = db.get_referral_by_referee(biz_id, customer_phone)
    if not referral or referral.get("status") != "pending":
        return  # Referral already resolved or not found

    verify_code = referral.get("verifyCode")
    if not verify_code:
        return

    owner_phone = business.get("ownerPhone") or business.get("owner_phone") or ""
    if not owner_phone:
        return

    now_iso = datetime.utcnow().isoformat()
    expires_iso = (datetime.utcnow() + timedelta(hours=24)).isoformat()

    db.create_referral_owner_confirmation(biz_id, {
        "businessId": biz_id,
        "referralId": referral["id"],
        "refereePhone": db._clean_phone(customer_phone),
        "referrerPhone": referral.get("referrerPhone", ""),
        "verifyCode": verify_code,
        "ownerPhone": db._clean_phone(owner_phone),
        "status": "pending",
        "expiresAt": expires_iso,
    })

    customer_name = (
        booking.get("customerName")
        or customer.get("name")
        or customer.get("customerName")
        or "A customer"
    )
    msg = (
        f"👋 *Referral visit — confirmation needed*\n\n"
        f"*{customer_name}* just had an appointment and was referred by one of your customers.\n\n"
        f"Did they show up and receive their discount?\n\n"
        f"✅ Reply *YES {verify_code}* if they came in\n"
        f"❌ Reply *NO* if they did not show up\n\n"
        f"_(This request expires in 24 hours)_"
    )
    await send_to_owner(business, msg)

    db.update_booking(
        booking_id,
        {
            "referralOwnerConfirmationSent": True,
            "referralOwnerConfirmationSentAt": now_iso,
        },
        biz_id,
    )
    logger.info(
        "[VISIT_SWEEP] Owner referral confirmation sent booking=%s code=%s owner=%s biz=%s",
        booking_id, verify_code, owner_phone, biz_id,
    )
