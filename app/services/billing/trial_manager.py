"""Trial manager — 7-day free trial logic.

Rules (from PRICING_MATRIX.md):
  - Length: 7 days
  - Credit card required at signup: NO
  - Default plan during trial: PRO (full feature access)
  - Trial-end conversion: prompt for plan choice at day 7
  - Soft card-collection prompt: day 5 (optional, not required)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TRIAL_DAYS: int = 7
SOFT_PROMPT_DAY: int = 5  # Send optional soft reminder on day 5
HARD_PROMPT_DAY: int = 7  # Send conversion/plan-selection prompt on day 7


class TrialStatus:
    """Immutable snapshot of a business's trial state."""

    __slots__ = (
        "active", "days_remaining", "days_elapsed", "expired", "trial_end",
    )

    def __init__(
        self,
        *,
        active: bool,
        days_remaining: int,
        days_elapsed: int,
        expired: bool,
        trial_end: datetime | None,
    ) -> None:
        self.active = active
        self.days_remaining = days_remaining
        self.days_elapsed = days_elapsed
        self.expired = expired
        self.trial_end = trial_end

    def __repr__(self) -> str:
        return (
            f"<TrialStatus active={self.active} "
            f"days_remaining={self.days_remaining} "
            f"expired={self.expired}>"
        )


_EXPIRED = TrialStatus(
    active=False, days_remaining=0, days_elapsed=TRIAL_DAYS,
    expired=True, trial_end=None,
)


def get_trial_status(business: dict) -> TrialStatus:
    """Compute the current trial status snapshot for a business dict."""
    plan = str(business.get("plan") or "").lower()
    if plan not in ("trial", "trialing"):
        return _EXPIRED

    now = datetime.now(timezone.utc)

    trial_ends_raw = business.get("trialEndsAt")
    if not trial_ends_raw:
        return _EXPIRED
    try:
        trial_end = datetime.fromisoformat(str(trial_ends_raw).replace("Z", "+00:00"))
        if trial_end.tzinfo is None:
            trial_end = trial_end.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _EXPIRED

    # Derive trial start from stored field or fall back to (end - 7 days)
    trial_start_raw = business.get("trialStartedAt") or business.get("createdAt")
    try:
        trial_start = datetime.fromisoformat(str(trial_start_raw).replace("Z", "+00:00"))
        if trial_start.tzinfo is None:
            trial_start = trial_start.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        trial_start = trial_end - timedelta(days=TRIAL_DAYS)

    days_elapsed = max(0, (now - trial_start).days)
    days_remaining = max(0, (trial_end - now).days)
    expired = now > trial_end
    active = not expired

    return TrialStatus(
        active=active,
        days_remaining=days_remaining,
        days_elapsed=days_elapsed,
        expired=expired,
        trial_end=trial_end,
    )


def should_send_soft_prompt(business: dict) -> bool:
    """Return True when the day-5 soft card-collection prompt should be sent.

    Conditions: trial active, at least 5 days elapsed, not yet sent.
    """
    status = get_trial_status(business)
    if not status.active:
        return False
    if status.days_elapsed < SOFT_PROMPT_DAY:
        return False
    if business.get("trialSoftPromptSentAt"):
        return False  # Already sent
    return True


def should_send_hard_prompt(business: dict) -> bool:
    """Return True when the day-7 conversion prompt should be sent.

    Conditions: still on trial plan, at least 7 days elapsed OR trial expired,
    and the conversion prompt has not already been sent.
    """
    plan = str(business.get("plan") or "").lower()
    if plan not in ("trial", "trialing"):
        return False  # Already converted or different state

    status = get_trial_status(business)
    if status.days_elapsed < HARD_PROMPT_DAY and not status.expired:
        return False

    if business.get("trialConversionPromptSentAt"):
        return False  # Already sent
    return True


def build_trial_fields(now: datetime | None = None) -> dict:
    """Return the Firestore fields that start a fresh 7-day trial."""
    if now is None:
        now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=TRIAL_DAYS)
    return {
        "plan": "trialing",
        "trialStartedAt": now.isoformat(),
        "trialEndsAt": trial_end.isoformat(),
    }
