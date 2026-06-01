"""Stripe Checkout redirect URLs — single place to avoid 404 mismatches."""

from __future__ import annotations

from app.config import settings


def checkout_redirect_urls(business_id: str, plan: str) -> tuple[str, str]:
    """Return (success_url, cancel_url) for Stripe Checkout sessions.

    Routes are served by ``app.api.v1.billing`` at ``/api/v1/billing/success``
    and ``/api/v1/billing/cancel``.
    """
    base = settings.BASE_URL.rstrip("/")
    plan_key = (plan or "starter").lower()
    # Stripe replaces {CHECKOUT_SESSION_ID} on redirect — used to sync payment if webhook fails.
    success_url = (
        f"{base}/api/v1/billing/success"
        f"?biz={business_id}&plan={plan_key}&session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = f"{base}/api/v1/billing/cancel"
    return success_url, cancel_url
