"""WhatsApp Webhook — receives events from the whatsmeow bridge.

The bridge POSTs every incoming message / connection event to this
endpoint.  We validate the webhook secret, filter for relevant
events, and hand off to the ``OnboardingService``.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response

from app.config import settings
from app.services.onboarding_service import OnboardingService
from app.services.customer_ai_service import CustomerAIService
from app.services.owner_takeover_service import handle_owner_message
from app.services import ai_pause_service
from app.services.whatsmeow_client import WhatsmeowClient, is_our_outbound_echo
from app.owner.commands.handlers import handle_owner_command, is_owner_message
from app import firestore as db
from app.integrations import cartesia_client

logger = logging.getLogger(__name__)
router = APIRouter()

_onboarding = OnboardingService()
_customer_ai = CustomerAIService()
_wa = WhatsmeowClient()
_onboarding_device_id = settings.WHATSMEOW_ONBOARDING_DEVICE_ID or settings.WHATSMEOW_DEFAULT_DEVICE_ID

# ── server startup timestamp (UTC monotonic) ──────────────────────────────────
# Used to skip WhatsApp offline-queue replay messages.  When the bridge
# reconnects after a restart WA pushes back all messages it received while
# the bridge was down.  Any message whose payload timestamp pre-dates our own
# startup (minus a small grace window) must have been handled in the previous
# session — processing it again would generate duplicate AI replies.
_SERVER_START_MONOTONIC: float = time.monotonic()
_SERVER_START_WALL: float = time.time()   # Unix epoch seconds

# Messages older than this many seconds before server start are replay-skipped.
# 120 s gives a comfortable margin — if the server was restarted quickly a
# legitimate in-flight message (arrived just before restart) still has 2 min
# to be delivered before we consider it "old".
_REPLAY_GRACE_S: int = 120

# ── in-memory event log (last 50 attempts, cleared on restart) ────────────────
_events: deque[dict[str, Any]] = deque(maxlen=50)

# ── message deduplication cache ───────────────────────────────────────────────
# Maps message_id -> expiry_monotonic_time.  Entries live for 60 seconds which
# is long enough to cover any bridge retry window without growing unbounded.
# Cleaned up lazily (purge on every insert).
_DEDUP_TTL_S = 60
_seen_message_ids: dict[str, float] = {}


def _is_duplicate(message_id: str) -> bool:
    """Return True if this message_id was already processed within the TTL."""
    if not message_id:
        return False
    now = time.monotonic()
    # Lazy purge of expired entries
    expired = [k for k, exp in _seen_message_ids.items() if now > exp]
    for k in expired:
        del _seen_message_ids[k]
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = now + _DEDUP_TTL_S
    return False


# ── call deduplication cache ──────────────────────────────────────────────────
# Prevents duplicate voice-note follow-ups when the bridge retries the
# call_missed webhook.  TTL is longer (5 min) because call ID collision
# across retries is the main risk we're guarding against.
_CALL_DEDUP_TTL_S = 300
_seen_call_ids: dict[str, float] = {}


def _is_duplicate_call(call_id: str) -> bool:
    """Return True if we already sent a follow-up for this call_id."""
    if not call_id:
        return False
    now = time.monotonic()
    expired = [k for k, exp in _seen_call_ids.items() if now > exp]
    for k in expired:
        del _seen_call_ids[k]
    if call_id in _seen_call_ids:
        return True
    _seen_call_ids[call_id] = now + _CALL_DEDUP_TTL_S
    return False


def _is_owner_own_phone(chat_phone: str, business: dict) -> bool:
    """True when chat_phone is specifically the business owner's own personal phone.

    Used to detect the owner's self-chat on the biz device (a natural command
    channel). Intentionally does NOT match Recepte / admin phones — those are
    handled by is_protected_number and must stay blocked.
    """
    from app.services.ai_pause_service import _digits
    target = _digits(chat_phone)
    if not target:
        return False
    for key in ("ownerPhone", "owner_phone"):
        cand = _digits(business.get(key) or "")
        if not cand:
            continue
        if cand == target:
            return True
        if len(cand) >= 10 and len(target) >= 10 and cand[-10:] == target[-10:]:
            return True
    return False


def _log_event(status: str, phone: str = "", message_id: str = "", detail: str = "", error: str = "") -> None:
    _events.appendleft(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": status,  # "processed" | "skipped" | "error"
            "phone": phone,
            "message_id": message_id,
            "detail": detail,
            "error": error,
        }
    )


# ── Missed-call follow-up ─────────────────────────────────────────────────────

async def _handle_missed_call(payload: dict) -> None:
    """Send a voice-note follow-up to a caller after an auto-rejected missed call.

    Called when the Go bridge fires a ``call_missed`` webhook (20 s timer expired
    and the call was rejected automatically).

    Flow:
      1. Dedup on call_id — only one follow-up per missed call.
      2. Look up the business that owns the device.
      3. Generate a short voice note via Cartesia TTS.
      4. Send the voice note to the caller.
      5. Fall back to a text message if audio fails.
    """
    device_id = payload.get("device_id", "")
    data = payload.get("payload", {})
    call_id = (data.get("call_id") or "").strip()
    caller_phone = (data.get("caller_phone") or "").strip()
    caller_jid = (data.get("caller_jid") or "").strip()

    # Use JID as send target if available (preserves @lid etc.), else fall back to digits.
    send_to = caller_jid or caller_phone

    logger.info(
        "[CALL-MISSED] call_id=%r device=%r caller_phone=%r caller_jid=%r",
        call_id, device_id, caller_phone, caller_jid,
    )

    if not call_id or not caller_phone:
        logger.warning("[CALL-MISSED] skipped — missing call_id or caller_phone")
        _log_event("skipped", detail="call_missed: missing call_id or caller_phone")
        return

    # ── Dedup: only one follow-up per call ────────────────────────────────────
    if _is_duplicate_call(call_id):
        logger.info("[CALL-MISSED] skipped — duplicate call_id=%r (already sent follow-up)", call_id)
        _log_event("skipped", phone=caller_phone, detail=f"call_missed duplicate call_id={call_id!r}")
        return

    # ── Look up business ──────────────────────────────────────────────────────
    business = db.get_business_by_wa_session_id(device_id)
    if not business:
        logger.warning("[CALL-MISSED] no business found for device=%r — skipping follow-up", device_id)
        _log_event("skipped", phone=caller_phone, detail=f"call_missed: no business for device {device_id!r}")
        return

    biz_name = business.get("name", "us")
    vs = business.get("verticalSettings", {})
    languages = vs.get("languages", business.get("supportedLanguages", ["en"]))
    lang = (languages[0] if languages else "en")[:2].lower()
    voice_id: str | None = vs.get("cartesiaVoiceId") or None

    # ── Build voice note text ─────────────────────────────────────────────────
    missed_call_text = (
        f"Hey! You just called {biz_name}. We're here on WhatsApp — "
        "voice or text, whatever's easier. "
        "Tell me how I can help: book, get info, or edit an appointment?"
    )

    logger.info(
        "[CALL-MISSED] generating voice note for caller=%r business=%r lang=%s",
        caller_phone, biz_name, lang,
    )

    # ── TTS + send voice note ─────────────────────────────────────────────────
    try:
        audio_bytes = await cartesia_client.synthesize(
            missed_call_text, voice_id=voice_id, language=lang
        )
        logger.info(
            "[CALL-MISSED] synthesized %d bytes — sending voice note to %s (device=%s)",
            len(audio_bytes), caller_phone, device_id,
        )
        await _wa.send_audio(
            send_to, audio_bytes, device_id=device_id,
            mime_type=cartesia_client.OUTPUT_MIME_TYPE, ptt=True,
        )
        logger.info(
            "[CALL-MISSED] voice note sent successfully to %s (call_id=%r)",
            caller_phone, call_id,
        )
        _log_event("call_missed_followup", phone=caller_phone, detail=f"voice note sent call_id={call_id!r}")
    except Exception as exc:
        logger.error(
            "[CALL-MISSED] voice note failed for %s (call_id=%r) — falling back to text: %s",
            caller_phone, call_id, exc,
        )
        # ── Text fallback ─────────────────────────────────────────────────
        text_fallback = (
            f"👋 You just called {biz_name}. We couldn't answer, but we're here!\n"
            f"Send us a message and we'll help you right away 😊"
        )
        try:
            await _wa.send_message(send_to, text_fallback, device_id=device_id)
            logger.info("[CALL-MISSED] text fallback sent to %s", caller_phone)
            _log_event(
                "call_missed_followup",
                phone=caller_phone,
                detail=f"text fallback sent call_id={call_id!r} (audio failed: {exc})",
            )
        except Exception as fallback_exc:
            logger.error(
                "[CALL-MISSED] text fallback also failed for %s: %s",
                caller_phone, fallback_exc,
            )
            _log_event(
                "error",
                phone=caller_phone,
                detail=f"call_missed follow-up FAILED call_id={call_id!r}",
                error=str(fallback_exc),
            )


async def _process_webhook(payload: dict) -> None:
    """Background task: process a validated webhook payload."""
    try:
        event = payload.get("event")
        device_id = payload.get("device_id", "")
        data = payload.get("payload", {})

        logger.info(
            "[PROCESS] event=%r device=%r from=%r chat=%r msg_id=%r body=%r",
            event,
            device_id,
            data.get("from", ""),
            data.get("chat_id", ""),
            data.get("message_id", ""),
            (data.get("body") or "")[:100],
        )

        # ── Call events ────────────────────────────────────────────────────────
        if event == "call_offer":
            logger.info("[CALL_OFFER] call_id=%r caller=%r", data.get("call_id"), data.get("caller_phone"))
            logger.info(
                "[CALL] call_offer received — session=%r call_id=%r caller_phone=%r caller_jid=%r",
                device_id,
                data.get("call_id", ""),
                data.get("caller_phone", ""),
                data.get("caller_jid", ""),
            )
            _log_event(
                "call_offer",
                phone=data.get("caller_phone", ""),
                detail=f"call_id={data.get('call_id', '')!r} caller_jid={data.get('caller_jid', '')!r}",
            )
            return

        if event == "call_missed":
            logger.info("[CALL_MISSED] call_id=%r caller=%r", data.get("call_id"), data.get("caller_phone"))
            await _handle_missed_call(payload)
            return

        # ── Owner takeover: outbound message in a customer chat ───────────
        # The bridge forwards messages with IsFromMe=true (the owner manually
        # replied from their phone) as event=owner_message. We must filter out
        # our OWN API-sent replies (AI responses, owner-command replies,
        # automation notifications) since those also produce outbound echoes.
        if event == "owner_message":
            # ── Global onboarding number guard ───────────────────────────────
            # If this owner_message came from the Recepte global onboarding
            # number (configured via WHATSMEOW_GLOBAL_NUMBER), do NOT run the
            # customer-takeover path. The global number is not tied to any
            # single SMB owner↔customer relationship, so treating an outbound
            # from it as a "takeover" would trigger a spurious 90-minute AI
            # pause on whichever chat happens to be the recipient. See the
            # 11:32:34 AI-pause incident on 972547778077.
            device_own_phone = payload.get("phone", "")
            if device_own_phone and ai_pause_service.is_protected_number(device_own_phone):
                logger.info(
                    "[OWNER-TAKEOVER] skipping — device own phone=%s is the global "
                    "onboarding number; no AI pause for global flow",
                    device_own_phone,
                )
                _log_event(
                    "skipped",
                    phone=data.get("chat_id", ""),
                    message_id=data.get("message_id", ""),
                    detail=f"owner_message from global onboarding number ({device_own_phone}) — pause suppressed",
                )
                return

            owner_msg_id = data.get("message_id", "")
            if is_our_outbound_echo(owner_msg_id):
                logger.debug(
                    "[OWNER-TAKEOVER] skipping API-sent echo msg_id=%r device=%r",
                    owner_msg_id, device_id,
                )
                return

            owner_chat = (data.get("chat_id") or "").strip()
            owner_body = (data.get("body") or "").strip()
            if not owner_chat or not owner_body:
                logger.info("[OWNER-TAKEOVER] skipping — empty chat_id or body")
                return

            business = db.get_business_by_wa_session_id(device_id)
            if not business:
                logger.warning(
                    "[OWNER-TAKEOVER] no business for device=%r — ignoring takeover",
                    device_id,
                )
                return

            # ── Owner self-chat command channel ──────────────────────────────
            # When the owner types a recognized command in their OWN personal
            # number's chat on the biz device (i.e. the self-chat that appears
            # after a command reply is sent there), execute the command and
            # reply back to that same self-chat.
            # This check runs BEFORE the protected-number guard because the
            # owner's phone IS in the protected list — but for commands from
            # the self-chat we want to process them, not drop them.
            # Non-command text in self-chat (e.g. notes) is still dropped by
            # the protected-number guard below, preventing false takeovers.
            #
            # RESUME_AI is allowed in self-chat ONLY when an explicit customer
            # phone is supplied (e.g. "resume 919905252720"). Without a phone
            # the command has no implicit target in self-chat — silently let
            # it fall through to be handled elsewhere rather than guessing.
            from app.owner.commands.parser import parse_command as _pc_sc, CommandType as _CT_sc
            _sc_cmd = _pc_sc(owner_body)
            _sc_is_resume_with_phone = (
                _sc_cmd["type"] == _CT_sc.RESUME_AI
                and bool((_sc_cmd.get("args") or {}).get("phone"))
            )
            if (
                (
                    _sc_cmd["type"] not in (_CT_sc.UNKNOWN, _CT_sc.RESUME_AI)
                    or _sc_is_resume_with_phone
                )
                and _is_owner_own_phone(owner_chat, business)
            ):
                _is_duplicate(owner_msg_id)
                _sc_owner_phone = (
                    business.get("ownerPhone")
                    or business.get("owner_phone")
                    or device_id.replace("biz-", "", 1)
                )
                logger.info(
                    "[OWNER-CMD-SELF] business=%s cmd=%s body=%r",
                    business.get("id"), _sc_cmd["type"], owner_body[:80],
                )
                try:
                    await handle_owner_command(
                        business=business,
                        message=owner_body,
                        device_id=device_id,
                        owner_phone=_sc_owner_phone,
                        reply_jid=owner_chat,  # reply back into the self-chat
                    )
                    _log_event(
                        "processed",
                        phone=owner_chat,
                        message_id=owner_msg_id,
                        detail=f"owner cmd {_sc_cmd['type']} in self-chat",
                    )
                except Exception:
                    logger.exception(
                        "[OWNER-CMD-SELF] dispatch failed biz=%s cmd=%s",
                        business.get("id"), _sc_cmd["type"],
                    )
                return

            # ── Owner KB confirmation reply in self-chat ─────────────────────
            # `YES KB-XXXX <answer>` or `NO KB-XXXX` is a legitimate self-chat
            # reply but is NOT recognised by the owner-command parser, so it
            # would otherwise fall through to the protected-number guard below
            # (the owner's own phone is in the protected list) and get dropped.
            # Route it explicitly here so the KB confirmation flow completes.
            if _is_owner_own_phone(owner_chat, business):
                from app.services.knowledge_base_service import (
                    parse_owner_reply as _kb_parse_owner_reply,
                    handle_owner_kb_reply as _kb_handle_owner_reply,
                )
                if _kb_parse_owner_reply(owner_body) is not None:
                    _is_duplicate(owner_msg_id)
                    _sc_owner_phone = (
                        business.get("ownerPhone")
                        or business.get("owner_phone")
                        or device_id.replace("biz-", "", 1)
                    )
                    try:
                        kb_handled = await _kb_handle_owner_reply(
                            business=business,
                            owner_phone=_sc_owner_phone,
                            body=owner_body,
                            device_id=device_id,
                        )
                    except Exception:
                        logger.exception(
                            "[OWNER-KB-SELF] handler failed biz=%s body=%r",
                            business.get("id"), owner_body[:80],
                        )
                        kb_handled = False
                    if kb_handled:
                        logger.info(
                            "[OWNER-KB-SELF] business=%s body=%r — KB reply processed",
                            business.get("id"), owner_body[:80],
                        )
                        _log_event(
                            "processed",
                            phone=owner_chat,
                            message_id=owner_msg_id,
                            detail="owner KB confirmation reply (self-chat)",
                        )
                        return

            # ── Protected-chat guard ─────────────────────────────────────────
            # If the owner is replying *inside their chat with Recepte itself*
            # (the global onboarding number, the owner's own phone, or any
            # admin), this is not a customer takeover — it's the owner
            # talking to us / themselves. Pausing AI for that "customer" would
            # corrupt the global support thread.
            if ai_pause_service.is_protected_number(owner_chat, business):
                logger.info(
                    "[OWNER-TAKEOVER] skipping — chat=%r is a protected number "
                    "(global/owner/admin); business=%s",
                    owner_chat, business.get("id"),
                )
                _log_event(
                    "skipped",
                    phone=owner_chat,
                    message_id=owner_msg_id,
                    detail="owner_message in protected (global/owner/admin) chat",
                )
                return

            # Register this message_id so the onboarding device doesn't
            # re-process the same message when it arrives as a regular echo.
            _is_duplicate(owner_msg_id)

            # If the owner typed a reporting/config command while viewing a
            # customer chat (e.g. "Today", "Summary"), execute it as an owner
            # command and do NOT trigger a customer AI-pause / takeover.
            # RESUME_AI is handled by handle_owner_message (it needs the
            # customer's phone to know which pause to clear).
            from app.owner.commands.parser import parse_command as _parse_cmd, CommandType as _CT
            _ocmd = _parse_cmd(owner_body)
            if _ocmd["type"] not in (_CT.UNKNOWN, _CT.RESUME_AI):
                logger.info(
                    "[OWNER-CMD-IN-CHAT] business=%s cmd=%s body=%r",
                    business.get("id"), _ocmd["type"], owner_body[:80],
                )
                _owner_jid = data.get("from", "")
                _owner_phone = (
                    business.get("ownerPhone")
                    or business.get("owner_phone")
                    or device_id.replace("biz-", "", 1)
                )
                try:
                    await handle_owner_command(
                        business=business,
                        message=owner_body,
                        device_id=device_id,
                        owner_phone=_owner_phone,
                        reply_jid=_owner_jid or _owner_phone,
                    )
                    _log_event(
                        "processed",
                        phone=owner_chat,
                        message_id=owner_msg_id,
                        detail=f"owner cmd {_ocmd['type']} in customer chat",
                    )
                except Exception:
                    logger.exception(
                        "[OWNER-CMD-IN-CHAT] dispatch failed biz=%s cmd=%s",
                        business.get("id"), _ocmd["type"],
                    )
                return

            # Not a recognized command — genuine reply in a customer chat
            # (owner takeover) or a resume command for this customer.
            logger.info(
                "[OWNER-TAKEOVER] business=%s chat=%s body=%r",
                business.get("id"), owner_chat, owner_body[:80],
            )
            try:
                await handle_owner_message(
                    business=business,
                    customer_phone=owner_chat,
                    body=owner_body,
                )
                _log_event(
                    "owner_takeover",
                    phone=owner_chat,
                    message_id=owner_msg_id,
                    detail=f"owner replied: {owner_body[:60]!r}",
                )
            except Exception as exc:
                logger.exception(
                    "[OWNER-TAKEOVER] handler failed business=%s chat=%s",
                    business.get("id"), owner_chat,
                )
                _log_event(
                    "error",
                    phone=owner_chat,
                    message_id=owner_msg_id,
                    error=str(exc),
                )
            return

        # Only process incoming text messages
        if event != "message":
            logger.info("[SKIP] event=%r is not 'message'", event)
            _log_event("skipped", detail=f"event={event!r} (not 'message')")
            return

        if data.get("is_from_me"):
            logger.info("[SKIP] is_from_me=True (outbound echo)")
            _log_event("skipped", detail="is_from_me=True")
            return

        if data.get("is_group"):
            logger.info("[SKIP] is_group=True (group message)")
            _log_event("skipped", detail="is_group=True")
            return

        body = (data.get("body") or "").strip()
        message_type = (data.get("message_type") or "").lower()
        media_url = (data.get("media_url") or "").strip()
        mime_type = (data.get("mime_type") or "audio/ogg; codecs=opus").strip()
        is_audio = message_type in ("audio", "ptt") and bool(media_url)
        is_location = message_type == "location" and bool(body)

        if not body and not is_audio:
            logger.info("[SKIP] empty body (type=%r) chat=%r", message_type, data.get('chat_id', '?'))
            _log_event("skipped", detail="empty body")
            return

        phone = data.get("chat_id", "")
        push_name = data.get("push_name", "")
        message_id = data.get("message_id", "")
        sender = data.get("from", "")

        # Skip newsletter / broadcast messages
        if "@newsletter" in sender:
            logger.info("[SKIP] newsletter/broadcast from %s", sender)
            _log_event(
                "skipped",
                phone=phone,
                message_id=message_id,
                detail="newsletter/broadcast message",
            )
            return

        if not phone:
            logger.warning("[SKIP] missing chat_id in payload")
            _log_event("skipped", detail="missing chat_id")
            return

        # ── Self-echo suppression: drop messages where chat_id == device's own number ──
        # WhatsMeow echoes back scheduled/sent messages as incoming webhooks whose
        # chat_id equals the device's own phone.  is_from_me=False is unreliable for
        # these, so we use the top-level `phone` field (always the device's own number).
        device_own_phone = payload.get("phone", "")
        if device_own_phone and phone == device_own_phone:
            logger.info(
                "[WEBHOOK] skipped — self-echo (chat_id=%s == device own phone)", phone
            )
            _log_event("skipped", phone=phone, message_id=message_id, detail="self-echo: chat_id==device own phone")
            return

        # ── Replay suppression: drop offline-queue messages replayed on reconnect ─
        # The bridge's Guard 2 handles this at the Go level. This is a second-line
        # defence for the Python side (e.g. if an older bridge binary is deployed).
        # A message timestamp older than (server_start - REPLAY_GRACE_S) means it
        # was received in a previous server session and must not be duplicated.
        msg_ts = data.get("timestamp")  # Unix epoch seconds (int)
        if msg_ts:
            msg_age_s = _SERVER_START_WALL - int(msg_ts)  # seconds before our start
            if msg_age_s > _REPLAY_GRACE_S:
                logger.info(
                    "[WEBHOOK] skipped — offline-replay msg from %s (age=%ds > grace=%ds)",
                    phone, int(msg_age_s), _REPLAY_GRACE_S,
                )
                _log_event(
                    "skipped",
                    phone=phone,
                    message_id=message_id,
                    detail=f"offline-replay: msg {int(msg_age_s)}s old (pre-startup)",
                )
                return

        logger.info(
            "[WEBHOOK] processing: phone=%s push_name=%r device=%r body=%r",
            phone, push_name, device_id, (body or "<audio>")[:80],
        )

        # ── Deduplication: drop bridge retries for the same message ──────────
        if _is_duplicate(message_id):
            logger.info("[WEBHOOK] skipped — duplicate message_id=%r from %s", message_id, phone)
            _log_event("skipped", phone=phone, message_id=message_id, detail="duplicate (bridge retry)")
            return

        # ── Outbound-echo suppression: drop echoes of messages we sent ────────
        # The bridge fires a webhook for every message it delivers (including ones
        # we asked it to send). Those echoes have a fresh message_id returned by
        # the bridge's /send/message response, which we registered in
        # is_our_outbound_echo(). Without this check the echo re-enters the
        # pipeline and creates an infinite send→echo→send feedback loop.
        if is_our_outbound_echo(message_id):
            logger.info("[WEBHOOK] skipped — outbound echo message_id=%r from %s", message_id, phone)
            _log_event("skipped", phone=phone, message_id=message_id, detail="outbound echo suppressed")
            return

        # ── Routing decision log ──────────────────────────────────────────────
        # Re-read from settings so that test patches (monkeypatch.setattr on settings)
        # take effect without needing to restart the interpreter.
        current_onboarding_device = settings.WHATSMEOW_ONBOARDING_DEVICE_ID or settings.WHATSMEOW_DEFAULT_DEVICE_ID
        routing = "onboarding" if device_id == current_onboarding_device else "customer-ai"
        logger.info(
            "[WEBHOOK] routing phone=%s device=%r -> %s",
            phone, device_id, routing,
        )

        # Messages arriving on the Recepte global device → onboarding
        # Messages on other device IDs → customer AI (dynamic replies)
        if device_id != current_onboarding_device:
            # Look up the business that owns this WhatsApp session
            business = db.get_business_by_wa_session_id(device_id)
            biz_id = business["id"] if business else None
            logger.info(
                "[WEBHOOK-ROUTE] biz-device path | device=%r biz=%r owner_check_phone=%s",
                device_id, biz_id, phone,
            )
            if business:
                # Check if the sender is the business owner / admin
                if is_owner_message(business, phone):
                    print(
                        f"[OWNER-CMD] device={device_id} owner_phone={phone} "
                        f"msg_id={message_id!r} body={body[:60]!r}"
                    )
                    # ── Owner KB confirmation (YES KB-XXXX / NO KB-XXXX) ──────
                    # Must run BEFORE the referral handler because referral's
                    # "NO" regex matches any message starting with "no" and
                    # would otherwise swallow "NO KB-XXXX". The KB matcher is
                    # strict (verdict + literal "kb-" + code) so anything that
                    # isn't a KB reply cleanly falls through.
                    from app.services.knowledge_base_service import handle_owner_kb_reply
                    kb_handled = await handle_owner_kb_reply(
                        business=business,
                        owner_phone=phone,
                        body=body,
                        device_id=device_id,
                    )
                    if kb_handled:
                        _log_event(
                            "processed",
                            phone=phone,
                            message_id=message_id,
                            detail="owner KB confirmation reply",
                        )
                        return

                    # ── Owner referral confirmation (YES 1234 / NO) ───────────
                    # Runs after KB so bare "NO" still reaches the referral
                    # handler. Must run before the general owner command
                    # parser so these replies are never routed as unknown
                    # commands.
                    from app.services.referral_service import handle_owner_referral_reply
                    referral_handled = await handle_owner_referral_reply(
                        business=business,
                        owner_phone=phone,
                        body=body,
                        device_id=device_id,
                    )
                    if referral_handled:
                        _log_event(
                            "processed",
                            phone=phone,
                            message_id=message_id,
                            detail="owner referral confirmation reply",
                        )
                        return

                    await handle_owner_command(
                        business=business,
                        message=body,
                        device_id=device_id,
                        owner_phone=phone,
                    )
                    _log_event(
                        "processed",
                        phone=phone,
                        message_id=message_id,
                        detail=f"owner command reply via device {device_id!r}",
                    )
                else:
                    # Respect autoReply flag — if disabled, silently skip AI response
                    logger.debug("Business autoReply setting: %s", business.get('autoReply'))
                    print(
                        f"[CUSTOMER-MSG] device={device_id} customer_phone={phone} "
                        f"biz={biz_id} msg_id={message_id!r} body={(body or '<audio>')[:60]!r}"
                    )
                    if business.get("autoReply") is False:
                        logger.info("Auto-reply disabled for business %s, skipping AI", business.get("id"))
                        _log_event(
                            "skipped",
                            phone=phone,
                            message_id=message_id,
                            detail="autoReply disabled",
                        )
                    else:
                        # ── Subscription / trial gate ─────────────────────────
                        # Block AI replies for businesses whose trial has expired
                        # and that have not yet subscribed to a plan.
                        from app.services.billing.feature_gate import can_access_feature
                        # Safety: only enforce for businesses that explicitly have a
                        # known billing/onboarding state. Legacy docs with no plan field
                        # should continue operating until migrated.
                        _known_states = {
                            "onboarding", "trial", "trialing", "expired", "past_due",
                            "cancelled", "starter", "pro", "active",
                        }
                        _plan_raw = str(business.get("plan") or "").lower()
                        if _plan_raw in _known_states and not can_access_feature(business, "ai_receptionist"):
                            logger.info(
                                "[BILLING-GATE] AI blocked for expired/unsubscribed business=%s "
                                "(phone=%s) — skipping AI reply",
                                biz_id, phone,
                            )
                            _log_event(
                                "skipped",
                                phone=phone,
                                message_id=message_id,
                                detail=f"AI gated — business {biz_id} has no active plan",
                            )
                            return

                        # ── CSAT rating reply (must run before AI / referral) ──
                        # If we asked the customer to rate (1-5) and they did,
                        # consume the digit here so the AI never sees it as a
                        # booking-time message ("send 5 people at 7pm").
                        try:
                            from app.services.csat_service import handle_csat_reply
                            csat_handled = await handle_csat_reply(
                                business=business,
                                customer_phone=phone,
                                body=body or "",
                            )
                        except Exception as exc:
                            logger.warning("[CSAT] reply handler error: %s", exc)
                            csat_handled = False
                        if csat_handled:
                            _log_event(
                                "processed",
                                phone=phone,
                                message_id=message_id,
                                detail="CSAT rating reply handled",
                            )
                            return

                        # ── Visit confirmation handler (highest priority) ──────
                        # Must run before referral detection and AI routing so
                        # YES/NO replies are never consumed by the AI chatbot.
                        from app.services.visit_service import handle_visit_reply
                        visit_handled = await handle_visit_reply(
                            business_id=business["id"],
                            customer_phone=phone,
                            body=body,
                            business=business,
                        )
                        if visit_handled:
                            _log_event(
                                "processed",
                                phone=phone,
                                message_id=message_id,
                                detail="visit confirmation reply handled",
                            )
                            return

                        # ── Referral code detection (runs before AI routing) ──
                        from app.services.referral_service import detect_referral_code, handle_referral_message
                        referral_code = detect_referral_code(body)
                        if referral_code:
                            logger.info(
                                "[REFERRAL] Detected code %r in message from %s (biz=%s)",
                                referral_code, phone, business.get("id"),
                            )
                            handled = await handle_referral_message(
                                business_id=business["id"],
                                sender_phone=phone,
                                referral_code=referral_code,
                                business=business,
                            )
                            if handled:
                                _log_event(
                                    "processed",
                                    phone=phone,
                                    message_id=message_id,
                                    detail=f"referral code={referral_code!r} handled",
                                )
                                return
                            # Code not found — fall through to normal AI routing

                        # Route through AI for dynamic, intent-aware responses
                        import time
                        customer_ai_start = time.time()
                        if is_audio:
                            logger.info(
                                "[AUDIO] Routing audio message from %s (device=%r, mime=%r)",
                                phone,
                                device_id,
                                mime_type,
                            )
                            await _customer_ai.handle_audio_message(
                                business=business,
                                customer_phone=phone,
                                media_url=media_url,
                                mime_type=mime_type,
                                push_name=push_name,
                                device_id=device_id,
                            )
                            duration = time.time() - customer_ai_start
                            logger.info("[LATENCY] Customer AI Audio processing for phone=%s msg_id=%s took %.3fs", phone, message_id, duration)
                            _log_event(
                                "processed",
                                phone=phone,
                                message_id=message_id,
                                detail=f"audio AI reply via device {device_id!r}",
                            )
                        else:
                            await _customer_ai.handle_customer_message(
                                business=business,
                                customer_phone=phone,
                                body=body,
                                push_name=push_name,
                                device_id=device_id,
                                message_id=message_id,
                            )
                            duration = time.time() - customer_ai_start
                            logger.info("[LATENCY] Customer AI Text processing for phone=%s msg_id=%s took %.3fs", phone, message_id, duration)
                            _log_event(
                                "processed",
                                phone=phone,
                                message_id=message_id,
                                detail=f"customer AI reply via device {device_id!r}",
                            )
            else:
                # No business linked to this device — stay silent.
                # The biz-* device may still be used by the owner for personal
                # chats (shared WhatsApp account), so sending any auto-reply
                # would leak into private conversations with friends/family.
                logger.warning(
                    "No business found for device_id %s — skipping auto-reply (chat=%s)",
                    device_id, phone,
                )
                _log_event(
                    "processed",
                    phone=phone,
                    message_id=message_id,
                    detail=f"no business linked to device {device_id!r} — silent skip",
                )
            return

        # Onboarding device only handles text and location shares — silently skip other audio/media
        if is_audio:
            logger.debug("[SKIP] audio message on onboarding device (text-only)")
            _log_event("skipped", phone=phone, message_id=message_id, detail="audio on onboarding device")
            return

        logger.info("[ONBOARDING-PROCESS] phone=%s push_name=%r msg_id=%r body=%r", phone, push_name, message_id, body[:100])
        import time
        onboarding_start = time.time()

        try:
            await _onboarding.handle_message(phone, body, push_name, message_id, message_type=message_type)
            duration = time.time() - onboarding_start
            logger.info("[LATENCY] Onboarding processing for phone=%s msg_id=%s took %.3fs", phone, message_id, duration)
            logger.info("[ONBOARDING-RESPONSE] Sent reply for phone=%s msg_id=%r", phone, message_id)
        except TypeError:
            # Backward compatibility for monkeypatched/test doubles that still
            # implement the older 4-argument signature.
            await _onboarding.handle_message(phone, body, push_name, message_id)
            duration = time.time() - onboarding_start
            logger.info("[LATENCY] Onboarding processing (legacy) for phone=%s msg_id=%s took %.3fs", phone, message_id, duration)
            logger.info("[ONBOARDING-RESPONSE] Sent reply (legacy) for phone=%s msg_id=%r", phone, message_id)
        
        _log_event("processed", phone=phone, message_id=message_id, detail=f"body={body[:80]!r}")

    except Exception as exc:
        phone = payload.get("payload", {}).get("chat_id", "?")
        message_id = payload.get("payload", {}).get("message_id", "")
        tb = traceback.format_exc()
        _log_event("error", phone=phone, message_id=message_id, error=f"{exc}\n{tb}")
        logger.exception("Error processing onboarding message from %s", phone)


@router.post("/whatsmeow-webhook")
async def whatsmeow_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
) -> dict:
    """Receive webhook POST from the whatsmeow bridge.

    Returns 200 immediately; actual processing runs as a background task
    so the bridge doesn't time out waiting for Claude / Firestore calls.
    """
    
    # ── secret validation ─────────────────────────────────────────────
    expected = settings.WEBHOOK_SECRET or settings.X_WEBHOOK_SECRET
    if expected and x_webhook_secret != expected:
        logger.warning("Webhook secret mismatch (got %s)", x_webhook_secret)
        return Response(status_code=401, content="Unauthorized")

    try:
        payload: dict = await request.json()
    except Exception as e:
        logger.warning("[Webhook] Failed to parse JSON: %s", e)
        return Response(status_code=400, content="Invalid JSON")

    # ── REQUEST LOG ─────────────────────────────────────────────────────
    _data = payload.get("payload", {})
    logger.info(
        "[REQUEST] event=%r device=%r from=%r chat=%r body=%r msg_id=%r",
        payload.get("event"),
        payload.get("device_id"),
        _data.get("from", ""),
        _data.get("chat_id", ""),
        (_data.get("body") or "")[:100],
        _data.get("message_id", ""),
    )

    background_tasks.add_task(_process_webhook, payload)
    
    # ── RESPONSE LOG ────────────────────────────────────────────────────
    response = {"status": "ok"}
    logger.info("[RESPONSE] Webhook accepted | status=%r", response.get("status"))
    return response


@router.get("/whatsmeow-webhook/health")
async def webhook_health() -> dict:
    """Simple liveness probe for the webhook receiver."""
    return {"status": "ok", "service": "whatsapp-webhook"}


@router.get("/whatsmeow-webhook/debug")
async def webhook_debug() -> dict:
    """Return the last 50 webhook processing attempts (errors, skips, successes).

    Use this to diagnose why a message was not delivered to WhatsApp.
    """
    return {"total": len(_events), "events": list(_events)}
