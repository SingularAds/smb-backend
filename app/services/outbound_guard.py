"""Outbound Guard — anti-ban send discipline for PROACTIVE WhatsApp messages.

WhatsApp bans numbers based primarily on WHO you message (strangers vs. people
who message you first), block/report rate, and robotic sending patterns. This
module is the single gate every *proactive* (business-initiated,
marketing-class) message must pass through:

    referral invites, CSAT prompts, and all future campaign mechanics
    (win-back, slot-fill, rebook, review asks).

It deliberately does NOT gate transactional traffic — AI replies to inbound
messages, booking confirmations, reminders, cancellation notices — because
those are customer-initiated and are WhatsApp's safest traffic class; delaying
or dropping them would hurt the product without reducing ban risk.

Checks applied to every proactive send (cheapest first):

  1. kill switch          — settings.WA_OUTBOUND_GUARD_ENABLED
  2. opt-out              — customer.marketingOptOut is permanent suppression
  3. 463 circuit breaker  — device is cooling down after a WhatsApp
                            "reachout time-locked" rejection
  4. number warm-up       — no proactive sends for WA_WARMUP_DAYS after a
                            fresh pairing (business.waPairedAt)
  5. business hours       — only 09:00-20:00 in the business's timezone
  6. touch budget         — max WA_TOUCH_BUDGET_PER_30D proactive messages
                            per contact per rolling 30 days, across ALL
                            mechanics (customer.proactiveTouches)
  7. daily cap            — max WA_PROACTIVE_DAILY_CAP proactive messages per
                            device per calendar day (wa_device_state)
  8. jitter               — randomized human-like delay before the send

On success the guard stamps the contact touch + device counter so every
mechanic automatically shares one budget. On WhatsApp 463 it opens the
device circuit breaker instead of retrying.

State layout (Firestore):
  customers/{phone}:           marketingOptOut, marketingOptOutAt,
                               lastProactiveAt, proactiveTouches[]
  wa_device_state/{device}:            cooldownUntil, last463At, count463Total
  wa_device_state/{device}_{YYYYMMDD}: proactiveCount   (atomic Increment)

Scalability note: the daily counter uses Firestore's atomic Increment on a
per-device-per-day doc (no read-modify-write race, no reset logic, safe with
multiple backend instances). The cap check itself is read-then-send — a small
overshoot under heavy concurrency is acceptable for an anti-ban ceiling.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import firestore as db
from app.config import settings
from app.integrations import posthog_client
from app.services.tz_utils import biz_tz
from app.services.whatsmeow_client import ReachoutTimelocked, WhatsmeowClient

logger = logging.getLogger(__name__)

_wa = WhatsmeowClient()


# ── result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GuardResult:
    """Outcome of a guarded proactive send.

    sent      — message was delivered to the bridge successfully.
    reason    — machine-readable block/send reason (for logs + metrics).
    permanent — True when retrying later can never succeed (e.g. opt-out);
                sweeps should mark the work item done instead of retrying.
    """
    sent: bool
    reason: str
    permanent: bool = False

    @property
    def blocked(self) -> bool:
        return not self.sent


# ── small helpers ─────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _device_for(business: dict) -> str:
    return business.get("waSessionId") or _wa.default_device_id or "default"


def _day_key(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


# ── individual checks (each returns a block-reason or "") ────────────────────

def _check_opt_out(customer: dict | None) -> str:
    if customer and customer.get("marketingOptOut"):
        return "opted_out"
    return ""


def _check_cooldown(device_id: str) -> str:
    state = db.get_wa_device_state(device_id) or {}
    until = _parse_iso(state.get("cooldownUntil"))
    if until and until > _now():
        return "device_cooldown"
    return ""


def _check_warmup(business: dict) -> str:
    """Fresh pairings must not send proactive messages for WA_WARMUP_DAYS.

    Businesses without waPairedAt predate the field and are treated as warmed
    (never block existing production tenants on a missing field).
    """
    paired_at = _parse_iso(business.get("waPairedAt"))
    if paired_at and _now() - paired_at < timedelta(days=settings.WA_WARMUP_DAYS):
        return "warming_up"
    return ""


def _check_business_hours(business: dict) -> str:
    try:
        local_now = _now().astimezone(biz_tz(business))
    except Exception:  # bad tz config must never break sends entirely
        local_now = _now()
    if not (settings.WA_PROACTIVE_HOUR_START <= local_now.hour < settings.WA_PROACTIVE_HOUR_END):
        return "outside_business_hours"
    return ""


def _recent_touches(customer: dict | None) -> list[str]:
    """The contact's proactive touches within the rolling 30-day window."""
    cutoff = _now() - timedelta(days=30)
    out: list[str] = []
    for raw in (customer or {}).get("proactiveTouches") or []:
        dt = _parse_iso(raw)
        if dt and dt >= cutoff:
            out.append(raw)
    return out


def _check_touch_budget(customer: dict | None) -> str:
    if len(_recent_touches(customer)) >= settings.WA_TOUCH_BUDGET_PER_30D:
        return "touch_budget_exhausted"
    return ""


def _check_daily_cap(device_id: str) -> str:
    count = db.get_wa_daily_count(device_id, _day_key(_now()))
    if count >= settings.WA_PROACTIVE_DAILY_CAP:
        return "daily_cap_reached"
    return ""


# ── 463 circuit breaker ───────────────────────────────────────────────────────

def _open_circuit_breaker(device_id: str, retry_after_s: int) -> None:
    """WhatsApp rejected a send with 463 — cool the whole device down.

    463 is an anti-abuse signal, not a transient error; hammering through it
    is exactly the pattern that escalates to a ban.
    """
    cooldown_s = max(int(retry_after_s or 0), settings.WA_463_COOLDOWN_MIN_S)
    until = _now() + timedelta(seconds=cooldown_s)
    state = db.get_wa_device_state(device_id) or {}
    db.upsert_wa_device_state(device_id, {
        "cooldownUntil": until.isoformat(),
        "last463At": _now().isoformat(),
        "count463Total": int(state.get("count463Total") or 0) + 1,
    })
    logger.warning(
        "[OUTBOUND-GUARD] 463 circuit breaker OPEN device=%s until=%s (total 463s: %s)",
        device_id, until.isoformat(), int(state.get("count463Total") or 0) + 1,
    )


# ── success bookkeeping ───────────────────────────────────────────────────────

def _stamp_touch(business_id: str, customer_phone: str, customer: dict | None, mechanic: str) -> None:
    now_iso = _now().isoformat()
    touches = _recent_touches(customer)  # prunes anything older than 30d
    touches.append(now_iso)
    db.upsert_customer(business_id, customer_phone, {
        "lastProactiveAt": now_iso,
        "lastProactiveMechanic": mechanic,
        "proactiveTouches": touches,
    })


def _track(business_id: str, customer_phone: str, event: str, properties: dict) -> None:
    try:
        posthog_client.capture(
            business_id=business_id,
            customer_phone=customer_phone,
            event=event,
            properties=properties,
        )
    except Exception:
        pass


# ── public API ────────────────────────────────────────────────────────────────

async def send_proactive(
    business: dict,
    customer_phone: str,
    message: str,
    mechanic: str,
) -> GuardResult:
    """Send a proactive (business-initiated) WhatsApp message through the
    anti-ban discipline gate.

    `mechanic` names the feature sending the message ("referral_invite",
    "csat_prompt", "win_back", …) — recorded on the touch and in metrics so
    all mechanics share one per-contact budget and per-device cap.

    Returns a GuardResult; never raises for guard blocks. Bridge/network
    errors other than 463 propagate to the caller like a direct send would.
    """
    business_id = business.get("id", "")
    device_id = _device_for(business)

    if not settings.WA_OUTBOUND_GUARD_ENABLED:
        # Kill switch: behave like the legacy direct send.
        await _wa.send_message(customer_phone, message, device_id=device_id)
        return GuardResult(sent=True, reason="guard_disabled")

    customer = None
    if business_id:
        try:
            customer = db.get_customer_by_phone(business_id, customer_phone)
        except Exception as exc:
            logger.warning("[OUTBOUND-GUARD] customer lookup failed (%s) — continuing", exc)

    # Ordered checks: permanent suppressions first, then transient limits.
    for check, permanent in (
        (lambda: _check_opt_out(customer), True),
        (lambda: _check_cooldown(device_id), False),
        (lambda: _check_warmup(business), False),
        (lambda: _check_business_hours(business), False),
        (lambda: _check_touch_budget(customer), False),
        (lambda: _check_daily_cap(device_id), False),
    ):
        reason = check()
        if reason:
            logger.info(
                "[OUTBOUND-GUARD] BLOCKED mechanic=%s biz=%s phone=%s device=%s reason=%s",
                mechanic, business_id, customer_phone, device_id, reason,
            )
            _track(business_id, customer_phone, "proactive_send_blocked",
                   {"mechanic": mechanic, "reason": reason, "device": device_id})
            return GuardResult(sent=False, reason=reason, permanent=permanent)

    # Human-like randomized delay — never send in robotic fixed intervals.
    jitter = random.uniform(
        settings.WA_PROACTIVE_JITTER_MIN_S, settings.WA_PROACTIVE_JITTER_MAX_S
    )
    await asyncio.sleep(jitter)

    try:
        await _wa.send_message(customer_phone, message, device_id=device_id)
    except ReachoutTimelocked as exc:
        _open_circuit_breaker(device_id, exc.retry_after_seconds)
        _track(business_id, customer_phone, "proactive_send_463",
               {"mechanic": mechanic, "device": device_id,
                "retry_after_s": exc.retry_after_seconds})
        return GuardResult(sent=False, reason="whatsapp_463", permanent=False)

    # Success — stamp the shared touch budget + device daily counter.
    if business_id:
        try:
            _stamp_touch(business_id, customer_phone, customer, mechanic)
        except Exception as exc:
            logger.warning("[OUTBOUND-GUARD] touch stamp failed (non-fatal): %s", exc)
    try:
        db.increment_wa_daily_count(device_id, _day_key(_now()))
    except Exception as exc:
        logger.warning("[OUTBOUND-GUARD] daily counter failed (non-fatal): %s", exc)

    logger.info(
        "[OUTBOUND-GUARD] SENT mechanic=%s biz=%s phone=%s device=%s jitter=%.1fs",
        mechanic, business_id, customer_phone, device_id, jitter,
    )
    _track(business_id, customer_phone, "proactive_send_sent",
           {"mechanic": mechanic, "device": device_id})
    return GuardResult(sent=True, reason="sent")


# ── opt-out intent (inbound) ─────────────────────────────────────────────────

# Exact-match whole-message keywords only — a customer typing one of these as
# their ENTIRE message is expressing "stop messaging me". Substring matching
# would false-positive on normal conversation ("posso parar na loja?").
_OPT_OUT_PHRASES = {
    # Portuguese
    "parar", "pare", "sair", "chega", "cancelar",
    "não quero mais", "nao quero mais",
    "não quero receber", "nao quero receber",
    "parar mensagens", "não enviar mais", "nao enviar mais",
    # Spanish
    "basta", "salir", "no quiero más", "no quiero mas", "no más", "no mas",
    "detener", "cancelar mensajes",
    # English
    "stop", "unsubscribe", "opt out", "optout", "no more messages",
}

_OPT_OUT_CONFIRMATION = {
    "en": "✅ Done — you won't receive promotional messages from us anymore. "
          "You can still message us anytime you need something.",
    "pt": "✅ Pronto — você não vai mais receber mensagens promocionais nossas. "
          "Você ainda pode falar com a gente sempre que precisar.",
    "es": "✅ Listo — ya no recibirás mensajes promocionales nuestros. "
          "Puedes escribirnos cuando necesites algo.",
}


def detect_opt_out_intent(body: str) -> bool:
    """True when the ENTIRE message is an opt-out phrase (multilingual)."""
    normalized = (body or "").strip().lower().strip(".,!?🙏🙅✋🛑")
    return normalized in _OPT_OUT_PHRASES


async def maybe_handle_opt_out(business: dict, customer_phone: str, body: str) -> bool:
    """Inbound interception: suppress a contact who asked us to stop.

    Non-breaking contract:
      * Not an opt-out phrase            → False (normal routing continues).
      * Opt-out phrase                   → marketingOptOut is ALWAYS set
        (silent safety — future proactive sends are suppressed).
      * ...and the contact was touched proactively in the last 30 days
        (clearly replying to OUR outreach) → confirmation sent, True
        (caller stops routing; the AI never sees the message).
      * ...but no recent proactive touch → False: the flag is set silently
        and the AI continues the conversation as before, so organic chats
        ("stop" mid-booking-banter) keep today's behavior.
    """
    if not detect_opt_out_intent(body):
        return False

    business_id = business.get("id", "")
    if not business_id:
        return False

    customer = None
    try:
        customer = db.get_customer_by_phone(business_id, customer_phone)
    except Exception as exc:
        logger.warning("[OUTBOUND-GUARD] opt-out customer lookup failed: %s", exc)

    now_iso = _now().isoformat()
    try:
        db.upsert_customer(business_id, customer_phone, {
            "marketingOptOut": True,
            "marketingOptOutAt": now_iso,
        })
    except Exception as exc:
        logger.warning("[OUTBOUND-GUARD] opt-out flag write failed: %s", exc)
        return False

    logger.info(
        "[OUTBOUND-GUARD] OPT-OUT recorded biz=%s phone=%s (body=%r)",
        business_id, customer_phone, body[:40],
    )
    _track(business_id, customer_phone, "proactive_opt_out", {"body": body[:80]})

    last_touch = _parse_iso((customer or {}).get("lastProactiveAt"))
    if not (last_touch and _now() - last_touch <= timedelta(days=30)):
        return False  # organic conversation — let the AI keep handling it

    lang = (business.get("primaryLanguage") or "pt")[:2].lower()
    confirmation = _OPT_OUT_CONFIRMATION.get(lang) or _OPT_OUT_CONFIRMATION["en"]
    try:
        await _wa.send_message(customer_phone, confirmation, device_id=_device_for(business))
    except Exception as exc:
        logger.warning("[OUTBOUND-GUARD] opt-out confirmation send failed: %s", exc)
    return True
