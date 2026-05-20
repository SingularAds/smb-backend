"""Thin Telegram Bot API client for alerting Daniel (human escalation)."""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def send_message(text: str) -> bool:
    """Send a plain-text (or HTML) message to Daniel's Telegram chat.

    Returns True on success, False on failure.  Failures are logged but
    never raised so a Telegram outage never breaks the main flow.
    """
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_DANIEL_CHAT_ID

    if not token or not chat_id:
        logger.warning(
            "[TELEGRAM] TELEGRAM_BOT_TOKEN or TELEGRAM_DANIEL_CHAT_ID not set — alert skipped"
        )
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
            )
        if resp.status_code == 200:
            logger.info("[TELEGRAM] Alert sent to Daniel (chat_id=%s)", chat_id)
            return True
        logger.warning(
            "[TELEGRAM] Alert failed: HTTP %s — %s", resp.status_code, resp.text[:200]
        )
        return False
    except Exception as exc:
        logger.error("[TELEGRAM] Alert exception: %s", exc)
        return False
