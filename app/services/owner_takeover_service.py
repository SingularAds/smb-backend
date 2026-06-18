"""Owner-takeover handling.

When the owner manually replies in a customer's chat (using their primary
WhatsApp device — the one linked to the business number), the bridge sees
the outbound echo and forwards it as a new event ``owner_message``. This
module turns that signal into the user-facing behaviour:

  * If the message is a resume command ("resume" / "resume ai"),
    clear the pause and confirm to the owner.
  * Otherwise it's a genuine takeover:
      - pause AI for that customer for DEFAULT_PAUSE_MINUTES
      - record the owner's message in the conversation history so
        future AI replies (after auto-resume) have the context
      - notify the owner that AI is paused (one-time, not per message)

The webhook handler is responsible for filtering out our own API-sent
messages (via ``is_our_outbound_echo``) before reaching this module — by the
time we are called, the message is known to be human-typed by the owner.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app import firestore as db
from app.services import ai_pause_service
from app.services.ai_pause_service import (
    DEFAULT_PAUSE_MINUTES,
    PauseReason,
    is_active_pause,
    is_resume_command,
)
from app.services.automation import whatsapp_notifier

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 30  # match customer_ai_service to avoid drift


def _format_customer_label(convo: dict | None, customer_phone: str) -> str:
    """Human-readable label for the owner notification ('Sarah (+91...)' or just phone)."""
    name = ""
    if convo:
        name = (convo.get("customerName") or "").strip()
    if name:
        return f"{name} (+{customer_phone.lstrip('+')})"
    return f"+{customer_phone.lstrip('+')}"


async def handle_owner_message(
    business: dict,
    customer_phone: str,
    body: str,
) -> None:
    """Process a human-typed owner message in a customer's chat.

    Idempotent at the message level — re-delivering the same body in a paused
    chat simply extends the pause window; no double notification is sent
    because we only notify on the *transition* from "not paused" to "paused".
    """
    if not body or not body.strip():
        return  # media-only owner messages: ignore for pause purposes

    business_id = business["id"]
    phone_clean = db._clean_phone(customer_phone)

    # ── Defence in depth ────────────────────────────────────────────────────
    # The webhook layer already filters owner replies in protected chats
    # (global Recepte number, owner's own phone, admin phones), but the
    # service layer guards independently so direct callers / tests / future
    # entry-points never corrupt the global support thread.
    if ai_pause_service.is_protected_number(phone_clean, business):
        logger.info(
            "[OWNER-TAKEOVER] refusing pause/resume — phone=%s is protected; "
            "business=%s body=%r",
            phone_clean, business_id, body[:60],
        )
        return

    convo = db.get_customer_conversation(business_id, phone_clean)

    # ── Resume command ───────────────────────────────────────────────────────
    if is_resume_command(body):
        # "resume … <phone>" overrides the implicit chat phone — useful when
        # the owner types the command in any chat and names the customer.
        target_phone = ai_pause_service.extract_resume_phone(body) or phone_clean
        target_phone = db._clean_phone(target_phone)
        if ai_pause_service.is_protected_number(target_phone, business):
            logger.info(
                "[OWNER-TAKEOVER] refusing resume — target phone=%s is protected",
                target_phone,
            )
            return
        target_convo = convo if target_phone == phone_clean else db.get_customer_conversation(business_id, target_phone)
        if is_active_pause(target_convo):
            ai_pause_service.resume(business_id, target_phone)
            label = _format_customer_label(target_convo, target_phone)
            await _notify_owner_safely(
                business,
                f"✅ *AI resumed* for {label}.\nThe bot is handling this chat again.",
            )
        else:
            logger.info(
                "[OWNER-TAKEOVER] resume command for non-paused chat business=%s phone=%s",
                business_id, target_phone,
            )
        return

    # ── Out-of-scope pass-through ─────────────────────────────────────────────
    # If the AI just silenced itself because the customer's last message was
    # out-of-scope (e.g. "do you have chicken?"), the owner is replying to
    # answer THAT specific question — not taking over the conversation thread.
    # Triggering a 90-minute pause here would block the AI from handling the
    # customer's very next booking request.
    # Grace window: 30 minutes after an out-of-scope classification.
    _oos_ts = (convo or {}).get("lastOutOfScopeAt")
    if _oos_ts:
        try:
            _oos_age = datetime.utcnow() - datetime.fromisoformat(_oos_ts)
            if _oos_age < timedelta(minutes=30):
                # Owner is answering the out-of-scope question — just record in
                # history and leave AI active for the next customer message.
                messages: list = (convo or {}).get("messages", [])
                messages.append({"role": "assistant", "content": body, "origin": "owner"})
                if len(messages) > MAX_HISTORY_MESSAGES:
                    messages = messages[-MAX_HISTORY_MESSAGES:]
                db.upsert_customer_conversation(business_id, phone_clean, {
                    "messages":      messages,
                    "customerPhone": phone_clean,
                    "businessId":    business_id,
                    "lastMessageAt": datetime.utcnow().isoformat(),
                    # Clear the flag so a second owner reply triggers normal takeover
                    "lastOutOfScopeAt": None,
                })
                logger.info(
                    "[OWNER-TAKEOVER] skipping pause — owner replied within 30 min of "
                    "out-of-scope message business=%s phone=%s",
                    business_id, phone_clean,
                )
                return
        except (ValueError, TypeError):
            pass  # malformed timestamp → fall through to normal takeover

    # ── Genuine takeover ─────────────────────────────────────────────────────
    was_already_paused = is_active_pause(convo)

    # Append the owner's message to history as an assistant turn. From the
    # customer's perspective owner and AI both speak as the business — keeping
    # the history uniform means classifier + AI continuity work correctly when
    # AI eventually resumes.
    messages: list = (convo or {}).get("messages", [])
    messages.append({"role": "assistant", "content": body, "origin": "owner"})
    if len(messages) > MAX_HISTORY_MESSAGES:
        messages = messages[-MAX_HISTORY_MESSAGES:]

    ai_pause_service.pause(
        business_id=business_id,
        customer_phone=phone_clean,
        reason=PauseReason.OWNER_TAKEOVER,
        snippet=body,
        business=business,
    )
    # pause() upserted the pause fields; now persist the updated message list
    # without overwriting the pause fields we just set.
    db.upsert_customer_conversation(business_id, phone_clean, {
        "messages":       messages,
        "customerPhone":  phone_clean,
        "businessId":     business_id,
        "lastMessageAt":  datetime.utcnow().isoformat(),
    })

    if not was_already_paused:
        label = _format_customer_label(convo, phone_clean)
        await _notify_owner_safely(
            business,
            (
                f"🤖 *AI paused* for {label}.\n"
                f"You're handling this chat — AI resumes automatically in "
                f"{DEFAULT_PAUSE_MINUTES} minutes.\n\n"
                f"To resume early (without the customer seeing), send this "
                f"to your business WhatsApp number:\n"
                f"*resume {phone_clean}*"
            ),
        )


async def _notify_owner_safely(business: dict, message: str) -> None:
    """Send to owner via business device; swallow errors so they never break
    the takeover flow."""
    try:
        await whatsapp_notifier.send_to_owner(business, message)
    except Exception as exc:
        logger.warning(
            "[OWNER-TAKEOVER] could not notify owner business=%s: %s",
            business.get("id"), exc,
        )
