"""Human-approved Q&A learnings for the website assistant.

A "learning" is a single Q&A pair that a human agent approved via the
``/kb-learn`` command after the AI escalated a question it could not
answer. Learnings live in Firestore at
``system/web_assistant_kb/learnings/{auto_id}`` and are injected into the
LLM prompt next to the curated Global KB.

Two reasons learnings are stored separately from ``system/recepte_kb``:
  1. Crowd-sourced content should never silently leak into the WhatsApp
     customer AI pipeline (which reads the curated KB only).
  2. Curated KB is hand-edited by humans; learnings grow automatically
     and need their own lifecycle (dedup, expiry, ranking later).

This module owns a small in-process TTL cache so we hit Firestore at most
once every ``CHATWOOT_LEARNINGS_CACHE_TTL_S`` seconds, even under load.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


# ── In-process cache ────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache_entries: list[dict[str, Any]] | None = None
_cache_ts: float = 0.0


def _approx_token_count(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def normalize_question(text: str) -> str:
    """Return a deterministic, case/whitespace-insensitive form for dedup."""
    if not text:
        return ""
    # Lowercase, collapse whitespace, strip punctuation at ends.
    cleaned = re.sub(r"\s+", " ", text.strip().lower())
    cleaned = cleaned.strip(" .?!:;,\"'")
    return cleaned


def question_hash(text: str) -> str:
    """Stable SHA-1 of the normalized question (16 hex chars is plenty)."""
    norm = normalize_question(text)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _load_from_firestore() -> list[dict[str, Any]]:
    import app.firestore as db

    limit = max(1, int(settings.CHATWOOT_LEARNINGS_LIMIT))
    return db.list_web_learnings(limit=limit)


def get_learnings(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return cached learnings; refresh when stale or on demand."""
    global _cache_entries, _cache_ts

    ttl = max(1, int(settings.CHATWOOT_LEARNINGS_CACHE_TTL_S))
    now = time.monotonic()

    with _cache_lock:
        fresh = (
            not force_refresh
            and _cache_entries is not None
            and (now - _cache_ts) < ttl
        )
        if fresh:
            return list(_cache_entries or [])

    # Load outside the lock — Firestore call can block.
    try:
        entries = _load_from_firestore()
    except Exception as exc:
        logger.warning("[WEB-LEARN] Firestore load failed, serving last cache: %s", exc)
        with _cache_lock:
            return list(_cache_entries or [])

    with _cache_lock:
        _cache_entries = entries
        _cache_ts = now
    logger.info(
        "[WEB-LEARN] Loaded %d learnings from Firestore (approx_tokens=%d)",
        len(entries),
        _approx_token_count("".join(f"{e.get('question','')}{e.get('answer','')}" for e in entries)),
    )
    return list(entries)


def invalidate_cache() -> None:
    """Force the next `get_learnings()` call to reload from Firestore."""
    global _cache_entries, _cache_ts
    with _cache_lock:
        _cache_entries = None
        _cache_ts = 0.0


def save_learning(
    *,
    question: str,
    answer: str,
    agent_name: str | None,
    conversation_id: str,
    language: str | None,
) -> dict[str, Any]:
    """Persist a new learning; dedup by question hash (idempotent)."""
    import app.firestore as db

    q = (question or "").strip()
    a = (answer or "").strip()
    if not q or not a:
        raise ValueError("question and answer are both required")

    qhash = question_hash(q)
    existing = db.find_web_learning_by_question_hash(qhash)
    if existing:
        logger.info(
            "[WEB-LEARN] Skipping save — duplicate question hash=%s (existing id=%s)",
            qhash,
            existing.get("id"),
        )
        return existing

    payload = {
        "question": q,
        "answer": a,
        "questionHash": qhash,
        "agentName": agent_name or "",
        "conversationId": str(conversation_id),
        "language": language or "",
    }
    saved = db.add_web_learning(payload)
    invalidate_cache()
    logger.info(
        "[WEB-LEARN] Saved new learning id=%s agent=%s conv=%s hash=%s",
        saved.get("id"),
        agent_name,
        conversation_id,
        qhash,
    )
    return saved


def format_for_prompt(entries: list[dict[str, Any]]) -> str:
    """Render learnings as a single block for prompt injection.

    Returns an empty string when there are no learnings — callers can drop
    the whole section unconditionally without checking length.
    """
    if not entries:
        return ""
    lines: list[str] = [
        "--- LEARNED FROM PREVIOUS CONVERSATIONS (human-approved) ---",
    ]
    for e in entries:
        q = (e.get("question") or "").strip()
        a = (e.get("answer") or "").strip()
        if not q or not a:
            continue
        lines.append(f"Q: {q}")
        lines.append(f"A: {a}")
        lines.append("")  # blank separator
    lines.append("--- END LEARNED CONTENT ---")
    return "\n".join(lines)
