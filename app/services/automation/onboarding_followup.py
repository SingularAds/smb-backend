"""Onboarding drop-off follow-ups.

Owners regularly start onboarding and then go quiet mid-flow — before sharing
their details, before pairing WhatsApp, etc. This sweep sends a small, friendly
nudge inviting them to pick up where they left off.

Deliberately restrained (frequent nudges annoy and get people to block us):
  • Exactly TWO nudges per session, ever.
      – 1st after ``ONBOARDING_FOLLOWUP_1_DELAY_MIN``  of owner silence (1 h).
      – 2nd after ``ONBOARDING_FOLLOWUP_2_DELAY_MIN``  of owner silence (18 h).
  • Both windows sit inside WhatsApp's 24-h customer-care window (the owner
    messaged us first), so these are safe, non-template session messages — no
    anti-ban gating required.
  • Never nudges a completed / finalized / demo session, and never an abandoned
    one older than ``ONBOARDING_FOLLOWUP_MAX_AGE_MIN``.
  • At-most-once delivery: the stage stamp is written BEFORE sending, so a crash
    or double sweep can only ever DROP a nudge, never duplicate one.

Idle time is measured from ``lastInboundAt`` (stamped on every inbound owner
message), NOT from our own sends — so sending nudge #1 never resets the clock
for nudge #2.

Wired into the scheduler in app/services/automation/scheduler.py.
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

# Steps we never nudge:
#   • complete / post_onboarding — onboarding is finished.
#   • demo_salao_bella          — a demo roleplay, not real onboarding.
#   • plan_selection / new_biz_confirm — these belong to EXISTING customers
#     (post-trial billing recovery, duplicate-business guard), so an
#     "finish your onboarding" nudge would be wrong.
# Every other non-terminal step (discovery, pairing, calendar_setup, …) is a
# genuine mid-onboarding drop-off and IS eligible for a nudge.
_SKIP_STEPS = {
    "complete", "post_onboarding", "demo_salao_bella",
    "plan_selection", "new_biz_confirm",
}

# Hand-written copy per language — trust/tone copy is never machine-translated.
# ``{name}`` expands to a leading-space first name (" João") or "" so both
# "Hi João!" and "Hi!" read naturally. Falls back to English.
_FOLLOWUP_1: dict[str, str] = {
    "en": (
        "👋 Hi{name}! Sofia here. We started setting up your AI receptionist but "
        "didn't quite finish 🙂\n\nNo rush at all — whenever you have a moment, "
        "just reply here and we'll pick up right where we left off."
    ),
    "pt": (
        "👋 Oi{name}! Aqui é a Sofia. A gente começou a configurar o seu atendente "
        "de IA, mas não chegou a terminar 🙂\n\nSem pressa — quando puder, é só "
        "responder aqui que a gente continua de onde parou."
    ),
    "es": (
        "👋 ¡Hola{name}! Soy Sofia. Empezamos a configurar tu recepcionista con IA "
        "pero no llegamos a terminar 🙂\n\nSin prisa — cuando puedas, responde aquí "
        "y seguimos justo donde lo dejamos."
    ),
}

_FOLLOWUP_2: dict[str, str] = {
    "en": (
        "👋 Hey{name}, just checking in one last time. Your AI receptionist is still "
        "ready to finish whenever you are — it only takes a couple of minutes.\n\n"
        "Reply here anytime and I'll walk you through it. 💬"
    ),
    "pt": (
        "👋 Oi{name}, passando só mais uma vez pra dar um oi. Seu atendente de IA "
        "continua prontinho pra terminar quando você quiser — leva só uns minutinhos."
        "\n\nÉ só responder aqui que eu te ajudo. 💬"
    ),
    "es": (
        "👋 Hola{name}, paso una última vez a saludarte. Tu recepcionista con IA "
        "sigue listo para terminar cuando quieras — son solo un par de minutos.\n\n"
        "Responde aquí cuando puedas y te acompaño. 💬"
    ),
}


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


def _first_name(session: dict) -> str:
    raw = (session.get("pushName") or "").strip()
    if not raw:
        return ""
    first = raw.split()[0]
    # Guard against phone-number-ish or junk push names.
    if len(first) < 2 or first.isdigit():
        return ""
    return f" {first}"


def _message_for(stage: int, session: dict) -> str:
    lang = str(session.get("language") or "en")[:2].lower()
    table = _FOLLOWUP_1 if stage == 1 else _FOLLOWUP_2
    template = table.get(lang) or table["en"]
    return template.format(name=_first_name(session))


async def run_onboarding_followup_sweep() -> None:
    """Send drop-off nudges to owners who went quiet mid-onboarding."""
    if not settings.ONBOARDING_FOLLOWUP_ENABLED:
        return

    now = _now()
    delay1 = timedelta(minutes=settings.ONBOARDING_FOLLOWUP_1_DELAY_MIN)
    delay2 = timedelta(minutes=settings.ONBOARDING_FOLLOWUP_2_DELAY_MIN)
    max_age = timedelta(minutes=settings.ONBOARDING_FOLLOWUP_MAX_AGE_MIN)

    # Candidates: last owner message between (now - max_age) and (now - delay1).
    # i.e. silent for at least the 1st-nudge delay, but not so long we consider
    # them abandoned. The stage/idle checks below pick the exact nudge.
    #
    # NOTE: lastInboundAt is stored as a NAIVE UTC ISO string (matching the rest
    # of the onboarding code, which uses datetime.utcnow().isoformat()). Firestore
    # range comparison on these is lexicographic, so the query bounds MUST also be
    # naive — a tz-aware "+00:00" suffix would sort inconsistently and silently
    # break the scan. Idle math below stays tz-aware (see _parse_dt) and is exact.
    now_naive = now.replace(tzinfo=None)
    start_iso = (now_naive - max_age).isoformat()
    end_iso = (now_naive - delay1).isoformat()

    try:
        sessions = db.list_onboarding_sessions_active_between(start_iso, end_iso, limit=1000)
    except Exception as exc:
        logger.exception("[ONB-FOLLOWUP] Failed to list candidate sessions: %s", exc)
        return

    sent = 0
    for session in sessions:
        try:
            if await _process_session(session, now, delay1, delay2):
                sent += 1
        except Exception as exc:
            logger.exception(
                "[ONB-FOLLOWUP] Error processing session=%s: %s", session.get("id"), exc,
            )

    logger.info(
        "[ONB-FOLLOWUP] Sweep complete — %d nudge(s) sent (checked %d candidate session(s))",
        sent, len(sessions),
    )


async def _process_session(
    session: dict,
    now: datetime,
    delay1: timedelta,
    delay2: timedelta,
) -> bool:
    phone = session.get("id") or session.get("ownerPhone") or ""
    if not phone:
        return False

    step = session.get("currentStep") or ""
    # Only truly-finished (or demo) sessions are excluded. Mid-setup steps like
    # pairing / calendar_setup / call_forwarding DO get nudged — "got the pairing
    # code but never linked WhatsApp" is one of the most valuable drop-offs to
    # recover, and the copy ("let's pick up where we left off") fits it.
    if step in _SKIP_STEPS:
        return False
    if session.get("followupSuppressed"):
        return False

    last_in = _parse_dt(session.get("lastInboundAt"))
    if last_in is None:
        return False  # can't measure idle — the query shouldn't return these anyway
    idle = now - last_in

    stage = int(session.get("followupStage") or 0)
    if stage >= 2:
        return False

    if stage == 0 and idle >= delay1:
        target_stage = 1
        stamp_field = "followup1SentAt"
    elif stage == 1 and idle >= delay2:
        target_stage = 2
        stamp_field = "followup2SentAt"
    else:
        return False

    message = _message_for(target_stage, session)
    device = session.get("onboardingDeviceId") or None

    # Stamp BEFORE sending → at-most-once (prefer a missed nudge over a duplicate).
    now_iso = now.isoformat()
    try:
        db.upsert_onboarding_session(phone, {
            "followupStage": target_stage,
            stamp_field: now_iso,
        })
    except Exception:
        logger.exception("[ONB-FOLLOWUP] Failed to stamp stage for %s — skipping send", phone)
        return False

    try:
        await _wa.send_message(phone, message, device_id=device)
    except Exception as exc:
        # Already stamped; we intentionally do NOT retry (avoids duplicate nudges).
        logger.warning(
            "[ONB-FOLLOWUP] Send failed for %s (stage %d): %s — nudge dropped",
            phone, target_stage, exc,
        )
        return False

    try:
        transcript.record_message(
            phone, "assistant", message, step=step,
        )
    except Exception:
        pass

    logger.info(
        "[ONB-FOLLOWUP] Sent nudge #%d to %s (step=%s, idle=%dm)",
        target_stage, phone, step, int(idle.total_seconds() // 60),
    )
    return True
