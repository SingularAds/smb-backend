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

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings

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


# ── WhatsApp deep-link: generate a fresh checkout URL and redirect ────────────

@router.get("/subscribe")
async def subscribe_redirect(
    businessId: str = Query(..., description="Firestore business document ID"),
    plan: str = Query("starter", description="starter or pro"),
    period: str = Query("monthly", description="monthly or annual"),
) -> Response:
    """Redirect the owner to a freshly-created Stripe Checkout page.

    This endpoint is the link that goes inside WhatsApp reminder messages.
    Because Stripe Checkout sessions expire (~24 h), we generate a brand-new
    session on every click so the link never becomes stale.

    Usage:
        GET /api/v1/billing/subscribe?businessId=XXX&plan=starter
        GET /api/v1/billing/subscribe?businessId=XXX&plan=pro&period=annual
    """
    from app import firestore as db
    from app.services.billing.stripe_service import create_checkout_session

    plan = plan.lower()
    if plan not in ("starter", "pro"):
        return Response(
            content='{"error":"plan must be starter or pro"}',
            status_code=400,
            media_type="application/json",
        )

    business = db.get_business_by_id(businessId)
    if not business:
        return Response(
            content='{"error":"Business not found"}',
            status_code=404,
            media_type="application/json",
        )

    base_url = settings.BASE_URL.rstrip("/")
    success_url = f"{base_url}/api/v1/billing/success?biz={businessId}&plan={plan}"
    cancel_url = f"{base_url}/api/v1/billing/cancel"

    checkout_url = create_checkout_session(
        business=business,
        plan=plan,
        success_url=success_url,
        cancel_url=cancel_url,
        billing_period=period,
    )

    if not checkout_url:
        logger.error("[BILLING] subscribe_redirect: checkout session failed for business=%s", businessId)
        # Fallback HTML — guide the owner to contact support rather than a raw JSON error
        html = _page(
            title="Payment Setup Failed",
            body=(
                "<h2>⚠️ We could not start the payment page.</h2>"
                "<p>Please reply <b>PAY</b> on WhatsApp and we will send you a fresh link.</p>"
            ),
            color="#e74c3c",
        )
        return HTMLResponse(content=html, status_code=503)

    logger.info("[BILLING] subscribe_redirect: redirecting business=%s plan=%s to Stripe", businessId, plan)
    return RedirectResponse(url=checkout_url, status_code=302)


# ── Post-payment landing pages ────────────────────────────────────────────────

@router.get("/success")
async def billing_success(
    biz: str = Query("", description="Business ID from success redirect"),
    plan: str = Query("", description="Plan name from success redirect"),
) -> HTMLResponse:
    """Landing page after successful Stripe payment.

    Stripe redirects the owner here after a completed checkout.
    Since the platform is WhatsApp-first, this page simply confirms payment
    and tells the owner to return to WhatsApp.
    """
    plan_label = plan.title() if plan else "Selected"
    html = _page(
        title="Payment Successful",
        body=(
            f"<h2>🎉 Payment confirmed!</h2>"
            f"<p>Your <b>Recepte {plan_label}</b> subscription is now <b>active</b>.</p>"
            f"<p>Your AI receptionist will resume automatically within a few seconds.</p>"
            f"<hr>"
            f"<p>📱 <b>Return to WhatsApp</b> — your assistant is ready.</p>"
            f"<p style='font-size:0.85em;color:#888;'>Business ID: {biz}</p>"
        ),
        color="#27ae60",
    )
    return HTMLResponse(content=html, status_code=200)


@router.get("/cancel")
async def billing_cancel() -> HTMLResponse:
    """Landing page when the owner cancels Stripe checkout.

    This is the cancel_url supplied to every Stripe Checkout session.
    """
    html = _page(
        title="Payment Cancelled",
        body=(
            "<h2>👋 No charge was made.</h2>"
            "<p>You cancelled the payment — that's completely fine.</p>"
            "<p>Your free trial data is still safe.</p>"
            "<hr>"
            "<p>📱 <b>Reply on WhatsApp</b> whenever you're ready to choose a plan.<br>"
            "Your AI receptionist will reactivate the moment payment is confirmed.</p>"
        ),
        color="#e67e22",
    )
    return HTMLResponse(content=html, status_code=200)


# ── Internal HTML helper ──────────────────────────────────────────────────────

def _page(title: str, body: str, color: str = "#2c3e50") -> str:
    """Return a minimal, mobile-friendly HTML page."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Recepte</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f6fa;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 20px;
    }}
    .card {{
      background: #fff;
      border-radius: 16px;
      padding: 40px 32px;
      max-width: 480px;
      width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,0.08);
      border-top: 6px solid {color};
      text-align: center;
    }}
    h2 {{ font-size: 1.6rem; margin-bottom: 16px; color: {color}; }}
    p  {{ color: #555; line-height: 1.6; margin-bottom: 12px; }}
    hr {{ border: none; border-top: 1px solid #eee; margin: 20px 0; }}
    b  {{ color: #333; }}
  </style>
</head>
<body>
  <div class="card">
    <p style="font-size:2rem;margin-bottom:8px;">🤖</p>
    <p style="font-weight:700;font-size:1.1rem;color:#333;margin-bottom:24px;">Recepte</p>
    {body}
  </div>
</body>
</html>"""
