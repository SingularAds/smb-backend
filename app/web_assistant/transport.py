"""Thin async wrapper around the Chatwoot REST API.

Only the three operations the orchestrator needs are exposed:
    * send_message       — public message visible to the visitor
    * send_private_note  — internal note visible only to agents
    * assign_team        — route the conversation to a human team

All other Chatwoot API surface is intentionally absent. Adding new
operations is one-method additions; we do not generalise prematurely.

Auth: the agent/bot access token is sent via ``api_access_token`` header
as documented by Chatwoot. Errors are logged and swallowed at this layer —
the orchestrator decides how to react (best-effort delivery; the visitor
should never see a backend failure).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 10.0


def _api_base() -> str:
    base = (settings.CHATWOOT_BASE_URL or "").rstrip("/")
    account = (settings.CHATWOOT_ACCOUNT_ID or "").strip()
    if not base or not account:
        raise RuntimeError(
            "Chatwoot is not configured — set CHATWOOT_BASE_URL and CHATWOOT_ACCOUNT_ID"
        )
    return f"{base}/api/v1/accounts/{account}"


def _headers() -> dict[str, str]:
    token = (settings.CHATWOOT_ACCESS_TOKEN or "").strip()
    if not token:
        raise RuntimeError("CHATWOOT_ACCESS_TOKEN is not set")
    return {
        "api_access_token": token,
        "Content-Type": "application/json",
    }


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Issue a POST to ``{api_base}{path}`` with sensible error handling."""
    url = f"{_api_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S) as client:
            resp = await client.post(url, headers=_headers(), json=payload)
    except httpx.HTTPError as exc:
        logger.warning("[CHATWOOT] POST %s network error: %s", url, exc)
        return None

    if resp.status_code >= 400:
        # Truncate response body so a giant HTML error page doesn't flood logs.
        body = (resp.text or "")[:500]
        logger.warning(
            "[CHATWOOT] POST %s failed status=%s body=%s",
            url, resp.status_code, body,
        )
        return None

    try:
        return resp.json()
    except ValueError:
        return None


async def send_message(conversation_id: str | int, text: str) -> int | None:
    """Send a public outgoing message to the visitor.

    Returns the Chatwoot message ID (int) on success, None on failure.
    Callers must register the returned ID with store.register_bot_message()
    so that the webhook echo of this message is not misidentified as a
    human-agent reply.
    """
    if not text or not text.strip():
        logger.info("[CHATWOOT] send_message skipped — empty text conv=%s", conversation_id)
        return None

    result = await _post(
        f"/conversations/{conversation_id}/messages",
        {
            "content": text,
            "message_type": "outgoing",
            "private": False,
        },
    )
    if result is None:
        return None
    msg_id = result.get("id")
    logger.info(
        "[CHATWOOT] Sent reply conv=%s len=%d msg_id=%s",
        conversation_id, len(text), msg_id,
    )
    return int(msg_id) if msg_id else None


async def send_private_note(conversation_id: str | int, text: str) -> int | None:
    """Send an internal note (only agents can see it).

    Returns the Chatwoot message ID on success, None on failure.
    """
    if not text or not text.strip():
        return None

    result = await _post(
        f"/conversations/{conversation_id}/messages",
        {
            "content": text,
            "message_type": "outgoing",
            "private": True,
        },
    )
    if result is None:
        return None
    msg_id = result.get("id")
    if msg_id:
        logger.info(
            "[CHATWOOT] Sent private note conv=%s msg_id=%s",
            conversation_id, msg_id,
        )
    return int(msg_id) if msg_id else None


async def assign_team(conversation_id: str | int, team_id: int) -> bool:
    """Assign the conversation to a Chatwoot team for human handoff."""
    if not team_id:
        # Treat 0 / unset as "no team configured" — silently succeed so the
        # orchestrator can still send its private note and public escalation
        # message even in setups without a support team.
        logger.info(
            "[CHATWOOT] assign_team skipped — CHATWOOT_SUPPORT_TEAM_ID not set"
        )
        return True

    result = await _post(
        f"/conversations/{conversation_id}/assignments",
        {"team_id": int(team_id)},
    )
    ok = result is not None
    if ok:
        logger.info(
            "[CHATWOOT] Assigned conv=%s to team=%s", conversation_id, team_id
        )
    return ok


async def toggle_status(
    conversation_id: str | int, status: str = "open"
) -> bool:
    """Reopen / pend / resolve a conversation (used when AI re-takes over)."""
    result = await _post(
        f"/conversations/{conversation_id}/toggle_status",
        {"status": status},
    )
    return result is not None
