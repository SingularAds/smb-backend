"""Append-only archive of the full onboarding conversation.

Why this exists: ``onboarding_sessions.{phone}.conversationHistory`` is only
appended by the conversational AI handlers — the structured steps (pairing,
trust interstitial, referral confirm, billing, …) send messages without
persisting them, and the post-onboarding handler used to overwrite the array
with a trimmed slice. The dashboard therefore never had the full conversation.

This module records EVERY inbound owner message and EVERY outbound Sofia
message from two central hooks (``handle_message`` and ``_send``), independent
of which handler processed the turn.

Storage:
    onboarding_transcripts/{phone}                  — parent doc (ownerPhone, updatedAt)
    onboarding_transcripts/{phone}/messages/{auto}  — {role, content, ts, step,
                                                       kind, messageId, delivered}

``ts`` is a strictly-monotonic ISO timestamp (process-local): coarse clocks
can stamp two turns identically, which breaks order_by on read. Recording is
fail-safe — a Firestore hiccup here must never break the live conversation.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from app import firestore as db

logger = logging.getLogger(__name__)

_COLLECTION = "onboarding_transcripts"

_ts_lock = threading.Lock()
_last_ts: datetime | None = None


def _archive_ts() -> str:
    """Strictly-monotonic UTC ISO timestamp (ties bumped by 1µs)."""
    global _last_ts
    with _ts_lock:
        now = datetime.utcnow()
        if _last_ts is not None and now <= _last_ts:
            now = _last_ts + timedelta(microseconds=1)
        _last_ts = now
        return now.isoformat()


def record_message(
    phone: str,
    role: str,
    content: str,
    *,
    step: str | None = None,
    kind: str = "text",
    message_id: str | None = None,
    delivered: bool = True,
) -> None:
    """Append one turn to the archive. Never raises."""
    try:
        phone_clean = db._clean_phone(phone)
        if not phone_clean or not content:
            return
        parent = db._db().collection(_COLLECTION).document(phone_clean)
        ts = _archive_ts()
        entry = {
            "role": role,
            "content": str(content),
            "ts": ts,
            "step": step or None,
            "kind": kind,
            "messageId": message_id or None,
            "delivered": delivered,
        }
        parent.set({"ownerPhone": phone_clean, "updatedAt": ts}, merge=True)
        parent.collection("messages").add(entry)
    except Exception as exc:
        logger.error("[TRANSCRIPT] failed to record %s turn for %s: %s", role, phone, exc)


def list_transcript(phone: str, limit: int = 1500) -> list[dict]:
    """Full archived conversation, oldest first. Returns [] on any failure."""
    try:
        phone_clean = db._clean_phone(phone)
        if not phone_clean:
            return []
        docs = (
            db._db()
            .collection(_COLLECTION)
            .document(phone_clean)
            .collection("messages")
            .order_by("ts")
            .limit(limit)
            .stream()
        )
        return [d.to_dict() or {} for d in docs]
    except Exception as exc:
        logger.error("[TRANSCRIPT] failed to list transcript for %s: %s", phone, exc)
        return []
