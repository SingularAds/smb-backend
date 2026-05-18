"""Trial Expiry Automation — Post-trial subscription reminders and service gate.

Runs as a daily scheduled job.  For each business whose 7-day trial has expired:

  1. Blocks AI replies (enforced at the webhook layer via feature_gate.py).
  2. Sends the owner a WhatsApp + SMS reminder on expiry day (0), day 1, day 3,
     and day 7 after expiry, prompting them to choose a plan.
  3. Stops sending reminders once ANY of these is true:
       - business.plan  becomes 'starter' or 'pro'  (paid subscription active)
       - business.status == 'inactive' / 'cancelled'
       - business.suppressTrialReminders == True   (owner opted out)

Reminder fields written to the business doc:
  trialExpiryReminderDay0SentAt
  trialExpiryReminderDay1SentAt
  trialExpiryReminderDay3SentAt
  trialExpiryReminderDay7SentAt
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app import firestore as db
from app.services.automation.whatsapp_notifier import _wa
from app.config import settings

logger = logging.getLogger(__name__)

# Reminder schedule: days AFTER trial expiry
_REMINDER_DAYS: list[int] = [0, 1, 3, 7]
# Field name per reminder day
_REMINDER_FIELD: dict[int, str] = {
    0: "trialExpiryReminderDay0SentAt",
    1: "trialExpiryReminderDay1SentAt",
    3: "trialExpiryReminderDay3SentAt",
    7: "trialExpiryReminderDay7SentAt",
}
# Plans that mean the owner is subscribed — stop all reminders
_SUBSCRIBED_PLANS = {"starter", "pro", "active"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_subscribed(business: dict) -> bool:
    """Return True when the business has an active paid subscription."""
    plan = str(business.get("plan") or "").lower()
    return plan in _SUBSCRIBED_PLANS


def _is_stopped(business: dict) -> bool:
    """Return True when reminders must not be sent."""
    if _is_subscribed(business):
        return True
    if business.get("suppressTrialReminders"):
        return True
    status = str(business.get("status") or "").lower()
    if status in ("inactive", "cancelled", "disabled"):
        return True
    return False


def _build_reminder_message(business: dict, days_after_expiry: int) -> str:
    """Build a WhatsApp/SMS reminder message for the owner."""
    biz_name = business.get("name") or "your business"
    starter_price = business.get("starterPriceEur") or 29
    pro_price = business.get("proPriceEur") or 69
    tier = business.get("billingTier") or "T2"

    if days_after_expiry == 0:
        header = "⏰ *Your 7-day free trial has ended.*"
    elif days_after_expiry == 1:
        header = "📣 *Your Recepte trial ended yesterday.*"
    elif days_after_expiry == 3:
        header = "⚠️ *Recepte has been paused for 3 days.*"
    else:
        header = "🔴 *Your AI receptionist has been offline for a week.*"

    msg = (
        f"{header}\n\n"
        f"*{biz_name}* — your AI receptionist is currently paused because "
        f"your free trial has expired and no plan has been selected yet.\n\n"
        f"*Choose a plan to reactivate:*\n"
        f"  • *Starter* — €{starter_price}/month — AI receptionist + bookings\n"
        f"  • *PRO* — €{pro_price}/month — Everything + full marketing engine\n\n"
        f"To subscribe, visit: {settings.BASE_URL.rstrip('/')}/billing\n\n"
        f"Reply *STOP* to stop receiving these reminders."
    )
    return msg


async def _send_owner_reminder(business: dict, days_after_expiry: int) -> bool:
    """Send the reminder via WhatsApp + SMS, with optional email."""
    owner_phone = business.get("ownerPhone") or ""
    if not owner_phone:
        logger.warning(
            "[TRIAL-EXPIRY] No ownerPhone for business=%s — skipping reminder",
            business.get("id"),
        )
        return False

    msg = _build_reminder_message(business, days_after_expiry)
    sent = False

    # WhatsApp: use the global onboarding device (Recepte service number → owner)
    onboarding_device = settings.WHATSMEOW_ONBOARDING_DEVICE_ID or settings.WHATSMEOW_DEFAULT_DEVICE_ID
    try:
        await _wa.send_message(owner_phone, msg, device_id=onboarding_device)
        logger.info(
            "[TRIAL-EXPIRY] WhatsApp reminder day=%d sent to owner=%s biz=%s",
            days_after_expiry, owner_phone, business.get("id"),
        )
        sent = True
    except Exception as wa_exc:
        logger.warning(
            "[TRIAL-EXPIRY] WhatsApp reminder failed for owner=%s biz=%s: %s",
            owner_phone, business.get("id"), wa_exc,
        )

    # SMS/message channel via notification_service (second channel)
    try:
        from app.services.notification_service import NotificationService
        _notifier = NotificationService()
        sms_sent = _notifier._send(owner_phone, msg)
        if sms_sent:
            logger.info(
                "[TRIAL-EXPIRY] SMS reminder day=%d sent to owner=%s biz=%s",
                days_after_expiry, owner_phone, business.get("id"),
            )
            sent = True
        else:
            logger.warning(
                "[TRIAL-EXPIRY] SMS reminder failed for owner=%s biz=%s",
                owner_phone, business.get("id"),
            )
    except Exception as sms_exc:
        logger.warning(
            "[TRIAL-EXPIRY] SMS reminder error for owner=%s biz=%s: %s",
            owner_phone, business.get("id"), sms_exc,
        )

    # Optional email channel (when ownerEmail exists)
    owner_email = business.get("ownerEmail") or ""
    if owner_email:
        try:
            from app.services.mail_service import MailService
            _mail = MailService()
            html = msg.replace("\n", "<br>").replace("*", "<b>", 1)
            # Simple plain-text-to-html: replace all bold markers
            import re
            html = re.sub(r"\*(.+?)\*", r"<b>\1</b>", msg.replace("\n", "<br>"))
            _mail._send_email(
                to_email=owner_email,
                subject=f"Recepte — Your trial has expired for {business.get('name', '')}",
                html_content=f"<p>{html}</p>",
                text_content=msg,
            )
            logger.info(
                "[TRIAL-EXPIRY] Email reminder day=%d sent to email=%s biz=%s",
                days_after_expiry, owner_email, business.get("id"),
            )
            sent = True
        except Exception as mail_exc:
            logger.warning(
                "[TRIAL-EXPIRY] Email reminder failed for biz=%s: %s",
                business.get("id"), mail_exc,
            )

    return sent


async def run_trial_expiry_sweep() -> None:
    """Daily job: find expired trials and send reminders per schedule."""
    now = _now()
    businesses = db.list_active_businesses(limit=500)

    for biz in businesses:
        try:
            await _process_single_business(biz, now)
        except Exception as exc:
            logger.exception(
                "[TRIAL-EXPIRY] Error processing business=%s: %s",
                biz.get("id"), exc,
            )

    logger.info("[TRIAL-EXPIRY] Sweep complete — checked %d businesses", len(businesses))


async def _process_single_business(business: dict, now: datetime) -> None:
    """Evaluate one business and send a reminder if due."""
    plan = str(business.get("plan") or "").lower()
    biz_id = business.get("id") or ""

    # Only process businesses on trial or onboarding plan
    if plan not in ("trialing", "trial", "onboarding"):
        return

    # Check if trial has ended
    trial_ends_raw = business.get("trialEndsAt")
    if not trial_ends_raw:
        # plan=onboarding with no trialEndsAt means WhatsApp was never connected — skip
        return

    trial_end = _parse_dt(trial_ends_raw)
    if not trial_end:
        return

    if now <= trial_end:
        return  # Trial still active — no reminder needed

    # Trial has expired.  Check subscription status.
    if _is_stopped(business):
        return

    # Calculate days since expiry (floor)
    days_after = int((now - trial_end).total_seconds() // 86400)

    # Find which reminder day is due (check in order: 0, 1, 3, 7)
    for day in _REMINDER_DAYS:
        if days_after < day:
            break  # too early for this and all subsequent days
        field = _REMINDER_FIELD[day]
        if business.get(field):
            continue  # already sent
        # Due and not yet sent — send it
        sent = await _send_owner_reminder(business, day)
        if sent:
            db.update_business_doc(biz_id, {field: now.isoformat()})
            logger.info(
                "[TRIAL-EXPIRY] Marked reminder day=%d for business=%s",
                day, biz_id,
            )
        break  # send at most one reminder per sweep cycle
