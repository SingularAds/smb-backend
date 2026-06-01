"""Canonical subscription plan information for onboarding & support replies.

Single source of truth for WhatsApp-facing plan/pricing answers.
Recepte has exactly TWO paid plans: Starter and Pro.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.services.billing.feature_gate import (
    PRO_FEATURES,
    STARTER_FEATURES,
    STARTER_MONTHLY_CONVERSATION_LIMIT,
    get_effective_plan,
)
from app.services.billing.pricing import DEFAULT_TIER, resolve_prices
from app.services.billing.trial_manager import TRIAL_DAYS, get_trial_status

# Recepte subscription catalog — only these two plans exist.
AVAILABLE_PLAN_KEYS: tuple[str, ...] = ("starter", "pro")

_PLAN_DISPLAY_NAMES: dict[str, str] = {
    "starter": "Starter",
    "pro": "Pro",
}

_STARTER_FEATURE_LINES: tuple[str, ...] = (
    "AI receptionist (WhatsApp + calls)",
    "Booking & calendar integration",
    "Email & WhatsApp support",
    f"Up to {STARTER_MONTHLY_CONVERSATION_LIMIT} conversations/month",
)

_PRO_EXTRA_FEATURE_LINES: tuple[str, ...] = (
    "Win-back automation",
    "Reminders (vaccine, treatment, birthday)",
    "Google Review automation",
    "Referral system",
    "Instagram collabs & tag-a-friend autopost",
    "Loyalty stamp card",
    "WhatsApp Status content generator",
    "Customer LTV insights",
    "Priority support",
    "Unlimited conversations",
)

# Catalog / pricing questions (does NOT include bare "pro" or "starter").
_CATALOG_INQUIRY_RE = re.compile(
    r"\b("
    r"price|pricing|cost|fee|how much|subscription|subscribe|monthly|annual|"
    r"payment|pay|upgrade|downgrade|cheapest|expensive|free|trial|"
    r"feature|features|what do i get|what's included|whats included|"
    r"plan details|plan info|available plan|all plans|compare plans|"
    r"quanto|cuanto|valor|tarifa|preço|preco|plano|planos|assinatura|pagar"
    r")\b",
    re.IGNORECASE,
)

_CATALOG_PHRASE_RE = re.compile(
    r"(tell me|show me|share|send me|what are|list).{0,30}\b(plan|plans|pricing|price)",
    re.IGNORECASE,
)

_CURRENT_PLAN_RE = re.compile(
    r"\b("
    r"which plan|what plan|my plan|current plan|am i on|plan am i|"
    r"my subscription|subscription status|billing status|"
    r"when.*renew|renewal date|expir|expires|expiry|"
    r"payment date|paid until|valid until|trial end|"
    r"my payment|payment details|payment info|billing details|my billing|my invoice|"
    r"how many day|days left|days remaining|how long|until when|"
    r"when does.*end|when will.*end|when does.*expire|when will.*expire"
    r")\b",
    re.IGNORECASE,
)

# Owner says they finished paying — must verify against DB, never show catalog.
_PAYMENT_COMPLETION_RE = re.compile(
    r"("
    r"done\s+with\s+payment|payment\s+done|done\s+paying|finished\s+paying|"
    r"completed\s+payment|finished\s+payment|made\s+(the\s+)?payment|"
    r"payment\s+complete|payment\s+completed|payment\s+finished|"
    r"i\s+(have\s+)?paid|already\s+paid|just\s+paid|"
    r"yes.{0,40}(done|finished|complete).{0,20}payment|"
    r"(done|finished|complete).{0,30}(with\s+)?payment|"
    r"pagado|paguei|payé|bezahlt|pagato"
    r")",
    re.IGNORECASE,
)

# Legacy short phrases (kept for compatibility).
_PAYMENT_CHECK_RE = re.compile(
    r"\b("
    r"i paid|i have paid|already paid|payment done|done paying|"
    r"paid already|completed payment|finished paying"
    r")\b",
    re.IGNORECASE,
)

# Asking for a checkout URL — not a catalog request.
_CHECKOUT_LINK_REQUEST_RE = re.compile(
    r"("
    r"payment\s+link|pay\s+link|checkout\s+link|stripe\s+link|"
    r"link\s+for\s+payment|link\s+to\s+pay|send\s+(me\s+)?(the\s+)?link|"
    r"how\s+(do\s+i|to)\s+pay|where\s+(do\s+i|to)\s+pay"
    r")",
    re.IGNORECASE,
)

_RESEND_CHECKOUT_RE = re.compile(
    r"\b(resend|re-send|send again|new link|another link|link again)\b",
    re.IGNORECASE,
)

_SELECTION_VERB_RE = re.compile(
    r"\b(want|get|choose|pick|select|subscribe|sign up|take|go with|"
    r"i'll take|ill take|interested in|go for)\b",
    re.IGNORECASE,
)


def parse_plan_selection(text: str) -> str | None:
    """Return 'starter' or 'pro' when the user is choosing a plan, else None."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return None

    # Bare selection (common WhatsApp replies)
    if re.match(r"^(starter|start|basic)$", normalized):
        return "starter"
    if re.match(r"^(pro|pri|professional|premium)$", normalized):
        return "pro"

    has_starter = bool(re.search(r"\b(starter|start|basic)\b", normalized))
    has_pro = bool(re.search(r"\b(pro|professional|premium)\b", normalized))

    # "pro plan" / "starter plan" with intent to subscribe
    if _SELECTION_VERB_RE.search(normalized) or re.search(
        r"\b(i want|i need|i'd like|id like)\b", normalized
    ):
        if has_pro and not has_starter:
            return "pro"
        if has_starter and not has_pro:
            return "starter"
        if has_pro and has_starter:
            return None  # ambiguous — let AI clarify

    # Short phrases: "pro plan", "starter plan" without inquiry context
    if re.match(r"^(pro|pri)\s+plan", normalized) or normalized in {
        "pro plan", "starter plan", "the pro plan", "the starter plan",
    }:
        return "pro" if has_pro or normalized.startswith("pro") or normalized.startswith("pri") else "starter"

    return None


def is_payment_status_check(text: str) -> bool:
    """True when the owner claims they completed or are completing payment."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _PAYMENT_CHECK_RE.search(stripped) or _PAYMENT_COMPLETION_RE.search(stripped):
        return True
    normalized = stripped.lower()
    # "Yes" + done/finished + pay*  e.g. "Yes I am done with payment"
    if re.search(r"\b(yes|yeah|yep|si|sí|sim)\b", normalized):
        if re.search(r"\b(done|finished|complete|paid)\b", normalized) and re.search(
            r"pay", normalized
        ):
            return True
    return False


# After a checkout link is sent, these short replies mean "I finished paying".
_POST_CHECKOUT_CONFIRM_WORDS: frozenset[str] = frozenset({
    "done", "finished", "complete", "completed", "ok", "okay", "yes", "paid",
    "pronto", "feito", "listo", "hecho", "fait", "fertig",
})


def has_pending_checkout(session: dict | None) -> bool:
    """True when we sent a Stripe link and are waiting for payment confirmation."""
    return bool(session and session.get("pendingCheckoutPlan"))


def is_payment_confirmation_attempt(text: str, session: dict | None = None) -> bool:
    """True when the owner is indicating they completed payment.

  Always matches explicit payment-completion phrases.
  After a checkout link was sent, also matches short replies like 'Done'.
    """
    if is_payment_status_check(text):
        return True
    if not has_pending_checkout(session):
        return False
    normalized = (text or "").strip().lower()
    if normalized in _POST_CHECKOUT_CONFIRM_WORDS:
        return True
    # Pending checkout + any completion wording with pay/payment
    if re.search(r"\b(done|finished|complete|paid)\b", normalized) and re.search(
        r"pay", normalized
    ):
        return True
    return False


def is_resend_checkout_request(text: str, session: dict | None = None) -> bool:
    """True when the owner wants the payment link sent again."""
    stripped = (text or "").strip()
    if not _RESEND_CHECKOUT_RE.search(stripped):
        return False
    # Avoid pairing-code resend confusion
    if re.search(r"\b(code|pairing|whatsapp)\b", stripped, re.IGNORECASE):
        return False
    return bool(has_pending_checkout(session) or parse_plan_selection(stripped))


def is_checkout_link_request(text: str) -> bool:
    """True when the owner wants a payment/checkout link (not a plan catalog)."""
    stripped = (text or "").strip()
    if not stripped or is_payment_status_check(stripped):
        return False
    return bool(_CHECKOUT_LINK_REQUEST_RE.search(stripped))


def is_subscription_paid_in_db(business: dict) -> bool:
    """Return True only when Firestore shows an active paid subscription."""
    effective = get_effective_plan(business)
    raw_plan = str(business.get("plan") or "").lower()
    billing_status = str(business.get("billingStatus") or "").lower()

    if effective in ("starter", "pro", "active"):
        return True
    if raw_plan in ("starter", "pro") and billing_status == "active":
        return True
    if business.get("stripeSubscriptionId") and billing_status == "active":
        return True
    return False


def is_current_plan_inquiry(text: str) -> bool:
    return bool(_CURRENT_PLAN_RE.search((text or "").strip()))


def has_plan_catalog_inquiry(text: str, session: dict | None = None) -> bool:
    """True when asking to see plans/pricing — not when selecting or confirming payment."""
    stripped = (text or "").strip()
    if parse_plan_selection(stripped):
        return False
    if is_payment_confirmation_attempt(stripped, session):
        return False
    if is_checkout_link_request(stripped):
        return False
    if is_current_plan_inquiry(stripped):
        return False
    return bool(
        _CATALOG_INQUIRY_RE.search(stripped) or _CATALOG_PHRASE_RE.search(stripped)
    )


def has_plan_pricing_intent(text: str, session: dict | None = None) -> bool:
    """Broad billing-related message (catalog, selection, status, or payment check)."""
    stripped = (text or "").strip()
    return classify_billing_message(stripped, session) is not None


def classify_billing_message(
    text: str, session: dict | None = None
) -> str | None:
    """Classify billing-related owner messages.

    Returns one of:
      payment_check  — owner claims they paid
      current_status — which plan am I on / renewal / expiry
      select_plan    — choosing starter or pro (checkout should be sent)
      catalog        — wants to see available plans and pricing
      None           — not a billing message
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    if is_payment_confirmation_attempt(stripped, session):
        return "payment_check"
    if is_resend_checkout_request(stripped, session):
        return "resend_checkout"
    if is_current_plan_inquiry(stripped):
        return "current_status"
    if parse_plan_selection(stripped):
        return "select_plan"
    if is_checkout_link_request(stripped):
        return "checkout_link"
    if has_plan_catalog_inquiry(stripped, session):
        return "catalog"
    return None


def resolve_business_prices(business: dict) -> dict[str, int]:
    tier = business.get("billingTier") or DEFAULT_TIER
    prices = resolve_prices(tier)
    return {
        "starter": int(business.get("starterPriceEur") or prices["starter"]),
        "pro": int(business.get("proPriceEur") or prices["pro"]),
    }


def _format_plan_block(plan_key: str, price_eur: int) -> list[str]:
    name = _PLAN_DISPLAY_NAMES[plan_key]
    lines = [f"*{name} Plan — €{price_eur}/month*"]
    if plan_key == "starter":
        for feat in _STARTER_FEATURE_LINES:
            lines.append(f"✅ {feat}")
    else:
        lines.append("✅ Everything in Starter")
        for feat in _PRO_EXTRA_FEATURE_LINES:
            lines.append(f"✅ {feat}")
    return lines


def format_available_plans_catalog(business: dict) -> str:
    prices = resolve_business_prices(business)
    lines = [
        "Recepte has *two subscription plans* — Starter and Pro:",
        "",
    ]
    lines.extend(_format_plan_block("starter", prices["starter"]))
    lines.append("")
    lines.extend(_format_plan_block("pro", prices["pro"]))
    lines.append("")
    lines.append(
        f"🎁 Every new business gets a *{TRIAL_DAYS}-day free trial* with full Pro "
        "access — no credit card required."
    )
    lines.append("")
    lines.append(
        "To subscribe, reply *starter* or *pro* and I'll send your secure payment link."
    )
    return "\n".join(lines)


def _describe_current_plan(business: dict) -> list[str]:
    effective = get_effective_plan(business)
    raw_plan = str(business.get("plan") or "").lower()
    biz_name = business.get("name", "your business")
    lines = [f"Business: {biz_name}"]

    if raw_plan == "onboarding":
        lines.append(
            "Current status: Setup in progress (onboarding). "
            f"Your {TRIAL_DAYS}-day free Pro trial starts once WhatsApp is connected."
        )
        return lines

    if effective in ("trialing", "trial"):
        trial = get_trial_status(business)
        lines.append("Current plan: Free trial (full Pro access)")
        if trial.trial_end:
            lines.append(f"Trial ends: {trial.trial_end.strftime('%B %d, %Y')}")
        if trial.days_remaining > 0:
            lines.append(f"Days remaining: {trial.days_remaining}")
        return lines

    display = _PLAN_DISPLAY_NAMES.get(effective, effective.upper())
    lines.append(f"Current plan: {display}")

    prices = resolve_business_prices(business)
    billing_period = business.get("billingPeriod", "monthly")
    if effective in ("starter", "pro", "active"):
        plan_key = "pro" if effective in ("pro", "active") else "starter"
        monthly = prices[plan_key]
        if billing_period == "annual":
            lines.append(f"Cost: €{monthly * 10}/year (annual billing)")
        else:
            lines.append(f"Cost: €{monthly}/month")

    # Prefer effective planExpiresAt (includes stacked days) over Stripe's raw renewal date
    renewal_raw = (
        business.get("planExpiresAt")
        or business.get("subscriptionRenewalDate", "")
    )
    if renewal_raw and effective in ("starter", "pro", "active"):
        try:
            rd = datetime.fromisoformat(str(renewal_raw).replace("Z", "+00:00"))
            if rd.tzinfo is None:
                rd = rd.replace(tzinfo=timezone.utc)
            lines.append(f"Next renewal: {rd.strftime('%B %d, %Y')}")
        except (ValueError, TypeError):
            pass

    billing_status = str(business.get("billingStatus") or "").lower()
    if billing_status and billing_status not in ("active", effective):
        lines.append(f"Billing status: {billing_status}")

    if effective == "expired":
        lines.append("Status: Trial/subscription expired — choose Starter or Pro to continue.")
    elif effective == "past_due":
        lines.append("Status: Payment overdue — please update billing to restore service.")
    elif effective == "cancelled":
        lines.append("Status: Subscription cancelled.")

    return lines


def format_current_plan_status_reply(business: dict) -> str:
    """Owner-facing reply for 'which plan am I on?' style questions."""
    effective = get_effective_plan(business)
    raw_plan = str(business.get("plan") or "").lower()
    prices = resolve_business_prices(business)
    lines: list[str] = ["📋 *Your subscription status*\n"]

    if raw_plan == "onboarding":
        lines.append(
            "You're still completing setup. Your *7-day free Pro trial* starts "
            "automatically once WhatsApp is connected."
        )
        lines.append(
            f"\nAfter the trial you can choose *Starter* (€{prices['starter']}/mo) "
            f"or *Pro* (€{prices['pro']}/mo)."
        )
        return "\n".join(lines)

    if effective in ("trialing", "trial"):
        trial = get_trial_status(business)
        lines.append("Plan: *Free trial* (full Pro features)")
        if trial.trial_end:
            lines.append(f"Trial ends: *{trial.trial_end.strftime('%B %d, %Y')}*")
        if trial.days_remaining > 0:
            lines.append(f"Days left: *{trial.days_remaining}*")
        lines.append(
            f"\nAfter the trial: *Starter* €{prices['starter']}/mo or *Pro* €{prices['pro']}/mo. "
            "Reply *starter* or *pro* anytime to subscribe early."
        )
        return "\n".join(lines)

    if effective in ("starter", "pro", "active"):
        plan_key = "pro" if effective in ("pro", "active") else "starter"
        lines.append(f"Plan: *{_PLAN_DISPLAY_NAMES[plan_key]}* — €{prices[plan_key]}/month")
        lines.append("Status: *Active* ✅")
        # Prefer planExpiresAt (includes stacked days) over raw Stripe renewal date
        renewal_raw = (
            business.get("planExpiresAt")
            or business.get("subscriptionRenewalDate", "")
        )
        if renewal_raw:
            try:
                rd = datetime.fromisoformat(str(renewal_raw).replace("Z", "+00:00"))
                if rd.tzinfo is None:
                    rd = rd.replace(tzinfo=timezone.utc)
                lines.append(f"Next renewal: *{rd.strftime('%B %d, %Y')}*")
            except (ValueError, TypeError):
                pass
        return "\n".join(lines)

    if effective == "expired":
        lines.append("Status: *Expired* — your trial or subscription has ended.")
        lines.append("Reply *starter* or *pro* to get a payment link and reactivate.")
        return "\n".join(lines)

    if effective == "past_due":
        lines.append("Status: *Payment overdue* ⚠️")
        lines.append("Please complete payment to restore your AI receptionist.")
        lines.append("Reply *starter* or *pro* for a new payment link.")
        return "\n".join(lines)

    lines.append(f"Status: {effective}")
    return "\n".join(lines)


def format_checkout_link_prompt(business: dict, session: dict | None = None) -> str:
    """When owner asks for a payment link — prompt plan choice or resend pending."""
    pending = (session or {}).get("pendingCheckoutPlan")
    if pending:
        display = _PLAN_DISPLAY_NAMES.get(pending.lower(), pending.title())
        return (
            f"You already have a *{display}* payment link pending.\n\n"
            "Complete payment at that link, or reply *starter* / *pro* "
            "if you want a different plan."
        )
    prices = resolve_business_prices(business)
    return (
        "I can send you a secure Stripe payment link right away.\n\n"
        f"• *Starter* — €{prices['starter']}/month\n"
        f"• *Pro* — €{prices['pro']}/month\n\n"
        "Reply *starter* or *pro* to get your link."
    )


def format_payment_pending_reply(pending_plan: str | None = None) -> str:
    plan_note = ""
    if pending_plan:
        display = _PLAN_DISPLAY_NAMES.get(pending_plan.lower(), pending_plan.title())
        plan_note = f" for the *{display}* plan"
    return (
        f"⏳ I checked our system — your payment{plan_note} is *not confirmed yet*.\n\n"
        "Please complete payment at the Stripe link we sent. "
        "Your plan activates *only after* our payment provider confirms it — "
        "you'll get a message here automatically.\n\n"
        "Need a new link? Reply *starter* or *pro*."
    )


def format_payment_confirmed_reply(business: dict) -> str:
    effective = get_effective_plan(business)
    biz_name = business.get("name", "your business")
    plan_key = "pro" if effective in ("pro", "active") else "starter"
    display = _PLAN_DISPLAY_NAMES.get(plan_key, plan_key.title())
    lines = [
        f"✅ *Payment confirmed!*\n",
        f"Your *{display}* plan for *{biz_name}* is *active*.",
    ]
    # Prefer planExpiresAt (includes stacked days) over raw Stripe renewal date
    renewal_raw = (
        business.get("planExpiresAt")
        or business.get("subscriptionRenewalDate", "")
    )
    if renewal_raw:
        try:
            rd = datetime.fromisoformat(str(renewal_raw).replace("Z", "+00:00"))
            if rd.tzinfo is None:
                rd = rd.replace(tzinfo=timezone.utc)
            lines.append(f"Next renewal: *{rd.strftime('%B %d, %Y')}*")
        except (ValueError, TypeError):
            pass
    lines.append("\nYour AI receptionist is live. 🎉")
    return "\n".join(lines)


def format_checkout_link_message(
    business: dict, plan: str, checkout_url: str
) -> str:
    plan_key = plan.lower()
    prices = resolve_business_prices(business)
    price = prices.get(plan_key, 0)
    biz_name = business.get("name", "your business")
    display = _PLAN_DISPLAY_NAMES.get(plan_key, plan_key.title())
    return (
        f"💳 *{display} Plan — €{price}/month*\n\n"
        f"Complete your secure payment here:\n{checkout_url}\n\n"
        f"Once payment is confirmed, your *{biz_name}* subscription activates "
        "automatically — I'll message you here when it's done. ✅"
    )


def build_plan_info_for_tool(business: dict) -> str:
    prices = resolve_business_prices(business)
    current_lines = _describe_current_plan(business)

    # Include explicit expiry/renewal date so AI can answer "when does my plan expire"
    expiry_lines: list[str] = []
    for date_field, label in (
        ("planExpiresAt", "Effective plan expiry (planExpiresAt)"),
        ("subscriptionRenewalDate", "Stripe renewal date (subscriptionRenewalDate)"),
    ):
        raw = business.get(date_field, "")
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                expiry_lines.append(f"{label}: {dt.strftime('%B %d, %Y')} ({raw})")
                break  # one date is enough
            except (ValueError, TypeError):
                pass
    if not expiry_lines:
        expiry_lines.append(
            "No renewal/expiry date stored yet — the Stripe subscription event may "
            "not have been processed. Advise the owner to check back shortly, or "
            "tell them their plan is billed monthly."
        )

    return "\n".join([
        "IMPORTANT: Recepte has EXACTLY TWO subscription plans: Starter and Pro.",
        "Never mention Basic, Enterprise, Premium, or any other plan name.",
        "To subscribe, call send_checkout_link with plan 'starter' or 'pro'.",
        "",
        "=== CURRENT PLAN STATUS ===",
        *current_lines,
        "",
        "=== RENEWAL / EXPIRY ===",
        *expiry_lines,
        "",
        "=== AVAILABLE PLANS (only these two exist) ===",
        f"Starter — €{prices['starter']}/month",
        *[f"  • {f}" for f in _STARTER_FEATURE_LINES],
        "",
        f"Pro — €{prices['pro']}/month",
        "  • Everything in Starter, plus:",
        *[f"  • {f}" for f in _PRO_EXTRA_FEATURE_LINES],
        "",
        f"Trial: {TRIAL_DAYS}-day free Pro trial for new businesses (no card required).",
        "",
        "Feature sets (internal reference):",
        f"  starter_features: {', '.join(sorted(STARTER_FEATURES))}",
        f"  pro_features: {', '.join(sorted(PRO_FEATURES))}",
    ])


def format_plan_pricing_reply_for_phone(
    phone: str, *, country: str | None = None, include_current: bool = False
) -> str:
    from app.services.billing.pricing import build_billing_snapshot

    snapshot = build_billing_snapshot(phone, country=country)
    pseudo_business = {"plan": "onboarding", **snapshot}
    return format_plan_pricing_reply(pseudo_business, include_current=include_current)


def format_plan_pricing_reply(business: dict, *, include_current: bool = True) -> str:
    parts: list[str] = []

    if include_current:
        raw_plan = str(business.get("plan") or "").lower()
        effective = get_effective_plan(business)

        if raw_plan == "onboarding":
            parts.append(
                "You're still finishing setup — your free trial hasn't started yet. "
                "It begins automatically once WhatsApp is connected. 👍"
            )
        elif effective in ("trialing", "trial"):
            trial = get_trial_status(business)
            remaining = trial.days_remaining
            parts.append(
                f"You're on the *free {TRIAL_DAYS}-day trial* with full Pro access"
                + (f" ({remaining} day{'s' if remaining != 1 else ''} left)." if remaining else ".")
            )
        elif effective in ("starter", "pro", "active"):
            plan_key = "pro" if effective in ("pro", "active") else "starter"
            prices = resolve_business_prices(business)
            parts.append(
                f"You're currently on the *{_PLAN_DISPLAY_NAMES[plan_key]} Plan* "
                f"(€{prices[plan_key]}/month)."
            )
        elif effective in ("expired", "past_due", "cancelled"):
            parts.append("Your subscription isn't active right now. Choose a plan below:")
        parts.append("")

    parts.append(format_available_plans_catalog(business))
    return "\n".join(parts).strip()
