"""FastAPI entrypoint for the Chatwoot website-assistant pipeline.

Single responsibility: receive the webhook, prove it came from Chatwoot,
return 200 fast, hand processing to a background task.

Industry-standard webhook design:
    1. Always respond within a few hundred ms — Chatwoot retries 5xx.
    2. Verify HMAC signature against the raw body BEFORE parsing JSON.
    3. Idempotency by ``message_id`` is enforced upstream in service.py
       (we do not dedup here so unit tests for dispatch stay simple).
    4. Any handler exception is logged but never raised to the caller —
       returning 500 to Chatwoot would trigger duplicate deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ── HMAC verification ───────────────────────────────────────────────────────
# Chatwoot signs webhook payloads with HMAC-SHA256 (hex digest) using the
# webhook secret configured against the webhook integration. The signature
# is delivered via the ``X-Chatwoot-Signature`` header.


def _verify_signature(raw_body: bytes, signature: Optional[str]) -> tuple[bool, str]:
    """Return (ok, reason) so callers can log a precise failure message."""
    secret = (settings.CHATWOOT_HMAC_SECRET or "").strip()

    if not secret:
        # No secret configured locally → treat as "unchecked / always pass".
        return True, "no_secret_configured"

    if not signature:
        return False, "no_signature_header"

    sig = signature.strip()
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if hmac.compare_digest(expected, sig):
        return True, "ok"
    return False, "hash_mismatch"


# ── Route ──────────────────────────────────────────────────────────────────


@router.post("/webhook/chatwoot")
async def chatwoot_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_chatwoot_signature: Optional[str] = Header(None),
) -> Response:
    """Receive a Chatwoot webhook delivery."""
    if not settings.CHATWOOT_ENABLED:
        # Feature flag — silently 200 so Chatwoot stops retrying.
        return Response(status_code=200)

    raw = await request.body()
    sig_ok, sig_reason = _verify_signature(raw, x_chatwoot_signature)
    if not sig_ok:
        require = settings.CHATWOOT_REQUIRE_SIGNATURE
        logger.warning(
            "[CHATWOOT] Webhook signature verification failed: %s (require=%s)",
            sig_reason,
            require,
        )
        if require:
            # Hard reject in production mode — Chatwoot will retry; the
            # failure is visible in logs and in Chatwoot's webhook delivery UI.
            return Response(
                status_code=401,
                content='{"error":"invalid signature"}',
                media_type="application/json",
            )
        # CHATWOOT_REQUIRE_SIGNATURE=False (staging / dev): accept unsigned
        # deliveries so Chatwoot's test-button and local testing work without
        # needing a valid sig header.

    try:
        payload: dict = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        logger.warning("[CHATWOOT] Webhook body is not valid JSON")
        return Response(
            status_code=400,
            content='{"error":"invalid json"}',
            media_type="application/json",
        )

    event_type = payload.get("event") or payload.get("type") or "?"
    logger.info(
        "[CHATWOOT] Webhook accepted event=%s conv=%s",
        event_type,
        (payload.get("conversation") or {}).get("id")
        or payload.get("conversation_id")
        or payload.get("id"),
    )

    # Hand off async — never block the webhook response on Firestore/LLM.
    background_tasks.add_task(_dispatch_safely, payload)

    return Response(status_code=200, content='{"ok":true}', media_type="application/json")


async def _dispatch_safely(payload: dict) -> None:
    """Run the handler under a top-level try so we never crash the worker."""
    # Import lazily so test rigs can patch service.handle_event without
    # FastAPI bringing the real module in at import time.
    try:
        from app.web_assistant import adapter, service
    except Exception as exc:  # pragma: no cover — import errors are fatal
        logger.exception("[CHATWOOT] Could not import handler modules: %s", exc)
        return

    try:
        inbound = adapter.parse_event(payload)
    except Exception as exc:
        logger.exception("[CHATWOOT] Adapter failed: %s", exc)
        return

    if inbound is None:
        # Adapter explicitly rejected the event (wrong type, bot echo, etc.)
        return

    try:
        await service.handle_event(inbound)
    except Exception as exc:
        logger.exception(
            "[CHATWOOT] Service handler failed for conv=%s: %s",
            getattr(inbound, "conversation_id", "?"),
            exc,
        )
