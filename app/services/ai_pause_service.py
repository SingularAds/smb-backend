"""Per-customer AI pause/resume state.

The AI must be paused for a single customer (not the whole business) when
any of these happen:

  * owner_takeover   — the owner manually replied in the customer's chat
  * frustration      — the classifier detected an upset customer
  * abuse            — the classifier detected inappropriate language
  * manual           — the owner explicitly typed "resume" (handled by resume(), not pause())
  * out_of_scope     — see note below; uses notify_only(), NOT pause()

`out_of_scope` (e.g. "yesterday my hair got washed") is treated as a single
silent skip + owner notification, NOT a pause. The customer's next message
about booking should still be answered by the AI.

State lives on the customer_conversations Firestore document under:

  aiPaused        : bool
  aiPausedAt      : ISO-8601 UTC
  aiPausedUntil   : ISO-8601 UTC (now + DEFAULT_PAUSE_MINUTES)
  aiPauseReason   : str (one of the values above)
  aiPauseSnippet  : str (≤120 chars of the message that triggered pause; for the
                         owner notification, NOT for analytics dashboards)

Storing on the conversation doc means the pause check costs zero extra reads —
the doc is loaded on every incoming message anyway.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app import firestore as db
from app.config import settings
from app.integrations import posthog_client

logger = logging.getLogger(__name__)

# Default pause window. The user requested 90 minutes; centralised here so it
# can be tuned without grepping the codebase.
DEFAULT_PAUSE_MINUTES = 90


class PauseReason(str, Enum):
    OWNER_TAKEOVER = "owner_takeover"
    FRUSTRATION    = "frustration"
    ABUSE          = "abuse"
    MANUAL         = "manual"


def _digits(value: str | None) -> str:
    """Strip every non-digit (including '+' and '@s.whatsapp.net' suffix)."""
    if not value:
        return ""
    head = str(value).split("@", 1)[0]
    return "".join(ch for ch in head if ch.isdigit())


def is_protected_number(phone: str, business: dict | None = None) -> bool:
    """True when `phone` must never be paused or treated as a customer.

    Covers:
      * the Recepte global onboarding number (every SMB owner has a chat
        with it — owner-replies in that thread are messages *to us*, never
        to a real customer of theirs)
      * the configured ``RECEPTE_PHONE`` (legacy default)
      * the business owner's own WhatsApp number (a self-chat or admin
        message must not pause the owner's own AI on themselves)
      * any registered admin phone of the business

    Uses last-10-digit comparison so country-code variants ("+91…",
    "0091…", bare "91…") all match.
    """
    target = _digits(phone)
    if not target:
        return False
    candidates: list[str] = []
    if settings.WHATSMEOW_GLOBAL_NUMBER:
        candidates.append(settings.WHATSMEOW_GLOBAL_NUMBER)
    if settings.RECEPTE_PHONE:
        candidates.append(settings.RECEPTE_PHONE)
    if business:
        # Per-business stored copies of the global onboarding number. We accept
        # several aliases so the field can be populated by any onboarding path
        # (server-side handler, dashboard, manual support edit) without forcing
        # everyone onto a single schema migration.
        for key in (
            "ownerPhone", "owner_phone", "waPhoneNumber",
            "globalOnboardingNumber", "onboardedViaPhone", "recepteNumber",
        ):
            if business.get(key):
                candidates.append(business[key])
        for admin in business.get("adminPhones") or []:
            candidates.append(admin)
    for raw in candidates:
        cand = _digits(raw)
        if not cand:
            continue
        if cand == target:
            return True
        # last-10-digit fallback (country-code-flexible)
        if len(cand) >= 10 and len(target) >= 10 and cand[-10:] == target[-10:]:
            return True
    return False


@dataclass(frozen=True)
class PauseState:
    paused: bool
    reason: str | None
    paused_at: datetime | None
    paused_until: datetime | None

    @property
    def expired(self) -> bool:
        return (
            self.paused
            and self.paused_until is not None
            and self.paused_until <= _now()
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def read_state(convo: dict | None) -> PauseState:
    """Read the pause state from an already-loaded conversation dict.

    Passing the dict (vs reloading from Firestore) keeps the hot path free of
    extra reads — callers always have the conversation in hand.
    """
    if not convo:
        return PauseState(paused=False, reason=None, paused_at=None, paused_until=None)
    return PauseState(
        paused=bool(convo.get("aiPaused")),
        reason=convo.get("aiPauseReason"),
        paused_at=_parse_iso(convo.get("aiPausedAt")),
        paused_until=_parse_iso(convo.get("aiPausedUntil")),
    )


def is_active_pause(convo: dict | None) -> bool:
    """True iff the conversation is currently in an unexpired pause."""
    state = read_state(convo)
    return state.paused and not state.expired


def pause(
    business_id: str,
    customer_phone: str,
    reason: PauseReason,
    snippet: str = "",
    minutes: int = DEFAULT_PAUSE_MINUTES,
    business: dict | None = None,
) -> PauseState:
    """Pause AI replies for one customer of one business.

    Idempotent — re-pausing extends the window. `snippet` is a short excerpt
    of the message that triggered the pause; it is included in the owner
    notification but not used for anything else.

    Hard refuses to pause `is_protected_number(customer_phone, business)` —
    the Recepte global, the owner's own number, and admin phones must
    never accidentally become "paused customers".
    """
    if is_protected_number(customer_phone, business):
        logger.warning(
            "[AI-PAUSE] REFUSED — phone=%s is a protected number (global/owner/admin); "
            "business=%s reason=%s",
            customer_phone, business_id, reason.value,
        )
        return PauseState(paused=False, reason=None, paused_at=None, paused_until=None)

    now = _now()
    until = now + timedelta(minutes=minutes)
    patch = {
        "aiPaused":       True,
        "aiPausedAt":     now.isoformat(),
        "aiPausedUntil":  until.isoformat(),
        "aiPauseReason":  reason.value,
        "aiPauseSnippet": (snippet or "")[:120],
    }
    db.upsert_customer_conversation(business_id, customer_phone, patch)
    logger.info(
        "[AI-PAUSE] business=%s phone=%s reason=%s until=%s",
        business_id, customer_phone, reason.value, until.isoformat(),
    )
    try:
        posthog_client.capture(
            business_id=business_id,
            customer_phone=customer_phone,
            event="ai_paused",
            properties={"reason": reason.value, "minutes": minutes},
        )
    except Exception:
        pass
    return PauseState(
        paused=True,
        reason=reason.value,
        paused_at=now,
        paused_until=until,
    )


def resume(business_id: str, customer_phone: str) -> None:
    """Clear an active pause. No-op if there was no pause."""
    patch = {
        "aiPaused":       False,
        "aiPausedUntil":  None,
        "aiPauseReason":  None,
        "aiPauseSnippet": None,
    }
    db.upsert_customer_conversation(business_id, customer_phone, patch)
    logger.info("[AI-RESUME] business=%s phone=%s", business_id, customer_phone)
    try:
        posthog_client.capture(
            business_id=business_id,
            customer_phone=customer_phone,
            event="ai_resumed",
            properties={},
        )
    except Exception:
        pass


# ── Resume-command detection ─────────────────────────────────────────────────
# Owner manual-resume command. Accepts:
#   "resume"                  — bare keyword
#   "resume ai" / "resume bot"
#   "resume 919905252720"     — explicit customer phone
#   "resume ai 919905252720"
# Plain prose like "please resume the booking" MUST NOT match — the regex
# anchors the verb at the START of the trimmed message and only allows the
# whitelisted continuations.
_RESUME_RE = re.compile(
    r"^\s*resume(?:\s+(?:ai|bot))?(?:\s+\+?\d{7,15})?\s*$",
    re.IGNORECASE,
)


def _normalize_resume_text(text: str) -> str:
    """Collapse spaces between digit groups so '9190683 19516' → '919068319516'."""
    return re.sub(r'(?<=\d)\s+(?=\d)', '', text)


def is_resume_command(text: str) -> bool:
    """True iff text is the owner resume command (with or without a phone arg)."""
    if not text:
        return False
    return bool(_RESUME_RE.match(_normalize_resume_text(text)))


def extract_resume_phone(text: str) -> str | None:
    """Return the digits-only phone from a `resume … <phone>` command, or None."""
    if not text:
        return None
    m = re.match(
        r"^\s*resume(?:\s+(?:ai|bot))?\s+\+?(\d{7,15})\s*$",
        _normalize_resume_text(text),
        re.IGNORECASE,
    )
    return m.group(1) if m else None
