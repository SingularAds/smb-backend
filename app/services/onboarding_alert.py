"""Human-support alert for stuck / confused / off-context conversations.

When a conversation genuinely needs a human — the owner cannot share their
location after repeated tries, a lookup keeps failing, the owner explicitly asks
for a person, or they go badly off-context — Sofia pings a configured
human-support WhatsApp number (``settings.ALERT_NUMBER``) with the client's phone
number, a one-line reason, and a short slice of recent context, so the agent can
open that chat and step in.

Works for BOTH conversation kinds:
  • ``session_kind="onboarding"`` — reads ``conversationHistory``, persists dedup
    state to ``onboarding_sessions``, sends from the owner's onboarding number.
  • ``session_kind="demo"``       — reads ``demoHistory``, persists dedup state to
    ``demo_sessions``, sends from the default global number.

Everything here is fully OPTIONAL, self-contained, and NON-BLOCKING:
  • If ``ALERT_NUMBER`` is not set, every function is a cheap no-op.
  • All sends are best-effort; a failure is logged and swallowed so an alerting
    hiccup can never break the conversation.
  • The assistant NEVER stops, pauses, or hands off because of an alert — the
    alert is delivered in parallel while Sofia keeps replying normally.
  • De-duplicated per session: at most one alert per ``_COOLDOWN`` window and at
    most ``_MAX_ALERTS_PER_SESSION`` alerts for a given contact, so a stuck or
    repetitive user never spams the support agent.
"""
from __future__ import annotations

import logging
import re
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
# Absolute ceiling per session, so a permanently-stuck contact can't page forever.
_MAX_ALERTS_PER_SESSION = 3
# How many recent turns to include as context in the alert.
_CONTEXT_TURNS = 4


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


def _history_field(session_kind: str) -> str:
    return "demoHistory" if session_kind == "demo" else "conversationHistory"


def _history(session: dict, session_kind: str) -> list[dict]:
    return session.get(_history_field(session_kind)) or []


def _last_user_message(session: dict, session_kind: str) -> str:
    for turn in reversed(_history(session, session_kind)):
        if turn.get("role") == "user":
            text = (turn.get("content") or "").strip()
            if text:
                return text[:300]
    return ""


def _recent_context(session: dict, session_kind: str) -> str:
    """A compact transcript of the last few turns for the support agent."""
    turns = _history(session, session_kind)[-_CONTEXT_TURNS:]
    lines: list[str] = []
    for turn in turns:
        who = "Client" if turn.get("role") == "user" else "Sofia"
        text = (turn.get("content") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"  {who}: {text[:180]}")
    return "\n".join(lines)


def _persist(phone: str, session_kind: str, updates: dict) -> None:
    try:
        if session_kind == "demo":
            db.upsert_demo_session(phone, updates)
        else:
            db.upsert_onboarding_session(phone, updates)
    except Exception:
        logger.exception("[ALERT] Failed to persist alert state for %s (%s)", phone, session_kind)


def _build_alert_text(phone: str, session: dict, reason: str, session_kind: str) -> str:
    push = session.get("pushName") or "—"
    lang = session.get("language") or "—"
    last_msg = _last_user_message(session, session_kind)
    context = _recent_context(session, session_kind)

    if session_kind == "demo":
        header = "🆘 *Demo chat needs a human*"
        channel = "demo number"
    else:
        header = "🆘 *Onboarding needs a human*"
        step = session.get("currentStep") or "—"
        onboarding_number = session.get("onboardingNumber") or ""
        channel = f"onboarding{f' · +{onboarding_number}' if onboarding_number else ''} · step: {step}"

    lines = [
        header,
        "",
        f"Client: {push} (+{phone})",
        f"Channel: {channel}  |  Lang: {lang}",
        "",
        f"Issue: {reason}",
    ]
    if last_msg:
        lines.append("")
        lines.append(f'Last message: "{last_msg}"')
    if context:
        lines.append("")
        lines.append("Recent context:")
        lines.append(context)
    lines.append("")
    lines.append(f"➡️ Open a WhatsApp chat with +{phone} to step in and help.")
    return "\n".join(lines)


async def maybe_alert_human(
    phone: str,
    session: dict,
    reason: str,
    *,
    force: bool = False,
    session_kind: str = "onboarding",
    category: str = "",
) -> bool:
    """Alert the human-support number about a conversation that needs a human.

    Returns True if an alert was actually sent. No-ops (returns False) when
    ``ALERT_NUMBER`` is unset, or when the per-session cooldown / ceiling has
    been hit (unless ``force`` is set, which still respects the hard ceiling).
    ``session_kind`` selects onboarding vs demo persistence + context.
    ``category`` classifies the alert for the Firestore audit log.
    """
    alert_number = (settings.ALERT_NUMBER or "").strip()
    if not alert_number or not phone:
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

    text = _build_alert_text(phone, session, reason, session_kind)

    # Send FROM a global number: the owner's onboarding number when we know it,
    # else the default global device (also used for the demo alerts). The alert
    # goes TO settings.ALERT_NUMBER so the agent sees it in one inbox.
    device = session.get("onboardingDeviceId") or None
    try:
        await _wa.send_message(alert_number, text, device_id=device)
    except Exception as exc:  # never let an alerting failure break the chat
        logger.warning("[ALERT] Failed to send human alert for %s: %s", phone, exc)
        return False

    stamp = _now().isoformat()
    updates = {
        "humanAlertLastAt": stamp,
        "humanAlertCount": count + 1,
        "humanAlertLastReason": reason[:300],
        "stuckCount": 0,  # we've acted on it
    }
    _persist(phone, session_kind, updates)
    session.update(updates)

    # Audit log: store the EXACT message sent to ALERT_NUMBER in Firestore so the
    # team can review what was shared (independent of the WhatsApp inbox). The db
    # helper is best-effort and never raises.
    db.log_alert(
        phone,
        session_kind=session_kind,
        category=category,
        reason=reason,
        text=text,
        device=device,
        business_name=session.get("businessName") or "",
        step=(session.get("currentStep") or "") if session_kind != "demo" else "demo",
    )

    # Record it on the transcript so the dashboard shows an escalation happened
    # (onboarding only — the demo is isolated and has its own store).
    if session_kind != "demo":
        try:
            transcript.record_message(
                phone, "assistant", f"[human-support alerted] {reason}",
                step=session.get("currentStep"),
            )
        except Exception:
            pass

    logger.info(
        "[ALERT] Human support alerted for %s (kind=%s, reason=%s)",
        phone, session_kind, reason,
    )
    return True


async def note_stuck(
    phone: str, session: dict, reason: str, *, session_kind: str = "onboarding",
) -> bool:
    """Record a 'no forward progress' signal; escalate once it repeats.

    Increments a per-session ``stuckCount``. When it reaches ``_STUCK_THRESHOLD``
    an alert is raised (and the counter reset). Returns True if an alert fired.
    Safe no-op when ``ALERT_NUMBER`` is unset.
    """
    if not (settings.ALERT_NUMBER or "").strip():
        return False

    count = int(session.get("stuckCount") or 0) + 1
    session["stuckCount"] = count
    _persist(phone, session_kind, {"stuckCount": count})

    if count >= _STUCK_THRESHOLD:
        return await maybe_alert_human(
            phone, session, reason, session_kind=session_kind, category="stuck",
        )
    return False


# How many times an owner asks for a pairing code / QR (incl. QR↔code switches and
# refreshes) before we treat them as stuck on pairing and page a human. Their
# FIRST choice counts as 1, so 3 means "asked a third time and still not linked".
_PAIRING_ATTEMPT_ALERT_AT = 3


async def note_pairing_attempt(
    phone: str, session: dict, *, session_kind: str = "onboarding",
) -> bool:
    """Count one WhatsApp pairing code/QR request. WhatsApp linking is the most
    important — and most drop-off-prone — onboarding step, so when the owner has
    asked for a code/QR ``_PAIRING_ATTEMPT_ALERT_AT`` times without linking they
    are clearly stuck: page a human ONCE (a ``pairingStuckAlerted`` latch prevents
    re-paging on every further attempt). No-op unless ``ALERT_NUMBER`` is set.
    """
    if not (settings.ALERT_NUMBER or "").strip():
        return False

    count = int(session.get("pairingAttemptCount") or 0) + 1
    session["pairingAttemptCount"] = count
    _persist(phone, session_kind, {"pairingAttemptCount": count})

    if count < _PAIRING_ATTEMPT_ALERT_AT or session.get("pairingStuckAlerted"):
        return False

    session["pairingStuckAlerted"] = True
    _persist(phone, session_kind, {"pairingStuckAlerted": True})
    return await maybe_alert_human(
        phone, session,
        f"Owner is stuck on WhatsApp pairing — asked for a code/QR {count} times "
        f"without linking their WhatsApp.",
        force=True, session_kind=session_kind, category="pairing_stuck",
    )


async def note_progress(
    phone: str, session: dict, *, session_kind: str = "onboarding",
) -> None:
    """Clear the stuck counter after a clear sign of forward progress."""
    if not int(session.get("stuckCount") or 0):
        return
    session["stuckCount"] = 0
    _persist(phone, session_kind, {"stuckCount": 0})


# ── Support-need classifier ───────────────────────────────────────────────────
# Detects, from a single inbound message, whether a human should be pinged IN
# PARALLEL (the assistant still replies normally). Deterministic and cheap — no
# LLM call. Two categories:
#   • "human_request"  — the person explicitly wants a human / real agent.
#   • "inappropriate"  — clearly off-context / abusive / sexual content.
# Returns (category, reason) or (None, "").

# Specific phrases / strong words that mean "I want a human". Generic words like
# "help"/"ajuda" are intentionally EXCLUDED here (too broad for substring match —
# the exact-match _DANIEL_TRIGGER_WORDS path already covers a bare "help").
_HUMAN_REQUEST_PATTERNS: tuple[str, ...] = (
    # English
    "human support", "human agent", "real person", "real human", "a human",
    "talk to a human", "speak to a human", "talk to a person", "speak to a person",
    "talk to someone", "speak to someone", "talk to an agent", "contact support",
    "customer support", "live agent", "real agent", "actual person", "want a human",
    "need a human", "representative", "talk to support", "human being",
    # Portuguese
    "humano", "atendente", "pessoa real", "falar com alguém", "falar com alguem",
    "falar com uma pessoa", "falar com um humano", "falar com humano",
    "suporte humano", "atendimento humano", "com uma pessoa de verdade",
    "gente de verdade", "quero falar com alguém", "quero falar com alguem",
    "um atendente", "ser humano",
    # Spanish
    "persona real", "hablar con alguien", "hablar con una persona",
    "hablar con un humano", "atención humana", "atencion humana",
    "soporte humano", "agente humano", "un agente", "representante humano",
)

# Clearly inappropriate / off-context terms (word-boundary matched so "sexta"
# (Friday), "Essex", etc. never false-positive on "sex"). Kept conservative.
_INAPPROPRIATE_RE = re.compile(
    r"\b("
    r"sex|sexo|nude|nudes|naked|porn|porno|pornô|xvideos|xnxx|"
    r"fuck|fucking|dick|pussy|boobs|tits|horny|blowjob|cum|"
    r"pelada|pelado|transar|foder|buceta|caralho|piroca|xota|"
    r"puta que|vai se fuder|filho da puta"
    r")\b",
    re.IGNORECASE,
)


# Bare, unambiguous "I want a human" words (a whole-message match). Kept apart
# from the generic _DANIEL_TRIGGER_WORDS ("help"/"ajuda"/"daniel") which are too
# broad or name-like to page a human on their own.
_HUMAN_WORDS_EXACT: frozenset[str] = frozenset({
    "human", "humans", "humano", "humanos", "humain",
    "person", "pessoa", "persona",
    "atendente", "agente", "representante", "representative",
})


def classify_support_need(text: str) -> tuple[str | None, str]:
    """Return (category, reason) if this message warrants a parallel human alert."""
    if not text:
        return None, ""
    lowered = text.strip().lower()

    # Whole message is a bare human-word ("humano", "person", "atendente"…).
    if re.sub(r"[^\w]", "", lowered) in _HUMAN_WORDS_EXACT:
        return "human_request", f"User asked to talk to a human: {text.strip()[:160]!r}"

    for pat in _HUMAN_REQUEST_PATTERNS:
        if pat in lowered:
            return "human_request", f"User asked to talk to a human: {text.strip()[:160]!r}"

    if _INAPPROPRIATE_RE.search(lowered):
        return "inappropriate", f"User went off-context / inappropriate: {text.strip()[:160]!r}"

    return None, ""


# ── Stuck / confused / frustrated signal ──────────────────────────────────────
# Phrases where the owner is telling us, in their own words, that they can't get
# something to work / don't understand / don't know how. Unlike an explicit human
# request this does NOT page immediately — the assistant guides first, and only if
# the owner keeps signaling (note_stuck threshold) do we escalate (client
# 2026-07-29). EN / PT / ES. Word-ish boundaries keep false-positives low.
_STUCK_PHRASE_RE = re.compile(
    r"("
    # ── English ──
    r"not working|does\s?n'?t work|is\s?n'?t work|are\s?n'?t work|did\s?n'?t work"
    r"|do\s?n'?t work|wo\s?n'?t work|stopped working|not letting me"
    r"|ca\s?n'?t connect|can\s?not connect|wo\s?n'?t connect|not connecting"
    r"|ca\s?n'?t link|can\s?not link|not linking"
    r"|do\s?n'?t understand|do not understand|not understand|no\s?t clear to me"
    r"|do\s?n'?t know how|do not know how|not sure how|no idea how|how do i even"
    r"|i'?m stuck|i am stuck|i'?m lost|so confusing|too confusing|too complicated"
    r"|too hard|too difficult|ca\s?n'?t do (?:it|this)|can\s?not do (?:it|this)"
    r"|not able to|unable to|i give up|nothing happens|not happening"
    # ── Portuguese ──
    r"|não funciona|nao funciona|não está funcion|nao esta funcion|não deu certo|nao deu certo"
    r"|não consigo|nao consigo|não entendi|nao entendi|não entendo|nao entendo"
    r"|não sei como|nao sei como|não sei fazer|nao sei fazer|muito difícil|muito dificil"
    r"|complicado demais|travou|não abre|nao abre|não conecta|nao conecta|desisto"
    # ── Spanish ──
    r"|no funciona|no está funcion|no esta funcion|no sirve|no me deja"
    r"|no entiendo|no sé cómo|no se como|no logro|no puedo hacer|muy difícil|muy dificil"
    r"|no conecta|no se conecta|me rindo"
    r")",
    re.IGNORECASE,
)


def classify_stuck_signal(text: str) -> str:
    """Return a short reason if the message expresses being stuck / confused /
    unable to proceed, else "". Used for the guide-first-then-escalate path."""
    if not text:
        return ""
    if _STUCK_PHRASE_RE.search(text.lower()):
        return f"Owner signaled they're stuck / can't proceed: {text.strip()[:160]!r}"
    return ""


async def alert_if_support_needed(
    phone: str,
    session: dict,
    text: str,
    *,
    session_kind: str = "onboarding",
) -> str | None:
    """Run the classifier on an inbound message and, if it flags, alert a human
    IN PARALLEL. Never blocks or changes the conversation — the caller keeps
    replying exactly as before. Returns the detected category, or None.

    An explicit human request bypasses the cooldown (force=True) since the person
    asked directly; off-context/inappropriate uses the normal cooldown so a
    string of such messages doesn't spam the agent.

    A softer "stuck / confused / can't proceed" signal does NOT page on the first
    hit — the assistant guides first; only if the owner KEEPS signaling does
    note_stuck escalate (client 2026-07-29). Returns "stuck" in that case.
    """
    if not (settings.ALERT_NUMBER or "").strip():
        return None

    # 1) Explicit human request / inappropriate → alert in parallel right away.
    category, reason = classify_support_need(text)
    if category:
        try:
            await maybe_alert_human(
                phone, session, reason,
                force=(category == "human_request"),
                session_kind=session_kind,
                category=category,
            )
        except Exception:
            logger.exception("[ALERT] alert_if_support_needed failed for %s", phone)
        return category

    # 2) "It's not working" / "I don't understand" / "I don't know how" → guide
    #    first (assistant replies as usual), escalate only if they stay stuck.
    stuck_reason = classify_stuck_signal(text)
    if stuck_reason:
        try:
            await note_stuck(phone, session, stuck_reason, session_kind=session_kind)
        except Exception:
            logger.exception("[ALERT] stuck-signal note failed for %s", phone)
        return "stuck"

    return None
