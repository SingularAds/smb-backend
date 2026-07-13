"""CSAT (Customer Satisfaction) — WhatsApp 1-5 rating flow.

Flow:
  1. After every AI reply we mark ``csatPending=True`` and ``csatLastAiReplyAt``
     on the customer conversation doc.
  2. A background sweep (started by automation/scheduler.py) runs every
     CSAT_SWEEP_INTERVAL_MINUTES. It scans conversations where:
       - csatPending == True
       - csatRequestedAt is None
       - csatLastAiReplyAt is older than CSAT_IDLE_MINUTES
       - csatLastRatedAt is older than CSAT_COOLDOWN_DAYS (or absent)
       - aiPaused != True (don't pester during an owner takeover)
     For each match it sends the rating prompt via WhatsApp and sets
     ``csatRequestedAt``.
  3. When the customer replies with a digit 1-5 (possibly with leading
     emoji / "thanks for asking"), ``handle_csat_reply`` consumes it,
     persists the rating, scores the Langfuse trace, and fires a PostHog
     event. ``handle_csat_reply`` returns True so the webhook router can
     short-circuit before reaching the intent classifier.

Storage:
  customer_conversations/{phone}.{
    csatPending: bool,
    csatLastAiReplyAt: iso,
    csatRequestedAt: iso | None,
    csatRating: 1..5,
    csatRatedAt: iso,
    csatLastRatedAt: iso (cooldown anchor),
    lastTraceId: str (Langfuse trace ID of the last AI turn)
  }
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app import firestore as db
from app.config import settings
from app.integrations import langfuse_client, posthog_client
from app.services import outbound_guard
from app.services.whatsmeow_client import WhatsmeowClient

logger = logging.getLogger(__name__)

_wa = WhatsmeowClient()


# ── Reply parsing ─────────────────────────────────────────────────────────────
# Accepts: "5", "5/5", "rating 5", "⭐⭐⭐⭐⭐", "5 stars", "thanks, 5",
# "I'd say 4", " 3 ", etc. Rejects anything where the only digit is part of
# a longer number (e.g. "call me at 5pm" → ignored).
_DIGIT_ONLY_RE = re.compile(r"^\s*([1-5])(?:\s*/\s*5)?(?:\s*stars?)?\s*$", re.IGNORECASE)
_STAR_RE = re.compile(r"^[\s⭐★☆]+$")  # ⭐ ★ ☆
_LEADING_DIGIT_RE = re.compile(r"\b([1-5])\b")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_rating(text: str | None) -> int | None:
    """Return a 1-5 integer if the message is a rating, else None."""
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    # Pure star reply: 1-5 star characters → that many stars
    if _STAR_RE.match(s):
        n = sum(1 for ch in s if ch in "⭐★")
        return n if 1 <= n <= 5 else None
    m = _DIGIT_ONLY_RE.match(s)
    if m:
        return int(m.group(1))
    # Short message with a clear rating digit (≤ 30 chars so we don't grab
    # random numbers out of long sentences).
    if len(s) <= 30:
        m2 = _LEADING_DIGIT_RE.search(s)
        if m2:
            return int(m2.group(1))
    return None


# ── Public API ────────────────────────────────────────────────────────────────
def mark_ai_reply(
    business_id: str,
    customer_phone: str,
    trace_id: str | None = None,
) -> None:
    """Record that an AI reply just went out so the sweep can find this convo.

    Idempotent. Called from customer_ai_service after _deliver_reply.
    """
    patch = {
        "csatPending": True,
        "csatLastAiReplyAt": _now().isoformat(),
        # Clear any prior request so a new conversation cycle can prompt again
        "csatRequestedAt": None,
    }
    if trace_id:
        patch["lastTraceId"] = trace_id
    try:
        db.upsert_customer_conversation(business_id, customer_phone, patch)
    except Exception as exc:
        logger.warning("[CSAT] mark_ai_reply failed business=%s phone=%s: %s",
                       business_id, customer_phone, exc)


async def handle_csat_reply(
    business: dict,
    customer_phone: str,
    body: str,
) -> bool:
    """If *body* is a rating reply for a pending CSAT, consume it.

    Returns True when the message was a rating (so the caller must stop further
    processing). Returns False for any other message — including the case where
    no CSAT was requested.
    """
    if not settings.CSAT_ENABLED:
        return False
    business_id = business["id"]
    phone_clean = db._clean_phone(customer_phone)
    convo = db.get_customer_conversation(business_id, phone_clean)
    if not convo or not convo.get("csatRequestedAt"):
        return False
    rating = parse_rating(body)
    if rating is None:
        # If the customer ignored the CSAT prompt and sent a real follow-up,
        # treat that as opting out of *this* prompt and fall through to AI.
        # Clear the request marker so we don't re-consume their next reply.
        db.upsert_customer_conversation(business_id, phone_clean, {
            "csatRequestedAt": None,
            "csatPending": False,
        })
        return False

    now = _now()
    db.upsert_customer_conversation(business_id, phone_clean, {
        "csatRating":      rating,
        "csatRatedAt":     now.isoformat(),
        "csatLastRatedAt": now.isoformat(),
        "csatPending":     False,
        "csatRequestedAt": None,
    })

    # Roll up onto customer record for owner dashboards
    try:
        cust = db.get_customer_by_phone(business_id, phone_clean) or {}
        ratings = list(cust.get("csatHistory") or [])
        ratings.append({"rating": rating, "at": now.isoformat()})
        ratings = ratings[-20:]
        avg = round(sum(r["rating"] for r in ratings) / len(ratings), 2)
        db.upsert_customer(business_id, phone_clean, {
            "lastCsat":    rating,
            "avgCsat":     avg,
            "csatHistory": ratings,
        })
    except Exception as exc:
        logger.warning("[CSAT] could not roll up to customer record: %s", exc)

    # Link rating to the last AI trace in Langfuse
    trace_id = convo.get("lastTraceId") or ""
    if trace_id:
        try:
            langfuse_client.score_trace(
                trace_id=trace_id,
                name="csat",
                value=float(rating),
                comment=f"WhatsApp 1-5 rating; raw={body[:60]!r}",
            )
        except Exception as exc:
            logger.warning("[CSAT] could not score trace=%s: %s", trace_id, exc)

    # PostHog funnel
    try:
        posthog_client.capture(
            business_id=business_id,
            customer_phone=phone_clean,
            event="csat_received",
            properties={
                "rating": rating,
                "is_promoter": rating >= 4,
                "is_detractor": rating <= 2,
            },
        )
    except Exception:
        pass

    # Thank the customer (and, on low scores, escalate to owner)
    try:
        if rating <= 2:
            ack = (
                "Thanks for the feedback — we're sorry we missed the mark. "
                "We've notified the owner to follow up personally."
            )
            try:
                from app.services.automation import whatsapp_notifier
                label = f"+{phone_clean.lstrip('+')}"
                await whatsapp_notifier.send_to_owner(
                    business,
                    (
                        f"⚠️ *Low CSAT* from {label} — rated {rating}/5.\n"
                        f"Please follow up personally."
                    ),
                )
            except Exception as exc:
                logger.warning("[CSAT] could not notify owner of low rating: %s", exc)
        else:
            ack = "Thanks for the feedback — we appreciate it! 🙏"
        await _wa.send_message(
            phone_clean,
            ack,
            device_id=business.get("waSessionId"),
        )
    except Exception as exc:
        logger.warning("[CSAT] could not send ack to %s: %s", phone_clean, exc)

    logger.info(
        "[CSAT] business=%s phone=%s rating=%d trace=%s",
        business_id, phone_clean, rating, trace_id or "-",
    )
    return True


# ── Sweep ─────────────────────────────────────────────────────────────────────
def _list_pending(business_id: str) -> list[dict]:
    """Return conversations that need a CSAT prompt right now."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    docs = (
        db._db().collection("businesses")
        .document(business_id)
        .collection("customer_conversations")
        .where(filter=FieldFilter("csatPending", "==", True))
        .stream()
    )
    out: list[dict] = []
    idle_cutoff = _now() - timedelta(minutes=settings.CSAT_IDLE_MINUTES)
    cooldown_cutoff = _now() - timedelta(days=settings.CSAT_COOLDOWN_DAYS)
    for doc in docs:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        if data.get("csatRequestedAt"):
            continue
        if data.get("aiPaused"):
            continue
        last_ai = data.get("csatLastAiReplyAt")
        if not last_ai:
            continue
        try:
            last_ai_dt = datetime.fromisoformat(last_ai)
            if last_ai_dt.tzinfo is None:
                last_ai_dt = last_ai_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if last_ai_dt > idle_cutoff:
            continue  # still actively chatting — don't interrupt
        last_rated = data.get("csatLastRatedAt")
        if last_rated:
            try:
                last_rated_dt = datetime.fromisoformat(last_rated)
                if last_rated_dt.tzinfo is None:
                    last_rated_dt = last_rated_dt.replace(tzinfo=timezone.utc)
                if last_rated_dt > cooldown_cutoff:
                    continue
            except Exception:
                pass
        out.append(data)
    return out


def _prompt_text(business: dict) -> str:
    name = business.get("name") or "us"
    return (
        f"Thanks for chatting with {name}! 🙏\n\n"
        f"How was your experience today?\n"
        f"Reply with a number:\n"
        f"*1* — Poor\n"
        f"*2* — Fair\n"
        f"*3* — Okay\n"
        f"*4* — Good\n"
        f"*5* — Excellent"
    )


async def sweep_business(business: dict) -> int:
    """Send CSAT prompts for one business. Returns number sent."""
    if not settings.CSAT_ENABLED:
        return 0
    business_id = business["id"]
    device_id = business.get("waSessionId")
    if not device_id:
        return 0
    pending = _list_pending(business_id)
    sent = 0
    text = _prompt_text(business)
    for convo in pending:
        phone = convo.get("customerPhone") or convo.get("id")
        if not phone:
            continue
        try:
            # CSAT prompts are business-initiated (marketing-class) — they go
            # through the outbound guard: opt-out, touch budget, daily cap,
            # business hours, warm-up, 463 circuit breaker, jitter.
            result = await outbound_guard.send_proactive(
                business, phone, text, mechanic="csat_prompt",
            )
            if result.sent:
                db.upsert_customer_conversation(business_id, phone, {
                    "csatRequestedAt": _now().isoformat(),
                })
                try:
                    posthog_client.capture(
                        business_id=business_id,
                        customer_phone=phone,
                        event="csat_requested",
                    )
                except Exception:
                    pass
                sent += 1
            elif result.permanent:
                # Opted-out contact: mark as requested so the sweep never
                # retries this conversation, and record why.
                db.upsert_customer_conversation(business_id, phone, {
                    "csatRequestedAt": _now().isoformat(),
                    "csatSkippedReason": result.reason,
                })
                logger.info("[CSAT] skipped permanently biz=%s phone=%s reason=%s",
                            business_id, phone, result.reason)
            else:
                # Transient block (cap / hours / cooldown) — leave unmarked so
                # the next sweep retries naturally.
                logger.info("[CSAT] deferred biz=%s phone=%s reason=%s",
                            business_id, phone, result.reason)
        except Exception as exc:
            logger.warning("[CSAT] could not send prompt biz=%s phone=%s: %s",
                           business_id, phone, exc)
    if sent:
        logger.info("[CSAT] sweep biz=%s sent=%d", business_id, sent)
    return sent


async def sweep_all() -> None:
    """Sweep CSAT prompts across all businesses with an active WhatsApp session."""
    if not settings.CSAT_ENABLED:
        return
    try:
        businesses = db.list_businesses_with_wa_session()
    except Exception as exc:
        logger.warning("[CSAT] could not list businesses: %s", exc)
        return
    total = 0
    for biz in businesses:
        try:
            total += await sweep_business(biz)
        except Exception as exc:
            logger.warning("[CSAT] sweep error biz=%s: %s", biz.get("id"), exc)
    if total:
        logger.info("[CSAT] full sweep complete sent=%d", total)
