"""Onboarding Analyzer — AI analysis of a single onboarding journey.

Backs the internal dashboard's "Analyze" button on the funnel drill-down:
given an owner phone (the onboarding_sessions doc id), it reconstructs the
conversation, asks an LLM why the prospect completed / stalled / dropped,
and returns a structured, evidence-backed analysis.

Data sources (in order):
  1. onboarding_transcripts/{phone}/messages — the append-only archive
     written by the hooks in onboarding_service (has timestamps + step
     snapshots; survives session wipes and post-onboarding trimming).
  2. Fallback: onboarding_sessions/{phone}.conversationHistory — lets the
     analyzer work on historical sessions that predate the archive.

Provider is switchable via config (ONBOARDING_ANALYZER_PROVIDER):
  * "openai"    — Langfuse-traced AsyncOpenAI (same observability stack as
                  the live Sofia chat), JSON mode, ONBOARDING_ANALYZER_OPENAI_MODEL.
  * "anthropic" — official Anthropic SDK, ONBOARDING_ANALYZER_ANTHROPIC_MODEL.

Results are persisted in onboarding_analyses/{phone} together with a
fingerprint (message count + last message ts + prompt version + model).
An unchanged fingerprint is served from Firestore with no LLM call, so
repeat clicks are free; ``force=True`` re-runs. The stored docs double as
the learning corpus (team feedback is appended to the same doc).

This module is read-only over sessions/transcripts and is never imported
by the live conversation flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app import firestore as db
from app.config import settings

logger = logging.getLogger(__name__)

# Bump whenever the system prompt or payload format changes materially —
# stored analyses carry it, so quality is comparable across versions and
# stale-version cache entries are naturally re-run.
PROMPT_VERSION = 1

_MAX_TRANSCRIPT_MESSAGES = 100   # head+tail sampling beyond this
_TRANSCRIPT_HEAD = 30            # opening exchanges carry acquisition context
_MAX_MESSAGE_CHARS = 600         # per-message cap (menus/links can be huge)
_MIN_OWNER_MESSAGES = 2          # below this there is nothing to analyze
_STILL_ACTIVE_WINDOW_HOURS = 48  # last activity within this → not "dropped" yet
_ANALYSIS_MAX_TOKENS = 1600

# Concurrency guards: this is an on-demand internal tool, not a throughput
# path — bound parallel LLM spend and collapse double-clicks per phone.
_llm_semaphore = asyncio.Semaphore(2)
_inflight_locks: dict[str, asyncio.Lock] = {}


class SessionNotFound(Exception):
    """No onboarding session or transcript exists for this phone."""


class InsufficientConversation(Exception):
    """Too few owner messages to produce a meaningful analysis."""


class AnalyzerError(Exception):
    """LLM call failed or returned an unusable response."""


DROP_OFF_REASONS = (
    "PRICING_CONCERN",
    "FEATURE_MISMATCH",
    "CONFUSION_FRICTION",
    "TRUST_HESITATION",
    "LOST_INTEREST_INACTIVE",
    "LANGUAGE_BARRIER",
    "TECHNICAL_ISSUE",
    "PAIRING_ABANDONED",
    "PAYMENT_ABANDONED",
    "INSUFFICIENT_EVIDENCE",
    "OTHER",
)


class OnboardingAnalysis(BaseModel):
    """Output contract enforced on every LLM response before storing."""

    customerIntent: str
    outcome: Literal["completed", "dropped", "still_active"]
    dropOffStage: str | None = None
    dropOffReason: Literal[
        "PRICING_CONCERN", "FEATURE_MISMATCH", "CONFUSION_FRICTION",
        "TRUST_HESITATION", "LOST_INTEREST_INACTIVE", "LANGUAGE_BARRIER",
        "TECHNICAL_ISSUE", "PAIRING_ABANDONED", "PAYMENT_ABANDONED",
        "INSUFFICIENT_EVIDENCE", "OTHER",
    ] | None = None
    confidence: Literal["high", "medium", "low"]
    evidence: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    frictionPoints: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    summary: str


ANALYST_SYSTEM_PROMPT = """\
You are an expert conversion analyst for Recepte, a WhatsApp AI receptionist
for small businesses (salons, clinics, restaurants, studios). Business owners
sign up by chatting with "Sofia", Recepte's AI sales/onboarding assistant, on
a WhatsApp number. You review ONE owner's full conversation and explain what
happened — especially WHY they dropped off, if they did.

Funnel stages, in order: started → details_collected → whatsapp_paired → completed.

IMPORTANT CONTEXT ABOUT THE TRANSCRIPT:
- OWNER lines are the prospective business owner; SOFIA lines are the AI.
- Owners write in many languages (mostly Portuguese/Spanish). Quote evidence
  verbatim in the original language, adding a short English gloss in brackets.
- Some sessions contain a DEMO segment where Sofia roleplays a *customer
  booking conversation* so the owner can preview the product. Demo lines are
  marked when known. Do not mistake demo roleplay for the owner's own answers.
- The SESSION FACTS block is computed by the system from the database.
  Trust it over your own inference (e.g. for the outcome and timings).

YOUR TASK — respond with ONLY a JSON object, no markdown fences, using
exactly these keys:
{
  "customerIntent": "1-2 sentences: what this owner wanted to achieve",
  "outcome": "completed" | "dropped" | "still_active"  (copy from SESSION FACTS),
  "dropOffStage": "the funnel stage where momentum died, or null if completed",
  "dropOffReason": one of PRICING_CONCERN | FEATURE_MISMATCH | CONFUSION_FRICTION |
      TRUST_HESITATION | LOST_INTEREST_INACTIVE | LANGUAGE_BARRIER |
      TECHNICAL_ISSUE | PAIRING_ABANDONED | PAYMENT_ABANDONED |
      INSUFFICIENT_EVIDENCE | OTHER — or null if outcome is "completed",
  "confidence": "high" | "medium" | "low",
  "evidence": ["verbatim quotes from the transcript that support your reason"],
  "objections": ["explicit objections or worries the owner voiced"],
  "frictionPoints": ["specific moments where the flow lost momentum — unanswered
      questions, confusing Sofia replies, too many steps, long silences"],
  "recommendations": ["max 3 concrete, flow-level changes (not per-user actions)
      that would reduce this kind of drop-off"],
  "summary": "2-3 plain-English sentences a marketing person can read as-is"
}

RULES:
- Ground every claim in the transcript. If the evidence is thin (e.g. the owner
  simply stopped replying with no stated objection), use dropOffReason
  INSUFFICIENT_EVIDENCE or LOST_INTEREST_INACTIVE, set confidence to "low" or
  "medium", and state the most likely explanation in the summary — never invent
  objections that were not voiced.
- If outcome is "completed", dropOffReason and dropOffStage must be null;
  still report objections/friction seen along the way (they matter for
  improving the flow) and set recommendations accordingly.
- If outcome is "still_active", frame the analysis as risk assessment: what
  might stall this session, based on what has happened so far.
- Recommendations must be about improving the onboarding FLOW or Sofia's
  script, not about chasing this individual owner.
"""


# ── Small utilities ───────────────────────────────────────────────────────────

def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Data assembly ─────────────────────────────────────────────────────────────

def _load_messages(phone: str) -> tuple[list[dict], str]:
    """Return (normalized messages oldest-first, source).

    Normalized shape: {role: "user"|"assistant", content, ts?, step?, mode?}.
    Prefers the transcript archive; falls back to the session's
    conversationHistory for pre-archive sessions.
    """
    archived = db.get_onboarding_transcript(phone)
    if archived:
        msgs = [
            {
                "role": m.get("role") or "user",
                "content": m.get("content") or "",
                "ts": m.get("ts"),
                "step": m.get("step"),
                "mode": m.get("mode"),
            }
            for m in archived
            if m.get("content")
        ]
        if msgs:
            return msgs, "archive"

    session = db.get_onboarding_session(phone)
    history = (session or {}).get("conversationHistory") or []
    msgs = [
        {"role": m.get("role") or "user", "content": m.get("content") or ""}
        for m in history
        if isinstance(m, dict) and isinstance(m.get("content"), str) and m.get("content")
    ]
    return msgs, "session_history"


def _derive_outcome(session: dict | None, business: dict | None) -> str:
    """completed / still_active / dropped — same completion rule as the
    analytics funnel (a business doc for this owner is authoritative)."""
    if business:
        return "completed"
    ts = (session or {}).get("timestamps") or {}
    last = _parse_dt(ts.get("lastActivityAt")) or _parse_dt(ts.get("startedAt"))
    if last and (_now() - last).total_seconds() < _STILL_ACTIVE_WINDOW_HOURS * 3600:
        return "still_active"
    return "dropped"


def _funnel_stage(session: dict | None) -> str:
    if not session:
        return "unknown"
    try:
        from app.services.analytics_service import funnel_stage
        return funnel_stage(session)
    except Exception:  # analytics import must never break the analyzer
        return (session.get("currentStep") or "unknown")


def _build_session_facts(
    phone: str,
    session: dict | None,
    business: dict | None,
    messages: list[dict],
    source: str,
    outcome: str,
) -> str:
    ts = (session or {}).get("timestamps") or {}
    started = _parse_dt(ts.get("startedAt"))
    last = _parse_dt(ts.get("lastActivityAt")) or started
    attribution = (session or {}).get("attribution") or {}
    biz_data = (session or {}).get("businessData") or {}

    owner_count = sum(1 for m in messages if m["role"] == "user")
    sofia_count = len(messages) - owner_count
    demo_count = sum(1 for m in messages if m.get("mode") == "demo")

    # Deterministic timing facts — LLMs are unreliable at timestamp math,
    # so compute them here and hand them over as ground truth.
    duration_line = "unknown"
    if started and last and last >= started:
        hours = (last - started).total_seconds() / 3600
        duration_line = f"{hours:.1f}h between first and last activity"
    days_silent = ""
    if last:
        days = (_now() - last).total_seconds() / 86400
        days_silent = f" — last activity {days:.1f} days ago"

    longest_gap = None
    prev_dt: datetime | None = None
    for m in messages:
        m_dt = _parse_dt(m.get("ts"))
        if m_dt and prev_dt:
            gap = (m_dt - prev_dt).total_seconds()
            if longest_gap is None or gap > longest_gap:
                longest_gap = gap
        if m_dt:
            prev_dt = m_dt
    gap_line = (
        f"{longest_gap / 3600:.1f}h" if longest_gap is not None
        else "unknown (no per-message timestamps in legacy history)"
    )

    lines = [
        "SESSION FACTS (computed by the system — trust these):",
        f"- Owner phone: {phone}",
        f"- Outcome: {outcome}",
        f"- Deepest funnel stage reached: {_funnel_stage(session)}"
        + (f" (currentStep={session.get('currentStep')})" if session else ""),
        f"- Business created: {'yes — ' + str(business.get('name')) if business else 'no'}",
        f"- Started: {_iso(started) or 'unknown'}; duration: {duration_line}{days_silent}",
        f"- Longest gap between consecutive messages: {gap_line}",
        f"- Acquisition: channel={attribution.get('channel') or 'unknown'}"
        + (f", campaign={attribution.get('campaign')}" if attribution.get("campaign") else ""),
        f"- Session language: {(session or {}).get('language') or 'unknown'}",
        f"- Messages analyzed: {len(messages)} ({owner_count} owner / {sofia_count} Sofia), "
        f"source={source}",
    ]
    if demo_count:
        lines.append(
            f"- Demo-mode messages: {demo_count} (Sofia roleplaying a CUSTOMER "
            "booking conversation as a product preview — not owner answers)"
        )
    if source == "session_history":
        lines.append(
            "- NOTE: legacy history (no timestamps/step markers); it may be "
            "partially trimmed and may interleave unmarked demo roleplay."
        )
    if biz_data:
        collected = ", ".join(f"{k}={_clip(str(v), 60)}" for k, v in list(biz_data.items())[:8])
        lines.append(f"- Business details collected so far: {collected}")
    return "\n".join(lines)


def _format_transcript(messages: list[dict]) -> str:
    msgs = messages
    omitted_note = ""
    if len(msgs) > _MAX_TRANSCRIPT_MESSAGES:
        tail = _MAX_TRANSCRIPT_MESSAGES - _TRANSCRIPT_HEAD
        omitted = len(msgs) - _MAX_TRANSCRIPT_MESSAGES
        omitted_note = f"\n[... {omitted} messages omitted ...]\n"
        msgs = msgs[:_TRANSCRIPT_HEAD] + msgs[-tail:]
        head_part, tail_part = msgs[:_TRANSCRIPT_HEAD], msgs[_TRANSCRIPT_HEAD:]
    else:
        head_part, tail_part = msgs, []

    def _line(m: dict) -> str:
        who = "OWNER" if m["role"] == "user" else "SOFIA"
        ts = ""
        m_dt = _parse_dt(m.get("ts"))
        if m_dt:
            ts = f"[{m_dt.strftime('%Y-%m-%d %H:%M')}] "
        tags = []
        if m.get("step"):
            tags.append(str(m["step"]))
        if m.get("mode") == "demo":
            tags.append("DEMO")
        tag = f" ({'/'.join(tags)})" if tags else ""
        return f"{ts}{who}{tag}: {_clip(m['content'])}"

    parts = [_line(m) for m in head_part]
    if omitted_note:
        parts.append(omitted_note)
        parts.extend(_line(m) for m in tail_part)
    return "\n".join(parts)


def _build_context_sections() -> str:
    """Product truth (Global KB — same source Sofia's prompt uses) plus the
    dashboard-editable marketing context for the analyzer."""
    sections: list[str] = []
    try:
        from app.services.global_kb import build_kb_prompt_section
        kb = build_kb_prompt_section()
        if kb:
            sections.append(
                "PRODUCT KNOWLEDGE BASE (what is actually true about Recepte — "
                "use it to judge whether Sofia answered correctly):\n" + kb
            )
    except Exception as exc:
        logger.warning("[ANALYZER] Global KB unavailable: %s", exc)

    try:
        ctx = db.get_analyzer_context()
        content = (ctx or {}).get("content") or ""
        if content.strip():
            sections.append(
                "MARKETING CONTEXT (provided by the Recepte team — objectives, "
                "expected journey, objection playbook):\n" + content.strip()
            )
    except Exception as exc:
        logger.warning("[ANALYZER] Analyzer context unavailable: %s", exc)
    return "\n\n".join(sections)


# ── LLM call (provider switch) ────────────────────────────────────────────────

def _provider_and_model() -> tuple[str, str]:
    provider = (settings.ONBOARDING_ANALYZER_PROVIDER or "openai").strip().lower()
    if provider == "anthropic":
        return "anthropic", settings.ONBOARDING_ANALYZER_ANTHROPIC_MODEL
    return "openai", settings.ONBOARDING_ANALYZER_OPENAI_MODEL


async def _call_llm(system: str, payload: str) -> str:
    provider, model = _provider_and_model()

    if provider == "anthropic":
        if not settings.ANTHROPIC_API_KEY:
            raise AnalyzerError("ANTHROPIC_API_KEY is not configured")
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=model,
            max_tokens=_ANALYSIS_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": payload}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise AnalyzerError("model declined the analysis request")
        text = next(
            (b.text for b in response.content if getattr(b, "type", "") == "text"),
            "",
        )
        if not text:
            raise AnalyzerError("empty response from Anthropic")
        return text

    if not settings.OPENAI_API_KEY:
        raise AnalyzerError("OPENAI_API_KEY is not configured")
    from app.integrations.langfuse_client import get_async_openai
    client = get_async_openai(settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=model,
        max_tokens=_ANALYSIS_MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ],
    )
    text = response.choices[0].message.content or ""
    if not text:
        raise AnalyzerError("empty response from OpenAI")
    return text


def _parse_analysis(raw: str) -> OnboardingAnalysis:
    """Parse + schema-validate the LLM output; tolerant of code fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    # Last resort: slice from first '{' to last '}'.
    if not text.lstrip().startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalyzerError(f"model returned invalid JSON: {exc}") from exc
    try:
        return OnboardingAnalysis.model_validate(data)
    except ValidationError as exc:
        raise AnalyzerError(f"model output failed schema validation: {exc}") from exc


# ── Public entry point ────────────────────────────────────────────────────────

async def analyze_onboarding_session(phone: str, force: bool = False) -> dict:
    """Analyze one onboarding journey; serve the stored result when fresh.

    Raises SessionNotFound / InsufficientConversation / AnalyzerError.
    """
    phone = db._clean_phone(phone)
    lock = _inflight_locks.setdefault(phone, asyncio.Lock())
    async with lock:
        session = await asyncio.to_thread(db.get_onboarding_session, phone)
        messages, source = await asyncio.to_thread(_load_messages, phone)
        if session is None and not messages:
            raise SessionNotFound(phone)

        owner_msgs = sum(1 for m in messages if m["role"] == "user")
        if owner_msgs < _MIN_OWNER_MESSAGES:
            raise InsufficientConversation(
                f"only {owner_msgs} owner message(s) — not enough to analyze"
            )

        provider, model = _provider_and_model()
        last_ts = next(
            (m.get("ts") for m in reversed(messages) if m.get("ts")), None
        )
        fingerprint = {
            "messageCount": len(messages),
            "lastMessageTs": last_ts,
            "promptVersion": PROMPT_VERSION,
            "provider": provider,
            "model": model,
        }

        stored = await asyncio.to_thread(db.get_onboarding_analysis, phone)
        if stored and not force and stored.get("fingerprint") == fingerprint:
            return {**stored, "cached": True}

        business = await asyncio.to_thread(db.get_business_by_owner_phone, phone)
        outcome = _derive_outcome(session, business)

        context = await asyncio.to_thread(_build_context_sections)
        facts = _build_session_facts(phone, session, business, messages, source, outcome)
        transcript = _format_transcript(messages)
        payload = "\n\n".join(
            p for p in (context, facts, "TRANSCRIPT (chronological):\n" + transcript) if p
        )

        async with _llm_semaphore:
            raw = await _call_llm(ANALYST_SYSTEM_PROMPT, payload)
        analysis = _parse_analysis(raw)

        doc = {
            "phone": phone,
            "analysis": analysis.model_dump(),
            "fingerprint": fingerprint,
            "provider": provider,
            "model": model,
            "promptVersion": PROMPT_VERSION,
            "transcriptSource": source,
            "messageCount": len(messages),
            "analyzedAt": _now().isoformat(),
            # Preserve accumulated team feedback across re-analyses.
            "feedback": (stored or {}).get("feedback", []),
        }
        await asyncio.to_thread(db.set_onboarding_analysis, phone, doc)
        logger.info(
            "[ANALYZER] analyzed phone=%s outcome=%s reason=%s provider=%s model=%s msgs=%d src=%s",
            phone, outcome, analysis.dropOffReason, provider, model, len(messages), source,
        )
        return {**doc, "cached": False}


async def record_analysis_feedback(phone: str, helpful: bool, note: str | None) -> None:
    """Attach 👍/👎 (+optional note) to the stored analysis — the signal used
    to measure prompt versions against each other."""
    phone = db._clean_phone(phone)
    feedback = {
        "helpful": helpful,
        "note": (note or "").strip()[:2000] or None,
        "at": _now().isoformat(),
        "promptVersion": PROMPT_VERSION,
    }
    ok = await asyncio.to_thread(db.append_onboarding_analysis_feedback, phone, feedback)
    if not ok:
        raise SessionNotFound(phone)
