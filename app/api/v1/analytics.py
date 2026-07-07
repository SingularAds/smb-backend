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
