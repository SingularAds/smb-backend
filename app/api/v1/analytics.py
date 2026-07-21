"""Internal analytics endpoints — team-only, gated by ANALYTICS_ADMIN_KEY.

Endpoints backing the internal dashboard (dashboard/ app):

  GET /api/v1/analytics/overview                     (read-only)
  GET /api/v1/analytics/businesses/{business_id}     (read-only)
  GET /api/v1/analytics/global-kb                    (read-only)
  PUT /api/v1/analytics/global-kb                    (the ONE write: edits the
      Global Recepte KB text injected into the sales/onboarding AI prompts)

Auth: require_admin_key (x-admin-key header / Bearer / ?key=) — deliberately
distinct from API_SECRET so dashboard access rotates independently. These
endpoints must never be exposed to SMB owners; a future owner-facing variant
should mount app.services.analytics_service.get_business_detail under its
own auth instead of reusing this router.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import firestore as fs
from app.api.deps import require_admin_key
from app.services import analytics_service

logger = logging.getLogger(__name__)

router = APIRouter()

# Guardrail for KB writes: the KB is injected into LLM prompts on every
# onboarding/sales turn, so a runaway document directly costs tokens.
_GLOBAL_KB_MAX_CHARS = 60_000


def _resolve_range(days: int | None, date_from: str | None, date_to: str | None):
    try:
        return analytics_service.resolve_range(days, date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/overview")
def analytics_overview(
    days: int | None = Query(30, ge=1, le=730, description="Lookback window in days (ignored when from/to given)"),
    date_from: str | None = Query(None, alias="from", description="Range start (ISO date/datetime, UTC)"),
    date_to: str | None = Query(None, alias="to", description="Range end (ISO date/datetime, UTC; defaults to now)"),
    include_test: bool = Query(False, description="Include QA/demo/test businesses"),
    _: None = Depends(require_admin_key),
) -> dict:
    """Platform overview: onboarding funnel, growth trend, aggregate KPIs,
    and the accounts table."""
    start, end = _resolve_range(days, date_from, date_to)
    return analytics_service.get_platform_overview(start, end, include_test=include_test)


@router.get("/businesses/{business_id}")
def analytics_business_detail(
    business_id: str,
    days: int | None = Query(30, ge=1, le=730),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    _: None = Depends(require_admin_key),
) -> dict:
    """Single-business drill-down: profile header, bookings, conversations
    (with transcripts), CSAT trend, complaints, knowledge gaps."""
    start, end = _resolve_range(days, date_from, date_to)
    detail = analytics_service.get_business_detail(business_id, start, end)
    if detail is None:
        raise HTTPException(status_code=404, detail="Business not found")
    return detail


# ── Global Recepte KB (view + edit) ──────────────────────────────────────────

class GlobalKbUpdate(BaseModel):
    content: str


def _global_kb_payload() -> dict:
    """Current KB with provenance — Firestore doc or the embedded default."""
    doc = fs.get_global_kb()
    if doc and doc.get("content"):
        content = str(doc["content"])
        source = "firestore"
        updated_at = doc.get("updatedAt")
    else:
        from app.services.global_kb import DEFAULT_KB_TEXT
        content = DEFAULT_KB_TEXT
        source = "default"
        updated_at = None
    return {
        "content": content,
        "source": source,
        "updatedAt": updated_at,
        "chars": len(content),
        "approxTokens": max(1, len(content) // 4),
        "maxChars": _GLOBAL_KB_MAX_CHARS,
    }


@router.get("/global-kb")
def get_global_kb(_: None = Depends(require_admin_key)) -> dict:
    """The Global Recepte KB (product/sales/pricing knowledge injected into
    the onboarding AI's prompts for every business)."""
    return _global_kb_payload()


@router.put("/global-kb")
def update_global_kb(
    payload: GlobalKbUpdate,
    _: None = Depends(require_admin_key),
) -> dict:
    """Replace the Global KB text. Takes effect immediately in this process;
    other workers pick it up within the 5-minute prompt cache TTL."""
    content = payload.content.replace("\r\n", "\n")
    if not content.strip():
        raise HTTPException(status_code=422, detail="KB content must not be empty")
    if len(content) > _GLOBAL_KB_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"KB content is {len(content):,} characters — the cap is "
                f"{_GLOBAL_KB_MAX_CHARS:,} (it is injected into every sales prompt)"
            ),
        )
    fs.set_global_kb(content)
    # Bust this process's prompt cache so the live AI uses the new text now.
    from app.services import global_kb as global_kb_module
    global_kb_module.get_global_kb(force_refresh=True)
    logger.info("[ANALYTICS] Global KB updated (%d chars)", len(content))
    return _global_kb_payload()


# ── Onboarding analyzer (AI analysis of onboarding journeys) ─────────────────
# Backs the "Analyze" button in the funnel drill-down. Results are cached in
# Firestore keyed by a transcript fingerprint, so repeat clicks cost nothing;
# ?force=true re-runs the LLM. See app/services/onboarding_analyzer_service.py.


class AnalysisFeedback(BaseModel):
    helpful: bool
    note: str | None = None


@router.post("/onboarding-sessions/{phone}/analyze")
async def analyze_onboarding_session(
    phone: str,
    force: bool = Query(False, description="Re-run the LLM even when a fresh cached analysis exists"),
    _: None = Depends(require_admin_key),
) -> dict:
    """AI analysis of one owner's onboarding journey: intent, drop-off reason
    (evidence-backed), objections, friction points, and flow recommendations."""
    from app.services import onboarding_analyzer_service as analyzer

    try:
        return await analyzer.analyze_onboarding_session(phone, force=force)
    except analyzer.SessionNotFound:
        raise HTTPException(status_code=404, detail="No onboarding session or transcript for this phone")
    except analyzer.InsufficientConversation as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except analyzer.AnalyzerError as exc:
        logger.error("[ANALYZER] analysis failed for %s: %s", phone, exc)
        raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}")


@router.post("/onboarding-sessions/{phone}/analysis-feedback")
async def onboarding_analysis_feedback(
    phone: str,
    payload: AnalysisFeedback,
    _: None = Depends(require_admin_key),
) -> dict:
    """Attach team feedback (helpful yes/no + note) to a stored analysis —
    the signal used to compare analyzer prompt versions over time."""
    from app.services import onboarding_analyzer_service as analyzer

    try:
        await analyzer.record_analysis_feedback(phone, payload.helpful, payload.note)
    except analyzer.SessionNotFound:
        raise HTTPException(status_code=404, detail="No stored analysis for this phone")
    return {"ok": True}


# ── Analyzer marketing context (view + edit — mirrors the Global KB) ─────────

_ANALYZER_CONTEXT_MAX_CHARS = 30_000


class AnalyzerContextUpdate(BaseModel):
    content: str


def _analyzer_context_payload() -> dict:
    doc = fs.get_analyzer_context()
    content = str((doc or {}).get("content") or "")
    return {
        "content": content,
        "updatedAt": (doc or {}).get("updatedAt"),
        "chars": len(content),
        "maxChars": _ANALYZER_CONTEXT_MAX_CHARS,
    }


@router.get("/analyzer-context")
def get_analyzer_context(_: None = Depends(require_admin_key)) -> dict:
    """Marketing context injected into every onboarding analysis prompt
    (objectives, expected journey, objection playbook — client-provided)."""
    return _analyzer_context_payload()


@router.put("/analyzer-context")
def update_analyzer_context(
    payload: AnalyzerContextUpdate,
    _: None = Depends(require_admin_key),
) -> dict:
    """Replace the analyzer marketing context. Empty content is allowed —
    it simply removes the section from future analysis prompts."""
    content = payload.content.replace("\r\n", "\n")
    if len(content) > _ANALYZER_CONTEXT_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"Context is {len(content):,} characters — the cap is {_ANALYZER_CONTEXT_MAX_CHARS:,}",
        )
    fs.set_analyzer_context(content)
    logger.info("[ANALYTICS] Analyzer context updated (%d chars)", len(content))
    return _analyzer_context_payload()
