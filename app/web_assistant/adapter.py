"""Translate Chatwoot webhook payloads into ``InboundChatMessage``.

Chatwoot delivers different payload shapes per event type. This module
contains every assumption about that JSON in one place. The rest of the
pipeline never imports Chatwoot-shaped data.

Reference (Chatwoot Webhooks docs):
    * ``message_created``       — full message object + nested conversation + sender
    * ``conversation_created``  — conversation object with no message yet

We drop the event when:
    * the event type is not one we care about
    * the message is outbound from an AgentBot (our own previous reply)
    * required fields are missing
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.web_assistant.types import InboundChatMessage, SenderType, WebhookEvent

logger = logging.getLogger(__name__)


# Map Chatwoot ``sender.type`` (or ``message.sender_type``) → our enum.
_SENDER_TYPE_MAP = {
    "Contact": SenderType.VISITOR,      # website visitor
    "contact": SenderType.VISITOR,
    "User": SenderType.AGENT,           # human agent (Recepte team)
    "user": SenderType.AGENT,
    "AgentBot": SenderType.BOT,
    "agent_bot": SenderType.BOT,
}


def _coerce_sender_type(raw: str | None) -> SenderType:
    if not raw:
        return SenderType.SYSTEM
    return _SENDER_TYPE_MAP.get(raw, SenderType.SYSTEM)


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _extract_browser_language(payload: dict) -> str | None:
    """Best-effort extraction of the visitor's browser language."""
    conv = payload.get("conversation") or {}
    meta = conv.get("meta") or {}
    sender = meta.get("sender") or {}
    add_attrs = sender.get("additional_attributes") or {}
    lang = (
        add_attrs.get("browser_language")
        or add_attrs.get("language")
        or (sender.get("custom_attributes") or {}).get("language")
    )
    return _safe_str(lang).strip() or None


def _extract_visitor_name(payload: dict) -> str | None:
    conv = payload.get("conversation") or {}
    sender = (conv.get("meta") or {}).get("sender") or {}
    name = sender.get("name") or sender.get("email") or None
    return _safe_str(name).strip() or None


def parse_event(payload: dict) -> InboundChatMessage | None:
    """Convert a raw Chatwoot webhook payload into our internal type.

    Returns ``None`` when the event is irrelevant or malformed; the caller
    treats that as "drop silently".
    """
    if not isinstance(payload, dict):
        return None

    event_raw = (payload.get("event") or payload.get("type") or "").strip()
    try:
        event = WebhookEvent(event_raw)
    except ValueError:
        logger.debug("[CHATWOOT] Ignoring unsupported event=%s", event_raw)
        return None

    # --- common fields ------------------------------------------------------
    account = payload.get("account") or {}
    account_id = _safe_str(account.get("id") or payload.get("account_id"))

    conv = payload.get("conversation") or {}
    conversation_id = _safe_str(
        conv.get("id")
        or payload.get("conversation_id")
        or payload.get("id")  # conversation_created delivers conv at root
    )
    if not conversation_id:
        logger.warning("[CHATWOOT] Dropping event=%s — missing conversation id", event_raw)
        return None

    inbox_id_raw = (
        payload.get("inbox_id")
        or conv.get("inbox_id")
        or (conv.get("meta") or {}).get("inbox_id")
        or (payload.get("inbox") or {}).get("id")
        or 0
    )
    try:
        inbox_id = int(inbox_id_raw) if inbox_id_raw else 0
    except (TypeError, ValueError):
        inbox_id = 0

    visitor_name = _extract_visitor_name(payload)
    browser_lang = _extract_browser_language(payload)

    # --- event-specific shapes ---------------------------------------------
    if event is WebhookEvent.CONVERSATION_CREATED:
        return InboundChatMessage(
            event=event,
            account_id=account_id,
            inbox_id=inbox_id,
            conversation_id=conversation_id,
            message_id=None,
            sender_type=SenderType.SYSTEM,
            sender_name=visitor_name,
            body="",
            is_private_note=False,
            visitor_browser_language=browser_lang,
            raw=payload,
        )

    # event == MESSAGE_CREATED
    # Chatwoot delivers message fields either at the top level or nested
    # under ``messages[0]`` depending on integration mode. Handle both.
    msg = payload
    if not msg.get("content") and isinstance(conv.get("messages"), list):
        latest_messages = conv.get("messages") or []
        if latest_messages:
            msg = latest_messages[-1]

    message_id = _safe_str(msg.get("id"))
    body = _safe_str(msg.get("content")).strip()
    is_private = bool(msg.get("private"))

    sender_block = msg.get("sender") or {}
    sender_raw = (
        msg.get("sender_type")
        or sender_block.get("type")
        or msg.get("message_type")
    )
    sender_type = _coerce_sender_type(sender_raw)

    # Chatwoot also sends ``message_type`` as "incoming" | "outgoing" | "template".
    # Outgoing means *we* (or an agent) sent it. Use this as a second signal
    # when ``sender.type`` is missing or ambiguous.
    message_type = _safe_str(msg.get("message_type")).lower()
    if sender_type is SenderType.SYSTEM:
        if message_type == "incoming":
            sender_type = SenderType.VISITOR
        elif message_type == "outgoing":
            # Could be agent or bot — without sender info we cannot tell.
            # Treat as BOT so we never react to our own echoes by accident.
            sender_type = SenderType.BOT

    sender_name = _safe_str(sender_block.get("name")) or visitor_name

    # Drop our own bot echoes immediately — protects against infinite loops.
    if sender_type is SenderType.BOT:
        logger.debug(
            "[CHATWOOT] Dropping bot/outgoing echo conv=%s msg=%s",
            conversation_id, message_id,
        )
        return None

    # Optional inbox safety filter — only respond to the configured website inbox.
    configured_inbox = int(settings.CHATWOOT_WEBSITE_INBOX_ID or 0)
    if configured_inbox and inbox_id and inbox_id != configured_inbox:
        logger.info(
            "[CHATWOOT] Dropping message from non-website inbox=%s (configured=%s)",
            inbox_id, configured_inbox,
        )
        return None

    if not body and not is_private:
        # Empty visitor message (image-only, attachment-only, etc.) — ignore.
        return None

    return InboundChatMessage(
        event=event,
        account_id=account_id,
        inbox_id=inbox_id,
        conversation_id=conversation_id,
        message_id=message_id or None,
        sender_type=sender_type,
        sender_name=sender_name or None,
        body=body,
        is_private_note=is_private,
        visitor_browser_language=browser_lang,
        raw=payload,
    )
