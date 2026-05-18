"""Webhooks Router — Stripe payment event handling.

All state changes flow through webhooks, never assumed from client redirects.
Stripe signature is verified on every request before any processing occurs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request, Response
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None),
) -> dict:
    """Receive and process a Stripe webhook event.

    Steps:
    1. Read raw body (required for HMAC verification — must not parse JSON first)
    2. Verify webhook signature against STRIPE_WEBHOOK_SECRET
    3. Route verified event to the correct handler
    4. Return {"received": True} so Stripe marks the delivery successful

    On signature failure we return 400 so Stripe retries the delivery.
    """
    from app.services.billing.stripe_service import verify_webhook, handle_webhook_event

    payload = await request.body()

    if not stripe_signature:
        logger.warning("[STRIPE] Webhook received without Stripe-Signature header")
        return Response(
            content='{"error":"Missing Stripe-Signature header"}',
            status_code=400,
            media_type="application/json",
        )

    event = verify_webhook(payload, stripe_signature)
    if event is None:
        logger.warning("[STRIPE] Webhook signature verification failed")
        return Response(
            content='{"error":"Invalid signature"}',
            status_code=400,
            media_type="application/json",
        )

    event_type: str = event.get("type") or ""
    logger.info("[STRIPE] Webhook received: %s id=%s", event_type, event.get("id"))

    try:
        result = handle_webhook_event(event)
        logger.info("[STRIPE] Webhook handled: %s → %s", event_type, result)
    except Exception as exc:
        logger.exception("[STRIPE] Webhook handler error for event=%s: %s", event_type, exc)
        # Return 200 so Stripe doesn't retry — the error is logged for manual review.
        # A 500 would cause Stripe to retry and potentially trigger the handler again.
        return {"received": True, "error": "handler_error"}

    return {"received": True, "result": result}
