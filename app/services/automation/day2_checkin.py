"""Day-2 trust check-in — client trust spec item 12.

Two days after the trial starts (i.e. after WhatsApp was connected), Sofia
sends the owner ONE friendly check-in that deliberately repeats the
"you can disconnect anytime" reminder. Counterintuitively, reminding owners
that they are free to leave builds trust — people who feel free stay.

Rules:
  • Sent exactly once per business (guarded by ``day2CheckinSentAt``).
  • Only for businesses with a linked WhatsApp (the reminder makes no sense
    otherwise) and an active trial start stamp.
  • Window: 2–4 days after trialStartedAt. Older businesses are skipped so
    enabling this job never spams the existing install base.
  • Respects ``suppressTrialReminders`` (the owner's STOP opt-out).
  • Copy is hand-written per language (en/pt/es) — trust copy must read
    perfectly, no machine translation. Falls back to English.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import firestore as db
from app.config import settings
from app.services.automation.whatsapp_notifier import _wa

logger = logging.getLogger(__name__)

_CHECKIN_FIELD = "day2CheckinSentAt"

# Hand-written copy — PT-BR is the primary market (client-approved tone).
_CHECKIN_MESSAGES: dict[str, str] = {
    "en": (
        "👋 Hi! Sofia here — quick day-2 check-in.\n\n"
        "I'm on duty answering your customers 24/7, and you can see everything "
        "I do right in your WhatsApp chats.\n\n"
        "And remember — you're always in control: you can disconnect me anytime, "
        "in 2 taps, in WhatsApp → Settings → Linked Devices.\n\n"
        "Need anything or want to change something? Just reply here 😊"
    ),
    "pt": (
        "👋 Oi! Sofia aqui — passando pra dar um oi no dia 2.\n\n"
        "Estou de plantão atendendo seus clientes 24/7, e você vê tudo o que eu "
        "faço direto nas suas conversas do WhatsApp.\n\n"
        "E lembra — você está sempre no controle: pode me desconectar quando "
        "quiser, em 2 toques, em WhatsApp → Configurações → Aparelhos conectados.\n\n"
        "Precisa de algo ou quer mudar alguma coisa? É só responder aqui 😊"
    ),
    "es": (
        "👋 ¡Hola! Sofia aquí — un saludo rápido del día 2.\n\n"
        "Estoy de guardia atendiendo a tus clientes 24/7, y puedes ver todo lo "
        "que hago directamente en tus chats de WhatsApp.\n\n"
        "Y recuerda — siempre tienes el control: puedes desconectarme cuando "
        "quieras, en 2 toques, en WhatsApp → Ajustes → Dispositivos vinculados.\n\n"
        "¿Necesitas algo o quieres cambiar alguna cosa? Solo responde aquí 😊"
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _checkin_message(business: dict) -> str:
    lang = str(business.get("primaryLanguage") or "en")[:2].lower()
    return _CHECKIN_MESSAGES.get(lang) or _CHECKIN_MESSAGES["en"]


async def run_day2_checkin_sweep() -> None:
    """Daily job: send the one-shot day-2 trust check-in where due."""
    now = _now()
    businesses = db.list_active_businesses(limit=500)
    sent = 0

    for biz in businesses:
        try:
            if await _process_single_business(biz, now):
                sent += 1
        except Exception as exc:
            logger.exception(
                "[DAY2-CHECKIN] Error processing business=%s: %s", biz.get("id"), exc,
            )

    logger.info(
        "[DAY2-CHECKIN] Sweep complete — %d check-ins sent (checked %d businesses)",
        sent, len(businesses),
    )


async def _process_single_business(business: dict, now: datetime) -> bool:
    biz_id = business.get("id") or ""
    if not biz_id:
        return False
    if business.get(_CHECKIN_FIELD):
        return False  # already sent — one-shot only
    if business.get("suppressTrialReminders"):
        return False  # owner opted out of reminders
    if not business.get("waSessionId"):
        return False  # WhatsApp never linked — the disconnect reminder makes no sense

    trial_start = _parse_dt(business.get("trialStartedAt"))
    if not trial_start:
        return False

    days = (now - trial_start).total_seconds() / 86400
    # 2–4 day window: due on day 2, and a grace day in case a sweep was missed.
    # Anything older is skipped so first deploy never spams existing businesses.
    if days < 2 or days >= 4:
        return False

    owner_phone = business.get("ownerPhone") or ""
    if not owner_phone:
        return False

    msg = _checkin_message(business)

    # Send from the global onboarding device — this is Sofia→owner (same chat
    # where onboarding happened), not the business's customer-facing number.
    onboarding_device = (
        settings.WHATSMEOW_ONBOARDING_DEVICE_ID or settings.WHATSMEOW_DEFAULT_DEVICE_ID
    )
    try:
        await _wa.send_message(owner_phone, msg, device_id=onboarding_device)
    except Exception as exc:
        logger.warning(
            "[DAY2-CHECKIN] Send failed for owner=%s biz=%s: %s — will retry next sweep",
            owner_phone, biz_id, exc,
        )
        return False

    db.update_business_doc(biz_id, {_CHECKIN_FIELD: now.isoformat()})
    logger.info("[DAY2-CHECKIN] Sent to owner=%s biz=%s", owner_phone, biz_id)
    return True
