"""Referral Automation — sends delayed referral invite messages.

The sweep runs every 15 minutes.  It looks for completed bookings that:
  1. Have ``referralInviteScheduled = True``
  2. Have ``referralInviteSent = False`` (or field absent)
  3. Were completed at least 90 minutes ago

When found, it builds a WhatsApp deep-link and sends the customer an
invite asking them to share their referral code with friends.

The deep-link pre-fills a message like:
  "Hi, I was referred by {customer_name}. Code: {code}"
so the friend only needs to tap Send — no typing required.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone, timedelta

from app import firestore as db
from app.services.automation.whatsapp_notifier import send_to_customer
from app.services.referral_service import get_or_create_referral_code

logger = logging.getLogger(__name__)

_INVITE_DELAY_MINUTES = 90


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


async def run_referral_invite_sweep() -> None:
    """Sweep all active businesses for completed bookings that need referral invites."""
    now = _now()
    logger.info("[REFERRAL_SWEEP] starting at %s", now.isoformat())
    businesses = db.list_active_businesses()
    total_sent = 0

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id:
            continue
        # Only businesses with an active WhatsApp session
        if not business.get("waSessionId"):
            continue

        # Feature flag — only send invites if business opted in
        if not business.get("referralFeatureEnabled"):
            continue

        referrer_pct = int(business.get("referrerDiscountPercent") or 25)
        referee_pct = int(business.get("refereeDiscountPercent") or 10)
        cooldown_days = int(business.get("referralInviteCooldownDays") or 90)

        try:
            bookings = db.list_bookings_by_status(biz_id, "completed", limit=200)
            for b in bookings:
                # Must be flagged for invite and not yet sent
                if not b.get("referralInviteScheduled"):
                    continue
                if b.get("referralInviteSent"):
                    continue

                completed_dt = _parse_dt(b.get("completedAt"))
                if not completed_dt:
                    continue

                minutes_since = (now - completed_dt).total_seconds() / 60
                if minutes_since < _INVITE_DELAY_MINUTES:
                    continue  # Not yet time

                customer_phone = b.get("customerPhone", "")
                if not customer_phone:
                    continue

                # Per-customer guards: opt-out and cooldown
                customer = db.get_customer_by_phone(biz_id, customer_phone)
                if (customer or {}).get("referralOptOut"):
                    logger.info(
                        "[REFERRAL_SWEEP] Skipping %s — opted out (biz=%s)",
                        customer_phone, biz_id,
                    )
                    db.update_booking(b["id"], {"referralInviteSent": True, "referralInviteSentAt": "skipped_opt_out"}, biz_id)
                    continue

                last_invite_dt = _parse_dt((customer or {}).get("lastReferralInviteAt"))
                if last_invite_dt:
                    days_since = (now - last_invite_dt).days
                    if days_since < cooldown_days:
                        logger.info(
                            "[REFERRAL_SWEEP] Skipping %s — cooldown (%d/%d days, biz=%s)",
                            customer_phone, days_since, cooldown_days, biz_id,
                        )
                        continue

                try:
                    await _send_referral_invite(business, b, customer_phone, referrer_pct=referrer_pct, referee_pct=referee_pct)
                    db.update_booking(
                        b["id"],
                        {
                            "referralInviteSent": True,
                            "referralInviteSentAt": now.isoformat(),
                        },
                        biz_id,
                    )
                    db.upsert_customer(biz_id, customer_phone, {"lastReferralInviteAt": now.isoformat()})
                    total_sent += 1
                    logger.info(
                        "[REFERRAL_SWEEP] Invite sent to %s (booking=%s biz=%s)",
                        customer_phone, b["id"], biz_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "[REFERRAL_SWEEP] Failed to send invite for booking %s: %s",
                        b.get("id"), exc,
                    )

        except Exception as exc:
            logger.exception(
                "[REFERRAL_SWEEP] Error processing biz %s: %s", biz_id, exc
            )

    logger.info("[REFERRAL_SWEEP] done — %d invites sent", total_sent)


async def _send_referral_invite(
    business: dict,
    booking: dict,
    customer_phone: str,
    referrer_pct: int = 25,
    referee_pct: int = 10,
) -> None:
    """Generate referral code and send the invite message to the customer."""
    business_id = business["id"]
    code = get_or_create_referral_code(business_id, customer_phone)

    customer = db.get_customer_by_phone(business_id, customer_phone)
    customer_name: str = (
        (customer or {}).get("name")
        or (customer or {}).get("customerName")
        or booking.get("customerName")
        or "there"
    )
    biz_name = business.get("name", "us")

    # Build WhatsApp deep-link so the friend only needs to tap Send.
    # Priority: waPhoneNumber (set on WA connect) → ownerPhone → businessPhone
    wa_number = (
        business.get("waPhoneNumber")
        or business.get("ownerPhone")
        or business.get("businessPhone")
        or ""
    )
    wa_number_clean = wa_number.lstrip("+").replace(" ", "").replace("-", "")
    # Only use the number if it's purely digits (guard against placeholder values)
    if not wa_number_clean.isdigit():
        wa_number_clean = ""

    referral_text = f"Hi, I was referred by {customer_name}. Code: {code}"
    encoded_text = urllib.parse.quote(referral_text)

    wa_link = (
        f"https://wa.me/{wa_number_clean}?text={encoded_text}"
        if wa_number_clean
        else ""
    )

    link_section = f"\n\nShare this link with your friend:\n{wa_link}" if wa_link else ""

    msg = (
        f"💛 *Thank you for your visit to {biz_name}, {customer_name}!*\n\n"
        f"We loved having you! 😊 Why not share the experience?\n\n"
        f"Your referral code: *{code}*{link_section}\n\n"
        f"When your friend completes their first visit, "
        f"you'll earn a *{referrer_pct}% discount* on your next appointment! 🎉\n"
        f"Your friend gets *{referee_pct}% off* their first visit too!"
    )
    await send_to_customer(business, customer_phone, msg)


async def run_referral_discount_expiry_sweep() -> None:
    """Daily sweep: expire pending discounts past their expiry date.

    Updates the customer record (clears pendingDiscount) and marks
    the matching referral doc as 'expired'.
    """
    now = _now()
    logger.info("[REFERRAL_EXPIRY] starting at %s", now.isoformat())
    businesses = db.list_active_businesses()
    total_expired = 0

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id:
            continue
        if not business.get("referralFeatureEnabled"):
            continue

        try:
            expired_customers = db.list_customers_with_expired_discounts(biz_id)
            for customer in expired_customers:
                customer_phone = customer.get("phone") or customer.get("id", "")
                if not customer_phone:
                    continue

                pending_reason = customer.get("pendingDiscountReason") or ""
                logger.info(
                    "[REFERRAL_EXPIRY] Expiring discount for %s (%s, biz=%s)",
                    customer_phone, pending_reason, biz_id,
                )

                db.upsert_customer(biz_id, customer_phone, {
                    "pendingDiscount": None,
                    "pendingDiscountReason": None,
                    "pendingDiscountExpiresAt": None,
                    "pendingDiscountUsed": False,
                })

                # Update referral doc status to 'expired'
                try:
                    if pending_reason == "referral_first_visit":
                        referral = db.get_referral_by_referee(biz_id, customer_phone)
                    elif pending_reason == "referral_reward":
                        referral = db.get_referral_by_referrer(biz_id, customer_phone)
                    else:
                        referral = None

                    if referral:
                        db.update_referral_doc(biz_id, referral["id"], {
                            "status": "expired",
                            "expiredAt": now.isoformat(),
                        })
                except Exception as exc:
                    logger.warning(
                        "[REFERRAL_EXPIRY] Could not update referral doc for %s: %s",
                        customer_phone, exc,
                    )

                total_expired += 1

        except Exception as exc:
            logger.exception(
                "[REFERRAL_EXPIRY] Error processing biz %s: %s", biz_id, exc
            )

    logger.info("[REFERRAL_EXPIRY] done — %d discounts expired", total_expired)
