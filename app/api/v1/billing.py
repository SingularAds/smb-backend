"""Billing API — plan selection, checkout, and subscription status.

These endpoints are called by the owner-facing frontend (or WhatsApp deep-links)
to initiate plan selection after the 7-day trial ends.

The backend resolves pricing; the frontend never decides the price.
Country, tier, and EUR prices are always read from the business document's
billing snapshot that was set at onboarding time.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/checkout")
async def create_checkout(request: Request) -> dict:
    """Create a Stripe Checkout Session for plan selection.

    Expected JSON body:
    {
        "businessId":    "...",
        "plan":          "starter" | "pro",
        "billingPeriod": "monthly" | "annual",    (optional, default: monthly)
        "successUrl":    "https://...",
        "cancelUrl":     "https://..."
    }

    Returns:
    {
        "checkoutUrl": "https://checkout.stripe.com/..."
    }

    The price is resolved entirely from the business document's billing snapshot.
    No price is accepted from the client.
    """
    from app import firestore as db
    from app.services.billing.stripe_service import create_checkout_session

    try:
        body = await request.json()
    except Exception:
        return Response(
            content='{"error":"Invalid JSON"}',
            status_code=400,
            media_type="application/json",
        )

    business_id: str = body.get("businessId") or ""
    plan: str = str(body.get("plan") or "").lower()
    billing_period: str = str(body.get("billingPeriod") or "monthly").lower()
    success_url: str = body.get("successUrl") or ""
    cancel_url: str = body.get("cancelUrl") or ""

    # Basic input validation at the boundary
    if not business_id:
        return Response(
            content='{"error":"businessId is required"}',
            status_code=400,
            media_type="application/json",
        )
    if plan not in ("starter", "pro"):
        return Response(
            content='{"error":"plan must be starter or pro"}',
            status_code=400,
            media_type="application/json",
        )
    if not success_url or not cancel_url:
        return Response(
            content='{"error":"successUrl and cancelUrl are required"}',
            status_code=400,
            media_type="application/json",
        )

    business = db.get_business_by_id(business_id)
    if not business:
        return Response(
            content='{"error":"Business not found"}',
            status_code=404,
            media_type="application/json",
        )

    checkout_url = create_checkout_session(
        business=business,
        plan=plan,
        success_url=success_url,
        cancel_url=cancel_url,
        billing_period=billing_period,
    )

    if not checkout_url:
        logger.error("[BILLING] Failed to create checkout session for business=%s", business_id)
        return Response(
            content='{"error":"Could not create checkout session"}',
            status_code=500,
            media_type="application/json",
        )

    logger.info(
        "[BILLING] Checkout session created for business=%s plan=%s period=%s",
        business_id, plan, billing_period,
    )
    return {"checkoutUrl": checkout_url}


@router.get("/status/{business_id}")
async def get_billing_status(business_id: str) -> dict:
    """Return the current billing state for a business.

    Response fields:
    - plan:             current plan name (trialing / starter / pro / expired / ...)
    - billingStatus:    raw Stripe-side status (active / past_due / cancelled / ...)
    - billingTier:      pricing tier (T0 / T1 / T2 / ...)
    - billingCountry:   ISO country used for tier resolution
    - starterPriceEur:  monthly Starter price in EUR for this tier
    - proPriceEur:      monthly Pro price in EUR for this tier
    - trialActive:      bool — trial is currently running
    - trialDaysRemaining: int
    - trialEndsAt:      ISO timestamp
    """
    from app import firestore as db
    from app.services.billing.trial_manager import get_trial_status
    from app.services.billing.feature_gate import get_effective_plan

    business = db.get_business_by_id(business_id)
    if not business:
        return Response(
            content='{"error":"Business not found"}',
            status_code=404,
            media_type="application/json",
        )

    trial = get_trial_status(business)
    effective = get_effective_plan(business)

    return {
        "plan":               effective,
        "billingStatus":      business.get("billingStatus") or "trialing",
        "billingTier":        business.get("billingTier"),
        "billingCountry":     business.get("billingCountry"),
        "starterPriceEur":    business.get("starterPriceEur"),
        "proPriceEur":        business.get("proPriceEur"),
        "trialActive":        trial.active,
        "trialDaysRemaining": trial.days_remaining,
        "trialEndsAt":        business.get("trialEndsAt"),
        "stripeCustomerId":   business.get("stripeCustomerId"),
    }


@router.post("/portal")
async def create_billing_portal(request: Request) -> dict:
    """Create a Stripe Billing Portal session for managing an existing subscription.

    Expected JSON body:
    {
        "businessId": "...",
        "returnUrl":  "https://..."
    }
    """
    from app import firestore as db
    from app.services.billing.stripe_service import create_billing_portal_session

    try:
        body = await request.json()
    except Exception:
        return Response(
            content='{"error":"Invalid JSON"}',
            status_code=400,
            media_type="application/json",
        )

    business_id: str = body.get("businessId") or ""
    return_url: str = body.get("returnUrl") or ""

    if not business_id or not return_url:
        return Response(
            content='{"error":"businessId and returnUrl are required"}',
            status_code=400,
            media_type="application/json",
        )

    business = db.get_business_by_id(business_id)
    if not business:
        return Response(
            content='{"error":"Business not found"}',
            status_code=404,
            media_type="application/json",
        )

    portal_url = create_billing_portal_session(business, return_url)
    if not portal_url:
        return Response(
            content='{"error":"Could not create billing portal session"}',
            status_code=500,
            media_type="application/json",
        )

    return {"portalUrl": portal_url}
