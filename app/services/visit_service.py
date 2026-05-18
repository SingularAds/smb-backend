"""Visit Confirmation Service — tracks real-world visit outcomes.

Problem
-------
When a booking time passes, we cannot automatically know if the customer
actually showed up. The old no-show sweep assumed everyone who didn't
explicitly cancel was a no-show, which produced false "We missed you!"
messages for customers who DID visit.

Solution
--------
1. All bookings default to ``visited = True`` (benefit of the doubt).
   The field is only flipped to ``False`` when:
     a) The customer themselves replies NO to the confirmation message.
     b) The booking is explicitly cancelled (by customer or owner).

2. 2 hours after the scheduled booking time, the sweep (invoked by the
   scheduler every 30 min) sends:
       "Did you visit us? Reply YES or NO"
   The booking is flagged: ``visitConfirmationSent = True``.

3. The WhatsApp webhook intercepts YES / NO replies BEFORE any referral
   detection or AI routing:

   YES → ``status = completed``, ``visited = True``, ``completedAt`` set.
         ``on_booking_completed()`` runs (visit count, referral rewards, etc.)
         A thank-you message is sent.

   NO  → ``status = noshow``, ``visited = False``.
         A gentle reschedule-offer message is sent.

4. Replies arriving after 48 h are out-of-window and fall through to AI.

State chart
-----------
  confirmed / pending
       │
   (+ 2 h)
       │
  [visit confirmation sent]
       │
   ┌───┴────┐
   │YES     │NO
   ▼        ▼
 completed  noshow
 visited=T  visited=F
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

from app import firestore as db
from app.services.automation.whatsapp_notifier import send_to_customer
from app.services.referral_service import on_booking_completed

logger = logging.getLogger(__name__)

# ── reply detection ───────────────────────────────────────────────────────────

_YES_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ya|yah|y|si|sim|confirma|confirmed|ok|okay|sure|✓|👍)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^\s*(no|nope|nah|na|nao|não|n|didn[''']?t|did\s+not)\b",
    re.IGNORECASE,
)

# How long we accept a YES/NO after sending the question
_CONFIRMATION_WINDOW_HOURS = 48


def is_yes_reply(text: str) -> bool:
    """True if the message is a YES-like confirmation."""
    return bool(_YES_RE.match(text.strip()))


def is_no_reply(text: str) -> bool:
    """True if the message is a NO-like denial."""
    return bool(_NO_RE.match(text.strip()))


# ── main entry point ──────────────────────────────────────────────────────────

async def handle_visit_reply(
    business_id: str,
    customer_phone: str,
    body: str,
    business: dict,
) -> bool:
    """Intercept a YES/NO visit-confirmation reply from a customer.

    Returns True  → message was handled; caller must stop further routing.
    Returns False → not a visit reply (no matching booking, or window expired,
                     or the customer is in an active AI booking conversation);
                     caller should continue with normal routing.
    """
    text = body.strip()

    # Pre-filter: only spend a DB round-trip for yes/no-like text
    if not (is_yes_reply(text) or is_no_reply(text)):
        return False

    booking = db.get_pending_visit_confirmation_booking(business_id, customer_phone)
    if not booking:
        # Normal "yes/no" that isn't for a visit confirmation — let AI handle
        return False

    # ── Active-conversation guard ─────────────────────────────────────────────
    # If the customer has been chatting with the AI *after* the visit confirmation
    # was sent (e.g. they started a new booking conversation), the YES/NO belongs
    # to that conversation — not to the visit confirmation.  Let the AI handle it.
    sent_at_raw = booking.get("visitConfirmationSentAt")
    if sent_at_raw:
        try:
            sent_at = datetime.fromisoformat(
                str(sent_at_raw).replace("Z", "+00:00")
            )
            if not sent_at.tzinfo:
                sent_at = sent_at.replace(tzinfo=timezone.utc)

            convo = db.get_customer_conversation(business_id, customer_phone)
            if convo:
                last_ai_raw = convo.get("lastMessageAt")
                if last_ai_raw:
                    last_ai = datetime.fromisoformat(
                        str(last_ai_raw).replace("Z", "+00:00")
                    )
                    if not last_ai.tzinfo:
                        last_ai = last_ai.replace(tzinfo=timezone.utc)
                    if last_ai > sent_at:
                        logger.info(
                            "[VISIT] skipping visit-confirmation intercept for booking %s "
                            "— AI conversation active after confirmation was sent "
                            "(last_ai=%s > sent_at=%s)",
                            booking.get("id"), last_ai.isoformat(), sent_at.isoformat(),
                        )
                        return False
        except (ValueError, TypeError) as exc:
            logger.debug("[VISIT] could not compare timestamps for booking %s: %s", booking.get("id"), exc)

    # Check the 48-hour window
    sent_at_raw = booking.get("visitConfirmationSentAt")
    if sent_at_raw:
        try:
            sent_at = datetime.fromisoformat(
                str(sent_at_raw).replace("Z", "+00:00")
            )
            if not sent_at.tzinfo:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            elapsed_h = (datetime.now(timezone.utc) - sent_at).total_seconds() / 3600
            if elapsed_h > _CONFIRMATION_WINDOW_HOURS:
                logger.info(
                    "[VISIT] confirmation window expired for booking %s "
                    "(%.1f h > %d h limit) — falling through to AI",
                    booking.get("id"), elapsed_h, _CONFIRMATION_WINDOW_HOURS,
                )
                return False
        except (ValueError, TypeError):
            pass  # If we can't parse, proceed anyway

    booking_id = booking["id"]

    if is_yes_reply(text):
        await _handle_yes(business_id, customer_phone, booking_id, booking, business)
    else:
        await _handle_no(business_id, customer_phone, booking_id, booking, business)

    return True


# ── YES / NO handlers ─────────────────────────────────────────────────────────

async def _handle_yes(
    business_id: str,
    customer_phone: str,
    booking_id: str,
    booking: dict,
    business: dict,
) -> None:
    """Customer confirmed they visited — complete the booking and run post-visit logic."""
    now_iso = datetime.utcnow().isoformat()

    db.update_booking(
        booking_id,
        {
            "status": "completed",
            "visited": True,
            "visitConfirmedAt": now_iso,
            "completedAt": now_iso,
        },
        business_id,
    )
    logger.info("[VISIT] YES — booking %s completed (customer=%s)", booking_id, customer_phone)

    # Post-completion referral / discount / visit-count logic
    try:
        await on_booking_completed(
            business_id=business_id,
            customer_phone=customer_phone,
            booking={**booking, "status": "completed", "completedAt": now_iso},
            business=business,
        )
    except Exception as exc:
        logger.warning("[VISIT] on_booking_completed failed for booking %s: %s", booking_id, exc)

    customer_name = booking.get("customerName") or "there"
    service = booking.get("serviceName") or "your visit"
    biz_name = business.get("name", "us")

    await send_to_customer(
        business,
        customer_phone,
        f"✅ *Thank you, {customer_name}!*\n\n"
        f"So glad you came in for *{service}* at *{biz_name}*! "
        f"We hope to see you again soon. 😊",
    )


async def _handle_no(
    business_id: str,
    customer_phone: str,
    booking_id: str,
    booking: dict,
    business: dict,
) -> None:
    """Customer said they didn't visit — mark as no-show and offer to reschedule."""
    now_iso = datetime.utcnow().isoformat()

    db.update_booking(
        booking_id,
        {
            "status": "noshow",
            "visited": False,
            "visitDeclinedAt": now_iso,
        },
        business_id,
    )
    logger.info("[VISIT] NO — booking %s marked noshow (customer=%s)", booking_id, customer_phone)

    customer_name = booking.get("customerName") or "there"
    service = booking.get("serviceName") or "your appointment"
    biz_name = business.get("name", "us")

    await send_to_customer(
        business,
        customer_phone,
        f"👋 *No worries, {customer_name}!*\n\n"
        f"Thanks for letting us know. We'd love to have you visit *{biz_name}* "
        f"for *{service}* soon!\n\n"
        f"Would you like to reschedule? Just tell us a date and time that works for you. 📅",
    )
