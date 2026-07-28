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
    global_device: str | None = Query(
        None,
        description=(
            "Scope the whole screen to ONE global onboarding number, by bridge "
            "session id (e.g. 'smba'). Omit for all numbers combined. The "
            "available ids are returned in the response's globalNumbers list."
        ),
    ),
    funnel_days: int | None = Query(
        None, ge=1, le=3650,
        description="Lookback window for the ONBOARDING FUNNEL only. Omit (with no funnel_from) for all time.",
    ),
    funnel_from: str | None = Query(
        None, description="Funnel-only range start (ISO date/datetime, UTC)",
    ),
    funnel_to: str | None = Query(
        None, description="Funnel-only range end (ISO date/datetime, UTC; defaults to now)",
    ),
    _: None = Depends(require_admin_key),
) -> dict:
    """Platform overview: onboarding funnel, growth trend, aggregate KPIs,
    and the accounts table — optionally scoped to one global number.

    The onboarding funnel carries its OWN date window (funnel_days / funnel_from
    / funnel_to). When none are supplied the funnel covers ALL TIME, while the
    rest of the screen still follows days / from / to.
    """
    start, end = _resolve_range(days, date_from, date_to)
    if funnel_days is not None or funnel_from is not None:
        funnel_start, funnel_end = _resolve_range(funnel_days, funnel_from, funnel_to)
    else:
        funnel_start = funnel_end = None
    return analytics_service.get_platform_overview(
        start, end, include_test=include_test, global_device=global_device,
        funnel_start=funnel_start, funnel_end=funnel_end,
    )


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


# ── Manual WhatsApp pairing (human-support tool) ──────────────────────────────
# Lets a support agent link a business owner's WhatsApp for them when the owner
# can't complete pairing in-chat. Proxies the same bridge calls Sofia uses and,
# when a registered business exists, arms the auto-finalize poll so onboarding
# completes end-to-end the moment the owner links.

# Lazy singleton — OnboardingService's constructor is cheap (client wrappers
# only, no network), but importing it lazily keeps this module light and avoids
# any import-order coupling with the onboarding stack.
_onboarding_singleton = None


def _get_onboarding():
    global _onboarding_singleton
    if _onboarding_singleton is None:
        from app.services.onboarding_service import OnboardingService
        _onboarding_singleton = OnboardingService()
    return _onboarding_singleton


class PairingGenerateRequest(BaseModel):
    phone: str
    mode: str = "code"  # "code" (default) | "qr"


@router.post("/pairing/generate")
async def analytics_pairing_generate(
    payload: PairingGenerateRequest,
    _: None = Depends(require_admin_key),
) -> dict:
    """Generate a pairing CODE (or QR) so a human agent can link a stuck owner's
    WhatsApp. Returns { ok, code | qrDataUrl, sessionId, phone, autoFinalize, … }.
    Errors are returned as { ok: false, error } with HTTP 200 so the dashboard
    can show a friendly message rather than a stack trace."""
    onb = _get_onboarding()
    result = await onb.admin_generate_pairing(payload.phone, mode=payload.mode)
    logger.info(
        "[ANALYTICS] Manual pairing requested phone=%s mode=%s ok=%s",
        payload.phone, payload.mode, result.get("ok"),
    )
    return result


@router.get("/pairing/status")
async def analytics_pairing_status(
    phone: str = Query(..., description="Owner phone (country code + number, digits only)"),
    _: None = Depends(require_admin_key),
) -> dict:
    """Live bridge status for biz-<phone> — used to confirm whether a number is
    stuck (needs_pairing) or already connected before/after generating a code."""
    onb = _get_onboarding()
    return await onb.admin_pairing_status(phone)
