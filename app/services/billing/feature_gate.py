"""Feature gate — authoritative map of which plans unlock which features.

The backend is the single source of truth.
No feature access may be determined by frontend or client logic.
"""

from __future__ import annotations

from datetime import datetime, timezone


# ── Plan → feature sets ───────────────────────────────────────────────────────

STARTER_FEATURES: frozenset[str] = frozenset({
    "ai_receptionist",           # WhatsApp + call AI receptionist
    "booking",                   # Booking + calendar integration
    "email_support",
    "whatsapp_support",
    # Starter has hard usage limits checked separately via is_within_conversation_limit()
})

PRO_FEATURES: frozenset[str] = STARTER_FEATURES | frozenset({
    "win_back_automation",       # Win-back automation
    "reminders",                 # Vaccine / treatment / birthday reminders
    "google_review_automation",  # Google Review automation
    "referral_system",           # Friend-brings-friend referral
    "instagram_collabs",         # Instagram collaborations
    "tag_friend_autopost",       # Tag-a-friend auto-post
    "loyalty_stamp_card",        # Loyalty stamp card
    "whatsapp_status_content",   # WhatsApp Status content generator
    "customer_ltv_insights",     # Customer LTV insights
    "priority_support",
})

# Trialing users get full PRO access for 7 days; no card required.
TRIAL_FEATURES: frozenset[str] = PRO_FEATURES

# Expired / past-due → zero access until the SMB owner subscribes.
# Per PRICING_MATRIX: "After the 7-day trial, if the user has not subscribed,
# access should stop completely for that SMB owner until they choose a plan and pay."
EXPIRED_FEATURES: frozenset[str] = frozenset()

_PLAN_FEATURES: dict[str, frozenset[str]] = {
    "trialing":  TRIAL_FEATURES,
    "trial":     TRIAL_FEATURES,   # legacy alias used in existing docs
    "starter":   STARTER_FEATURES,
    "pro":       PRO_FEATURES,
    "active":    PRO_FEATURES,     # legacy status alias
    "expired":   EXPIRED_FEATURES,
    "past_due":  EXPIRED_FEATURES,
    "cancelled": frozenset(),
}

# Starter plan monthly conversation cap
STARTER_MONTHLY_CONVERSATION_LIMIT: int = 500


# ── Public API ────────────────────────────────────────────────────────────────

def get_effective_plan(business: dict) -> str:
    """Return the business's effective plan name.

    - plan == 'trialing' / 'trial' and within trial window → 'trialing'
    - plan == 'trialing' / 'trial' and window expired      → 'expired'
    - Otherwise return the stored plan value verbatim.
    """
    plan = str(business.get("plan") or "expired").lower()

    if plan in ("trialing", "trial"):
        trial_ends_raw = business.get("trialEndsAt")
        if trial_ends_raw:
            try:
                trial_end = datetime.fromisoformat(
                    str(trial_ends_raw).replace("Z", "+00:00")
                )
                if trial_end.tzinfo is None:
                    trial_end = trial_end.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) <= trial_end:
                    return "trialing"
                return "expired"
            except (ValueError, TypeError):
                pass
        return "expired"

    return plan


def can_access_feature(business: dict, feature: str) -> bool:
    """Return True when the business's current plan grants access to *feature*."""
    effective = get_effective_plan(business)
    allowed = _PLAN_FEATURES.get(effective, frozenset())
    return feature in allowed


def is_within_conversation_limit(business: dict, monthly_count: int) -> bool:
    """Return True if the business has not yet hit its monthly conversation cap.

    Only the Starter plan is capped (500 conversations/month).
    All other plans are unlimited.
    """
    effective = get_effective_plan(business)
    if effective == "starter":
        return monthly_count < STARTER_MONTHLY_CONVERSATION_LIMIT
    return True
