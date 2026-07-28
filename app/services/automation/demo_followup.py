"""End-of-demo "About us" idle follow-up (client 2026-07-28).

After the Salão Bella demo ends we offer to pair the prospect's WhatsApp right in
the demo chat. Some prospects read the offer and go quiet without connecting.
This sweep sends them, ONCE, the "about us / recepte.co + human-support" message
so the thread never dead-ends and they know a human can help.

Restrained + at-most-once:
  • Only sessions that reached the end (``demoGuidanceSent``) and did NOT connect
    (``demoPairingMode`` != "paired") and haven't already been sent it
    (``demoAboutUsSent``).
  • Fires ``DEMO_ABOUTUS_IDLE_MIN`` minutes after the prospect's last activity.
  • The ``demoAboutUsSent`` stamp is written BEFORE sending, so a crash or a double
    sweep can only ever DROP the message, never duplicate it.
  • Sets ``awaitingHumanSupportAnswer`` so a following bare "yes" loops in a human
    (handled by the demo turn loop in onboarding_service).

Wired into the scheduler in app/services/automation/scheduler.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app import firestore as db
from app.config import settings
from app.services.whatsmeow_client import WhatsmeowClient

logger = logging.getLogger(__name__)

_wa = WhatsmeowClient()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_demo_aboutus_sweep() -> None:
    """Send the About-us / human-support message to prospects who were offered
    in-chat pairing at the end of the demo and then went idle without connecting."""
    if not settings.DEMO_ABOUTUS_ENABLED:
        return
    if not (settings.DEMO_WA_DEVICE_ID or "").strip():
        return  # demo number not configured — nothing to send from

    # The About-us copy lives with the rest of the demo strings; import it lazily
    # so this automation module has no import-time dependency on the (heavy)
    # onboarding service and can never introduce an import cycle.
    try:
        from app.services.onboarding_service import _DEMO_ABOUTUS, _demo_text
    except Exception:
        logger.exception("[DEMO-ABOUTUS] could not import demo copy — skipping sweep")
        return

    now = _now()
    idle = timedelta(minutes=settings.DEMO_ABOUTUS_IDLE_MIN)
    max_age = timedelta(minutes=settings.DEMO_ABOUTUS_MAX_AGE_MIN)

    # lastActivityAt is a NAIVE UTC ISO string (datetime.utcnow().isoformat());
    # Firestore range comparison on these is lexicographic, so the query bounds
    # MUST be naive too (same rule the onboarding follow-up sweep documents).
    now_naive = now.replace(tzinfo=None)
    start_iso = (now_naive - max_age).isoformat()
    end_iso = (now_naive - idle).isoformat()

    try:
        sessions = db.list_demo_sessions_active_between(start_iso, end_iso, limit=1000)
    except Exception as exc:
        logger.exception("[DEMO-ABOUTUS] failed to list demo sessions: %s", exc)
        return

    device = settings.DEMO_WA_DEVICE_ID or _wa.default_device_id
    sent = 0
    for session in sessions:
        try:
            if not session.get("demoGuidanceSent"):
                continue  # never reached the end-of-demo pairing offer
            if session.get("demoPairingMode") == "paired":
                continue  # they connected — nothing to nudge
            if session.get("demoAboutUsSent"):
                continue  # already sent once

            phone = session.get("id") or session.get("phone") or ""
            if not phone:
                continue
            lang = str(session.get("language") or "pt")[:2].lower()

            # Stamp BEFORE sending → at-most-once (prefer a missed message over a
            # duplicate). awaitingHumanSupportAnswer lets a following "yes" page a human.
            try:
                db.upsert_demo_session(phone, {
                    "demoAboutUsSent": True,
                    "awaitingHumanSupportAnswer": True,
                })
            except Exception:
                logger.exception("[DEMO-ABOUTUS] stamp failed for %s — skipping", phone)
                continue

            try:
                await _wa.send_message(phone, _demo_text(_DEMO_ABOUTUS, lang), device_id=device)
                sent += 1
            except Exception as exc:
                logger.warning("[DEMO-ABOUTUS] send failed for %s: %s — dropped", phone, exc)
        except Exception as exc:
            logger.exception("[DEMO-ABOUTUS] error processing %s: %s", session.get("id"), exc)

    logger.info(
        "[DEMO-ABOUTUS] sweep complete — %d message(s) sent (checked %d candidate session(s))",
        sent, len(sessions),
    )
