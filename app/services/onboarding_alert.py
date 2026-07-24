"""Human-support alert for stuck onboarding conversations.

When an onboarding conversation gets genuinely stuck — the owner cannot share
their location after repeated tries, a business-details lookup keeps failing, or
the owner explicitly asks for a human — Sofia pings a configured human-support
WhatsApp number (``settings.ALERT_NUMBER``) with the client's phone number and a
one-line summary, so the agent can open that chat and help out.

Everything here is fully OPTIONAL and self-contained:
  • If ``ALERT_NUMBER`` is not set, every function is a cheap no-op — behaviour
    is exactly as it was before.
  • All sends are best-effort; a failure is logged and swallowed so an alerting
    hiccup can never break onboarding.
  • De-duplicated per session: at most one alert per ``_COOLDOWN`` window and at
    most ``_MAX_ALERTS_PER_SESSION`` alerts for a given owner, so a stuck owner
    never spams the support agent.

The design intentionally mirrors the existing Telegram ``_daniel_handoff`` path
but delivers over WhatsApp to a client-configurable number.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app import firestore as db
from app.config import settings
from app.services.whatsmeow_client import WhatsmeowClient
from app.services import onboarding_transcript as transcript

logger = logging.getLogger(__name__)

_wa = WhatsmeowClient()

# How many consecutive "no-progress" signals before we escalate. Kept at 2 so a
# single hiccup never pages a human, but a real loop does.
_STUCK_THRESHOLD = 2
# Minimum gap between two alerts for the same session.
_COOLDOWN = timedelta(hours=6)
# Absolute ceiling per session, so a permanently-stuck owner can't page forever.
_MAX_ALERTS_PER_SESSION = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _last_owner_message(session: dict) -> str:
    history = session.get("conversationHistory") or []
    for turn in reversed(history):
        if turn.get("role") == "user":
            text = (turn.get("content") or "").strip()
            if text:
                return text[:300]
    return ""


def _build_alert_text(phone: str, session: dict, reason: str) -> str:
    push = session.get("pushName") or "—"
    step = session.get("currentStep") or "—"
    lang = session.get("language") or "—"
    onboarding_number = session.get("onboardingNumber") or ""
    last_msg = _last_owner_message(session)

    lines = [
        "🆘 *Onboarding needs a human*",
        "",
        f"Client: {push} (+{phone})",
    ]
    if onboarding_number:
        lines.append(f"On number: +{onboarding_number}")
    lines.append(f"Step: {step}  |  Lang: {lang}")
    lines.append("")
    lines.append(f"Issue: {reason}")
    if last_msg:
        lines.append("")
        lines.append(f'Last message from client: "{last_msg}"')
    lines.append("")
    lines.append(f"➡️ Open a WhatsApp chat with +{phone} to step in and help.")
    return "\n".join(lines)


async def maybe_alert_human(
    phone: str,
    session: dict,
    reason: str,
    *,
    force: bool = False,
) -> bool:
    """Alert the human-support number about a stuck onboarding conversation.

    Returns True if an alert was actually sent. No-ops (returns False) when
    ``ALERT_NUMBER`` is unset, or when the per-session cooldown / ceiling has
    been hit (unless ``force`` is set, which still respects the hard ceiling).
    """
    alert_number = (settings.ALERT_NUMBER or "").strip()
    if not alert_number:
        return False

    if not phone:
        return False

    count = int(session.get("humanAlertCount") or 0)
    if count >= _MAX_ALERTS_PER_SESSION:
        logger.info(
            "[ALERT] Ceiling reached for %s (%d alerts) — skipping (reason=%s)",
            phone, count, reason,
        )
        return False

    last_at = _parse_dt(session.get("humanAlertLastAt"))
    if not force and last_at is not None and (_now() - last_at) < _COOLDOWN:
        logger.info(
            "[ALERT] Within cooldown for %s (last=%s) — skipping (reason=%s)",
            phone, session.get("humanAlertLastAt"), reason,
        )
        return False

    text = _build_alert_text(phone, session, reason)

    # Send FROM the same onboarding number the client is on (falls back to the
    # global default device) so the agent sees the alert in the right inbox.
    device = session.get("onboardingDeviceId") or None
    try:
        await _wa.send_message(alert_number, text, device_id=device)
    except Exception as exc:  # never let an alerting failure break onboarding
        logger.warning("[ALERT] Failed to send human alert for %s: %s", phone, exc)
        return False

    stamp = _now().isoformat()
    updates = {
        "humanAlertLastAt": stamp,
        "humanAlertCount": count + 1,
        "humanAlertLastReason": reason[:300],
        # Clear the running stuck counter — we've acted on it.
        "stuckCount": 0,
    }
    try:
        db.upsert_onboarding_session(phone, updates)
    except Exception:
        logger.exception("[ALERT] Failed to persist alert stamp for %s", phone)
    session.update(updates)

    # Record it on the client's transcript so the dashboard shows an escalation
    # happened (as an internal/system note, not a message to the client).
    try:
        transcript.record_message(
            phone, "assistant", f"[human-support alerted] {reason}",
            step=session.get("currentStep"),
        )
    except Exception:
        pass

    logger.info("[ALERT] Human support alerted for %s (reason=%s)", phone, reason)
    return True


async def note_stuck(phone: str, session: dict, reason: str) -> bool:
    """Record a 'no forward progress' signal; escalate once it repeats.

    Increments a per-session ``stuckCount``. When it reaches ``_STUCK_THRESHOLD``
    an alert is raised (and the counter reset). Returns True if an alert fired.
    Safe no-op when ``ALERT_NUMBER`` is unset.
    """
    if not (settings.ALERT_NUMBER or "").strip():
        return False

    count = int(session.get("stuckCount") or 0) + 1
    session["stuckCount"] = count
    try:
        db.upsert_onboarding_session(phone, {"stuckCount": count})
    except Exception:
        logger.exception("[ALERT] Failed to persist stuckCount for %s", phone)

    if count >= _STUCK_THRESHOLD:
        return await maybe_alert_human(phone, session, reason)
    return False


async def note_progress(phone: str, session: dict) -> None:
    """Clear the stuck counter after a clear sign of forward progress.

    Best-effort and cheap: only writes when there is actually a non-zero counter
    to reset, so this adds no Firestore traffic on the happy path.
    """
    if not int(session.get("stuckCount") or 0):
        return
    session["stuckCount"] = 0
    try:
        db.upsert_onboarding_session(phone, {"stuckCount": 0})
    except Exception:
        logger.exception("[ALERT] Failed to reset stuckCount for %s", phone)
