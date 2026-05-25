"""Webhooks Router — Stripe payment event handling.

All state changes flow through webhooks, never assumed from client redirects.
Stripe signature is verified on every request before any processing occurs.
"""

from __future__ import annotations

import asyncio
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

    # ── Post-payment owner notification ──────────────────────────────────────
    # Fire-and-forget: send the owner a WhatsApp message confirming their plan
    # is now active.  We do this AFTER returning to Stripe so the 200 response
    # is never delayed by WhatsApp delivery latency.
    if event_type == "checkout.session.completed" and result == "activated":
        asyncio.ensure_future(_notify_owner_plan_activated(event))

    return {"received": True, "result": result}


async def _notify_owner_plan_activated(event: dict) -> None:
    """Send the business owner a WhatsApp payment-success confirmation.

    Called as a fire-and-forget task after checkout.session.completed is
    processed.  Failures are logged but never raise so they cannot affect the
    Stripe webhook acknowledgement.
    """
    try:
        import app.firestore as db
        from app.services.automation.whatsapp_notifier import send_to_owner

        session: dict = (event.get("data") or {}).get("object") or {}
        meta: dict = session.get("metadata") or {}
        business_id: str = meta.get("businessId") or ""
        plan: str = meta.get("plan") or "starter"

        if not business_id:
            logger.warning("[STRIPE] _notify_owner_plan_activated: no businessId in metadata")
            return

        business = db.get_business_by_id(business_id)
        if not business:
            logger.warning("[STRIPE] _notify_owner_plan_activated: business %s not found", business_id)
            return

        biz_name = business.get("name") or "your business"
        plan_label = plan.title()

        msg = (
            f"✅ *Payment confirmed — {biz_name}*\n\n"
            f"Your *Recepte {plan_label}* subscription is now *active*.\n\n"
            f"Your AI receptionist has been reactivated and is ready to take "
            f"bookings and answer customers again. 🎉\n\n"
            f"Thank you for choosing Recepte!"
        )

        sent = await send_to_owner(business, msg)
        if sent:
            logger.info(
                "[STRIPE] Payment confirmation WhatsApp sent to owner of business=%s plan=%s",
                business_id, plan,
            )
        else:
            logger.warning(
                "[STRIPE] Payment confirmation WhatsApp NOT sent for business=%s — no ownerPhone?",
                business_id,
            )
    except Exception as exc:
        logger.exception("[STRIPE] _notify_owner_plan_activated failed: %s", exc)

