"""Stripe billing integration.

Responsibilities
----------------
- Create a Stripe Customer at onboarding (no card required at trial start)
- Create a Stripe Checkout Session when the owner chooses a plan
- Verify and route Stripe webhook events to update internal subscription state
- Create Billing Portal sessions for managing existing subscriptions

Design principles
-----------------
- Stripe is the *payment processor*. The backend is the *source of truth*.
- All subscription status changes come through webhooks, never assumed from
  client-side redirects.
- Prices are resolved dynamically from the billing snapshot stored on the
  business document; no hardcoded Stripe Price IDs are required.
- Annual plans apply a 2-month-free discount (10 months charged for 12).
"""

from __future__ import annotations

import logging

import stripe

from app.config import settings

logger = logging.getLogger(__name__)

_stripe_configured = bool(settings.STRIPE_SECRET_KEY)
if _stripe_configured:
    stripe.api_key = settings.STRIPE_SECRET_KEY


# ── Stripe Customer ───────────────────────────────────────────────────────────

def create_stripe_customer(business: dict) -> str | None:
    """Create a Stripe Customer for the business.  Returns the Customer ID.

    Called during onboarding finalisation.  No payment method is attached —
    the 7-day trial requires no card.
    Returns None when Stripe is not configured or the call fails (non-fatal).
    """
    if not _stripe_configured:
        logger.warning(
            "[STRIPE] STRIPE_SECRET_KEY not set — skipping customer creation for business=%s",
            business.get("id"),
        )
        return None

    try:
        customer = stripe.Customer.create(
            name=business.get("name") or "",
            phone=business.get("ownerPhone") or "",
            metadata={
                "businessId":     business.get("id") or "",
                "ownerPhone":     business.get("ownerPhone") or "",
                "billingTier":    business.get("billingTier") or "",
                "billingCountry": business.get("billingCountry") or "",
            },
        )
        logger.info(
            "[STRIPE] Customer created: %s for business=%s",
            customer.id, business.get("id"),
        )
        return customer.id
    except stripe.StripeError as exc:
        logger.error(
            "[STRIPE] Failed to create customer for business=%s: %s",
            business.get("id"), exc,
        )
        return None


# ── Checkout Session ──────────────────────────────────────────────────────────

def create_checkout_session(
    business: dict,
    plan: str,
    success_url: str,
    cancel_url: str,
    billing_period: str = "monthly",
) -> str | None:
    """Create a Stripe Checkout Session for plan selection.

    Args:
        business:       Business document dict (must contain billing snapshot).
        plan:           "starter" or "pro".
        success_url:    Redirect after successful payment.
        cancel_url:     Redirect if the owner cancels.
        billing_period: "monthly" (default) or "annual" (2 months free).

    Returns:
        The Stripe-hosted checkout URL, or None on failure.
    """
    if not _stripe_configured:
        logger.warning("[STRIPE] STRIPE_SECRET_KEY not set — cannot create checkout session")
        return None

    plan_key = plan.lower()
    if plan_key not in ("starter", "pro"):
        logger.error("[STRIPE] Unknown plan: %r — must be 'starter' or 'pro'", plan)
        return None

    billing_period = billing_period.lower()
    if billing_period not in ("monthly", "annual"):
        billing_period = "monthly"

    # Resolve price from the billing snapshot stored on the business document
    price_field = "starterPriceEur" if plan_key == "starter" else "proPriceEur"
    price_eur: int = int(business.get(price_field) or _fallback_price_eur(business, plan_key))

    if billing_period == "annual":
        # 2 months free → charge 10 months × monthly rate
        unit_amount_cents: int = price_eur * 100 * 10
        interval = "year"
    else:
        unit_amount_cents = price_eur * 100
        interval = "month"

    business_id: str = business.get("id") or ""
    stripe_customer_id: str | None = business.get("stripeCustomerId")

    try:
        logger.debug(
            "[STRIPE] Creating checkout session for business=%s plan=%s period=%s has_customer=%s",
            business_id,
            plan_key,
            billing_period,
            bool(stripe_customer_id),
        )
        session_kwargs: dict = {
            "mode": "subscription",
            "line_items": [{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": unit_amount_cents,
                    "recurring": {"interval": interval},
                    "product_data": {
                        "name": (
                            f"Recepte {plan_key.title()} "
                            f"({'Monthly' if billing_period == 'monthly' else 'Annual'})"
                        ),
                        "metadata": {
                            "plan": plan_key,
                            "tier": business.get("billingTier") or "",
                        },
                    },
                },
                "quantity": 1,
            }],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {
                "businessId":    business_id,
                "plan":          plan_key,
                "billingTier":   business.get("billingTier") or "",
                "billingPeriod": billing_period,
            },
            "subscription_data": {
                "metadata": {
                    "businessId": business_id,
                    "plan":       plan_key,
                },
            },
            # "tax_id_collection": {"enabled": True},
            # "automatic_tax":     {"enabled": True},
        }

        if stripe_customer_id:
            session_kwargs["customer"] = stripe_customer_id
        

        checkout_session = stripe.checkout.Session.create(**session_kwargs)
        logger.info(
            "[STRIPE] Checkout session created: %s for business=%s plan=%s period=%s",
            checkout_session.id, business_id, plan_key, billing_period,
        )
        return checkout_session.url

    except stripe.StripeError as exc:
        logger.error(
            "[STRIPE] Failed to create checkout session for business=%s: %s",
            business_id, exc,
        )
        return None


def create_billing_portal_session(business: dict, return_url: str) -> str | None:
    """Create a Stripe Billing Portal session for managing an existing subscription.

    Returns the portal URL, or None when Stripe is not configured / no customer.
    """
    if not _stripe_configured:
        return None
    stripe_customer_id: str | None = business.get("stripeCustomerId")
    if not stripe_customer_id:
        logger.warning(
            "[STRIPE] Cannot create portal: no stripeCustomerId for business=%s",
            business.get("id"),
        )
        return None
    try:
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url,
        )
        return session.url
    except stripe.StripeError as exc:
        logger.error(
            "[STRIPE] Portal session failed for business=%s: %s",
            business.get("id"), exc,
        )
        return None


# ── Webhook handling ──────────────────────────────────────────────────────────

def _webhook_secrets() -> list[str]:
    """Return one or more signing secrets (comma-separated in env)."""
    raw = (settings.STRIPE_WEBHOOK_SECRET or "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def verify_webhook(payload: bytes, signature: str) -> dict | None:
    """Verify the Stripe webhook signature and parse the event.

    Returns the parsed event dict on success, or None on failure.
    Never raises — invalid webhooks are silently rejected here and the
    caller returns HTTP 400.

    Supports multiple secrets in STRIPE_WEBHOOK_SECRET (comma-separated), e.g.
    Dashboard production secret + Stripe CLI ``stripe listen`` secret.
    """
    secrets = _webhook_secrets()
    if not _stripe_configured or not secrets:
        logger.warning("[STRIPE] Webhook secret not configured — rejecting webhook")
        return None
    last_err: Exception | None = None
    for secret in secrets:
        try:
            event = stripe.Webhook.construct_event(payload, signature, secret)
            return dict(event)
        except stripe.error.SignatureVerificationError as exc:
            last_err = exc
            continue
        except Exception as exc:
            logger.error("[STRIPE] Webhook parse error: %s", exc)
            return None
    logger.warning("[STRIPE] Webhook signature verification failed: %s", last_err)
    return None


def sync_checkout_session(session_id: str) -> str:
    """Activate subscription from a completed Checkout Session (webhook fallback).

    Called from the billing success page when Stripe redirects after payment.
    Idempotent — safe if the webhook already updated the business doc.
    """
    if not _stripe_configured or not session_id:
        return "stripe_not_configured"

    import app.firestore as db  # deferred

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except stripe.StripeError as exc:
        logger.error("[STRIPE] Could not retrieve session %s: %s", session_id, exc)
        return "retrieve_failed"

    payment_status = getattr(session, "payment_status", None) or ""
    status = getattr(session, "status", None) or ""
    if payment_status != "paid":
        logger.info(
            "[STRIPE] sync_checkout_session %s payment_status=%s — not activating",
            session_id, payment_status,
        )
        return f"unpaid:{payment_status}"

    session_dict = session.to_dict() if hasattr(session, "to_dict") else dict(session)
    result = _on_checkout_completed(session_dict, db)
    logger.info("[STRIPE] sync_checkout_session %s → %s", session_id, result)
    return result


def handle_webhook_event(event: dict) -> str:
    """Route a verified Stripe webhook event to the appropriate handler.

    Returns a short status string used for logging and the HTTP response body.
    Import of db is deferred to avoid import-time Firebase initialisation.
    """
    import app.firestore as db  # deferred — Firebase must be initialised first

    event_type: str = event.get("type") or ""
    data_obj: dict = (event.get("data") or {}).get("object") or {}

    if event_type == "checkout.session.completed":
        return _on_checkout_completed(data_obj, db)

    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        return _on_subscription_updated(data_obj, db)

    if event_type == "customer.subscription.deleted":
        return _on_subscription_deleted(data_obj, db)

    if event_type == "invoice.payment_failed":
        return _on_payment_failed(data_obj, db)

    logger.debug("[STRIPE] Unhandled event type: %s", event_type)
    return "unhandled"


# ── Private event handlers ────────────────────────────────────────────────────

def _resolve_business(obj: dict, db) -> dict | None:
    """Find the business from Stripe object metadata or customer ID."""
    meta: dict = obj.get("metadata") or {}

    business_id = meta.get("businessId")
    if business_id:
        biz = db.get_business_by_id(business_id)
        if biz:
            return biz

    stripe_customer_id = obj.get("customer")
    if stripe_customer_id:
        biz = db.get_business_by_stripe_customer_id(stripe_customer_id)
        if biz:
            return biz

    logger.warning(
        "[STRIPE] Could not resolve business — obj.id=%s meta=%s",
        obj.get("id"), meta,
    )
    return None


def _on_checkout_completed(session: dict, db) -> str:
    """checkout.session.completed — subscription created via checkout."""
    business = _resolve_business(session, db)
    if not business:
        return "business_not_found"

    import datetime as _dt

    business_id: str = business["id"]
    plan: str = (session.get("metadata") or {}).get("plan") or "starter"
    stripe_customer_id: str | None = session.get("customer")
    stripe_subscription_id: str | None = session.get("subscription")

    now_utc = _dt.datetime.now(tz=_dt.timezone.utc)

    # ── Plan stacking: carry over unused days from any existing active period ─
    # If the business has an active trial or paid period with days remaining,
    # those days are added to the new 30-day subscription period.
    carryover_days = 0
    for date_field in ("planExpiresAt", "subscriptionRenewalDate"):
        existing_raw = business.get(date_field, "")
        if not existing_raw:
            continue
        try:
            existing_dt = _dt.datetime.fromisoformat(
                str(existing_raw).replace("Z", "+00:00")
            )
            if existing_dt.tzinfo is None:
                existing_dt = existing_dt.replace(tzinfo=_dt.timezone.utc)
            if existing_dt > now_utc:
                days_left = (existing_dt - now_utc).days
                carryover_days = max(carryover_days, days_left)
                break  # found a future date — no need to check further fields
        except (ValueError, TypeError):
            pass

    # Provisional expiry = 30 days from now + any carried-over days.
    # This is updated to the accurate Stripe value when
    # customer.subscription.created/updated fires shortly after.
    plan_expires_at = now_utc + _dt.timedelta(days=30 + carryover_days)

    updates: dict = {
        "plan":                    plan,
        "billingStatus":           "active",
        "stripeSubscriptionId":    stripe_subscription_id,
        # Provisional dates — overwritten by _on_subscription_updated
        "subscriptionRenewalDate": plan_expires_at.isoformat(),
        "planExpiresAt":           plan_expires_at.isoformat(),
    }
    if carryover_days:
        updates["subscriptionCarryoverDays"] = carryover_days
    if stripe_customer_id and not business.get("stripeCustomerId"):
        updates["stripeCustomerId"] = stripe_customer_id

    db.update_business_doc(business_id, updates)
    logger.info(
        "[STRIPE] checkout.session.completed → business=%s plan=%s subscription=%s carryover_days=%d",
        business_id, plan, stripe_subscription_id, carryover_days,
    )
    return "activated"


def _on_subscription_updated(sub: dict, db) -> str:
    """customer.subscription.created / updated — sync status."""
    import datetime as _dt

    business = _resolve_business(sub, db)
    if not business:
        return "business_not_found"

    business_id: str = business["id"]
    stripe_status: str = sub.get("status") or ""
    plan: str = (sub.get("metadata") or {}).get("plan") or business.get("plan") or "starter"

    _status_map: dict[str, str] = {
        "active":   "active",
        "past_due": "past_due",
        "unpaid":   "past_due",
        "canceled": "cancelled",
        "trialing": "trialing",
        "paused":   "paused",
    }
    billing_status = _status_map.get(stripe_status, stripe_status)

    updates: dict = {
        "billingStatus":        billing_status,
        "stripeSubscriptionId": sub.get("id"),
    }
    if stripe_status == "active":
        updates["plan"] = plan

    # Persist the billing cycle end date so the AI can answer renewal questions
    period_end_ts = sub.get("current_period_end")
    if period_end_ts:
        try:
            renewal_dt = _dt.datetime.fromtimestamp(
                int(period_end_ts), tz=_dt.timezone.utc
            )
            updates["subscriptionRenewalDate"] = renewal_dt.isoformat()

            # planExpiresAt = Stripe renewal date + any carryover bonus days
            # (set by _on_checkout_completed when days were stacked from a prior period)
            carryover = int(business.get("subscriptionCarryoverDays") or 0)
            effective_expiry = renewal_dt + _dt.timedelta(days=carryover)
            updates["planExpiresAt"] = effective_expiry.isoformat()
            if carryover:
                # Clear once applied so it doesn't compound on the next renewal
                updates["subscriptionCarryoverDays"] = 0
        except (TypeError, ValueError, OSError):
            pass

    db.update_business_doc(business_id, updates)
    logger.info(
        "[STRIPE] subscription %s → business=%s stripe_status=%s billed=%s",
        sub.get("id"), business_id, stripe_status, billing_status,
    )
    return f"synced:{billing_status}"


def _on_subscription_deleted(sub: dict, db) -> str:
    """customer.subscription.deleted — subscription cancelled."""
    business = _resolve_business(sub, db)
    if not business:
        return "business_not_found"

    db.update_business_doc(business["id"], {
        "billingStatus": "cancelled",
        "plan":          "expired",
    })
    logger.info("[STRIPE] subscription deleted → business=%s cancelled", business["id"])
    return "cancelled"


def _on_payment_failed(invoice: dict, db) -> str:
    """invoice.payment_failed — mark business as past_due."""
    stripe_customer_id: str | None = invoice.get("customer")
    if not stripe_customer_id:
        return "no_customer"

    business = db.get_business_by_stripe_customer_id(stripe_customer_id)
    if not business:
        return "business_not_found"

    db.update_business_doc(business["id"], {"billingStatus": "past_due"})
    logger.warning(
        "[STRIPE] invoice.payment_failed → business=%s marked past_due",
        business["id"],
    )
    return "past_due"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fallback_price_eur(business: dict, plan_key: str) -> int:
    """Return a safe fallback EUR price when the billing snapshot is absent."""
    from app.services.billing.pricing import DEFAULT_TIER, resolve_prices
    tier = business.get("billingTier") or DEFAULT_TIER
    prices = resolve_prices(tier)
    return prices[plan_key]  # type: ignore[literal-required]
