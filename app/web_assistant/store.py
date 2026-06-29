"""Firestore-backed conversation store for the website assistant.

One Firestore document per Chatwoot conversation lives at
``web_conversations/{conversation_id}``. The store is intentionally minimal:
it persists exactly what the orchestrator needs across requests and nothing
more. Higher-level decisions (language detection, escalation labelling,
prompt assembly) live in ``service.py``.

Document schema::

    {
      "conversationId":   str,
      "inboxId":          int,
      "visitorName":      str,
      "language":         str,                # locked on first substantial msg
      "aiPaused":         bool,               # True after a human agent replies
      "takeoverHintShown": bool,              # True once the post-takeover guide
                                             #   note has been delivered — prevents
                                             #   re-sending it after every pause/resume
                                             #   cycle (the agent only needs to be
                                             #   onboarded once per conversation)
      "botMessageIds":    [str],              # Chatwoot IDs of bot-sent messages;
                                             #   used to suppress webhook echoes that
                                             #   otherwise look like human-agent replies
      "lastEscalation":   {                   # set when AI returns [ESCALATE]
          "question":     str,
          "messageId":    str | None,
          "timestamp":    str (iso)
      } | None,
      "messages":         [                   # capped at settings.CHATWOOT_HISTORY_LIMIT
          {"role": "user"|"assistant", "content": str, "ts": str (iso)}
      ],
      "createdAt":        str (iso),
      "updatedAt":        str (iso)
    }
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(conversation_id: str) -> dict[str, Any]:
    """Return the conversation document, creating an empty shell if missing."""
    import app.firestore as db

    doc = db.get_web_conversation(conversation_id)
    if doc is None:
        return {
            "conversationId": str(conversation_id),
            "messages": [],
            "aiPaused": False,
            "takeoverHintShown": False,
            "language": None,
            "visitorName": None,
            "inboxId": None,
            "lastEscalation": None,
            "botMessageIds": [],
            "pendingEscalations": [],
        }
    # Defensive defaults — Firestore may return partial docs from older writes.
    doc.setdefault("messages", [])
    doc.setdefault("aiPaused", False)
    doc.setdefault("takeoverHintShown", False)
    doc.setdefault("language", None)
    doc.setdefault("visitorName", None)
    doc.setdefault("inboxId", None)
    doc.setdefault("lastEscalation", None)
    doc.setdefault("botMessageIds", [])
    doc.setdefault("pendingEscalations", [])
    return doc


def save(conversation_id: str, doc: dict[str, Any]) -> None:
    """Persist the conversation document (merge=True semantics)."""
    import app.firestore as db

    db.save_web_conversation(conversation_id, doc)


def append_message(doc: dict[str, Any], role: str, content: str) -> None:
    """Mutate ``doc['messages']`` in place: append + cap history."""
    if not content:
        return
    messages: list[dict[str, Any]] = doc.setdefault("messages", [])
    messages.append({"role": role, "content": content, "ts": _now_iso()})
    cap = max(1, int(settings.CHATWOOT_HISTORY_LIMIT))
    if len(messages) > cap:
        # Keep newest `cap` messages; drop the oldest.
        del messages[: len(messages) - cap]


def mark_ai_paused(doc: dict[str, Any], paused: bool) -> None:
    doc["aiPaused"] = bool(paused)


_BOT_MSG_IDS_CAP = 100  # max IDs retained per conversation; prevents unbounded growth


def register_bot_message(doc: dict[str, Any], message_id: int | str | None) -> None:
    """Record a Chatwoot message ID that was sent by this bot.

    Must be called immediately after every transport.send_message() /
    send_private_note() so that _handle_agent_message() can identify and
    suppress the Chatwoot webhook echo that would otherwise look identical
    to a genuine human-agent reply.

    Idempotent: registering the same ID twice is a no-op.
    Bounded: oldest IDs are evicted once the cap is reached.
    """
    if not message_id:
        return
    ids: list[str] = doc.setdefault("botMessageIds", [])
    key = str(message_id)
    if key in ids:
        return
    ids.append(key)
    if len(ids) > _BOT_MSG_IDS_CAP:
        del ids[: len(ids) - _BOT_MSG_IDS_CAP]


def is_bot_message(doc: dict[str, Any], message_id: int | str | None) -> bool:
    """Return True when ``message_id`` belongs to a message sent by this bot.

    Called in service._handle_agent_message() before the human-takeover
    logic to distinguish bot webhook echoes from real human replies.
    """
    if not message_id:
        return False
    return str(message_id) in (doc.get("botMessageIds") or [])


_PENDING_ESC_CAP = 20  # max pending escalations retained per conversation


def add_pending_escalation(
    doc: dict[str, Any], esc_id: str, question: str, message_id: str | None
) -> None:
    """Record a new unanswered escalation so agents can later /kb-learn <id>."""
    escalations: list = doc.setdefault("pendingEscalations", [])
    escalations.append({
        "id": esc_id,
        "question": question,
        "messageId": message_id,
        "timestamp": _now_iso(),
    })
    if len(escalations) > _PENDING_ESC_CAP:
        del escalations[: len(escalations) - _PENDING_ESC_CAP]


def pop_pending_escalation(doc: dict[str, Any], esc_id: str) -> dict[str, Any] | None:
    """Remove and return the escalation with ``esc_id``, or None if not found."""
    escalations: list = doc.get("pendingEscalations") or []
    for i, esc in enumerate(escalations):
        if esc.get("id") == esc_id:
            escalations.pop(i)
            doc["pendingEscalations"] = escalations
            return esc
    return None


def last_assistant_public_reply(doc: dict[str, Any]) -> str | None:
    """Most recent assistant message we stored (used by /kb-learn lookup)."""
    for msg in reversed(doc.get("messages") or []):
        if msg.get("role") == "assistant" and (msg.get("content") or "").strip():
            return msg["content"]
    return None


def last_visitor_question(doc: dict[str, Any]) -> str | None:
    """Most recent visitor (``user``) message we stored.

    Used by /kb-learn when the conversation never went through the explicit
    [ESCALATE] path — e.g. the AI answered (perhaps incorrectly), a human
    corrected, and now wants to save the corrected Q&A. The most recent
    visitor message is the right question to associate with the agent's
    reply.
    """
    for msg in reversed(doc.get("messages") or []):
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            return msg["content"]
    return None
