"""Internal analytics service — platform overview + per-business drill-down.

Read-only aggregation layer for the internal (team-only) analytics dashboard.
It NEVER writes to Firestore and never touches the live message pipeline
(customer_ai_service / onboarding_service) — everything is derived from data
the pipeline already persists.

Field names and shapes here were verified against production data
(2026-07-03 audit), which diverges from FIRESTORE_DB_STRUCTURE.md in several
places the normalizers below account for:

  • createdAt / trialEndsAt / booking datetime are a MIX of native Firestore
    timestamps, ISO strings, and missing values           → _parse_dt()
  • bookings use `service` OR `serviceName` (zero overlap) → _booking_service()
  • complaints use `complaint` + `customer{phone,name}`, not the documented
    `message` + `customerPhone`                            → normalize_complaint()
  • conversations (the "legacy" subcollection) is the REAL conversation log:
    transcript[{ts,role,text}] for voice, messages[{role,content}] for
    WhatsApp, plus status/outcome/duration/summary.
  • customer_conversations holds the live WhatsApp AI context (messages[])
    and the CSAT markers. No csatRating exists in production yet — the
    CSAT aggregates are built to return honest empty values.
  • onboarding_sessions.currentStep spans TWO state machines (the current
    conversational one and the retired awaiting_* one)     → funnel_stage()
  • Businesses can be deleted while their subcollections remain (orphans) —
    collection-group scans skip docs whose parent business is unknown.

Range semantics: bookings / conversations / CSAT / growth / funnel are scoped
to the requested date range; open complaints, pending knowledge gaps and
account health flags are point-in-time (current state).

`get_business_detail` is intentionally self-contained and auth-agnostic so it
can later back an SMB-owner-facing endpoint under different auth without a
rearchitecture.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from app import firestore as fs
from app.services import global_numbers
from app.services.attribution import CHANNEL_LABELS

logger = logging.getLogger(__name__)

# Firestore reads dominate request latency and are independent of each other —
# run them concurrently (the google-cloud-firestore sync client is thread-safe
# for reads). Wall time drops from the SUM of the scans to the slowest one.
_io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="analytics-io")


def _parallel(tasks: dict[str, Callable[[], Any]]) -> dict[str, Any]:
    """Run independent read tasks concurrently; raises the first failure."""
    futures = {name: _io_pool.submit(fn) for name, fn in tasks.items()}
    return {name: fut.result() for name, fut in futures.items()}

# Caps so a single dashboard request can't run away on a large tenant.
# _FETCH limits bound the Firestore reads; the table _LIMITs bound the rows
# returned to the UI. Summary counts are computed on everything fetched
# in-range (before the table cap) and a truncated flag is set when either
# cap was hit.
_GROUP_SCAN_LIMIT = 5000
_DETAIL_FETCH_LIMIT = 2000
_DETAIL_BOOKINGS_LIMIT = 300
_DETAIL_CONVERSATIONS_LIMIT = 200
_DETAIL_COMPLAINTS_LIMIT = 100
_DETAIL_KB_LIMIT = 100

_CACHE_TTL_S = 60
_CACHE_MAX_ENTRIES = 64
_overview_cache: dict[tuple, tuple[float, dict]] = {}
_detail_cache: dict[tuple, tuple[float, dict]] = {}


def _cache_get(cache: dict, key: tuple) -> dict | None:
    hit = cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    return None


def _cache_put(cache: dict, key: tuple, value: dict) -> None:
    if len(cache) >= _CACHE_MAX_ENTRIES:
        # Drop the oldest entries (dict preserves insertion order)
        for stale_key in list(cache)[: _CACHE_MAX_ENTRIES // 2]:
            cache.pop(stale_key, None)
    cache[key] = (time.monotonic(), value)


# ── Date handling ─────────────────────────────────────────────────────────────

def _parse_dt(value: Any) -> datetime | None:
    """Normalize any production date representation to a UTC-aware datetime.

    Handles native Firestore timestamps (datetime subclass), ISO strings
    (aware or naive — naive is treated as UTC), and returns None for
    anything unparseable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _in_range(dt: datetime | None, start: datetime, end: datetime) -> bool:
    return dt is not None and start <= dt < end


def resolve_range(
    days: int | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[datetime, datetime]:
    """Resolve query params into a [start, end) UTC window.

    Explicit from/to wins; otherwise `days` back from now (default 30).
    `now` is truncated to the minute so preset windows produce IDENTICAL
    (start, end) keys within the same minute — without this the response
    caches key on a microsecond timestamp and never hit.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if date_from:
        start = _parse_dt(date_from)
        end = _parse_dt(date_to) if date_to else now
        if start is None:
            raise ValueError(f"Unparseable 'from' date: {date_from!r}")
        if end is None:
            raise ValueError(f"Unparseable 'to' date: {date_to!r}")
        # A bare YYYY-MM-DD 'to' means "through the end of that day".
        if date_to and len(date_to.strip()) == 10:
            end = end + timedelta(days=1)
        if end <= start:
            raise ValueError("'to' must be after 'from'")
        return start, end
    span = days if days and days > 0 else 30
    return now - timedelta(days=span), now


# ── Test/demo account detection ───────────────────────────────────────────────

_TEST_ID_MARKERS = ("client-test", "demo", "test")
_TEST_NAME_MARKERS = ("test", "teste", "qa salon", "salão teste", "salao teste")


def is_test_business(biz_id: str, biz: dict) -> bool:
    """Heuristic flag for QA / demo / load-test accounts.

    Verified against production: explicit isQABusiness/isDemoBusiness flags
    exist on 8 docs; the other ~200 QA docs are identifiable by doc-id
    prefixes (QA_*, client-test-*, *demo*) and names ("Salão Teste QA",
    "Test Biz"). Heuristic by necessity — the dashboard exposes it as a
    toggle, never silently.
    """
    if biz.get("isQABusiness") or biz.get("isDemoBusiness"):
        return True
    bid = (biz_id or "").lower()
    if bid.startswith("qa_") or any(m in bid for m in _TEST_ID_MARKERS):
        return True
    name = str(biz.get("name") or "").lower()
    return any(m in name for m in _TEST_NAME_MARKERS)


def is_test_session(session_id: str, session: dict) -> bool:
    """QA/test onboarding session detector — the funnel's include_test toggle
    only filtered the businesses collection, so QA session docs
    (``client-test-*``, ``qa_*``, ``*demo*``) leaked into the funnel as real
    onboarders. onboarding_sessions doc ids are the owner phone/JID for real
    users, or a synthetic QA id for test fixtures.
    """
    if session.get("isQABusiness") or session.get("isDemoBusiness") or session.get("isTest"):
        return True
    sid = (session_id or "").lower()
    if sid.startswith("qa_") or any(m in sid for m in _TEST_ID_MARKERS):
        return True
    return False


# ── Global onboarding numbers ─────────────────────────────────────────────────
# Onboarding can run on one OR several global WhatsApp numbers (see
# app/services/global_numbers.py). Each onboarding session records the bridge
# session it arrived on in ``onboardingDeviceId``; that is how a prospect — and
# by extension the business they became — is attributed to a global number.
# Sessions created before multi-number support have no such field and are
# reported under UNATTRIBUTED_DEVICE so they are never silently dropped.

UNATTRIBUTED_DEVICE = "_unattributed"


def session_device(session: dict) -> str:
    """Which bridge device this onboarding session arrived on.

    Returns the bridge/session id (e.g. "smba"), or UNATTRIBUTED_DEVICE for
    sessions that predate multi-number support. NOTE: a device id is a MUTABLE
    pointer — the phone number behind it can be re-assigned — so it must not be
    used for historical number attribution. Use ``session_number`` for that.
    """
    return (session.get("onboardingDeviceId") or "").strip() or UNATTRIBUTED_DEVICE


def session_number(session: dict) -> str:
    """The immutable global NUMBER this onboarding session arrived on.

    Prefers the phone digits captured on the session at onboarding time
    (``onboardingNumber``) — stable even after the bridge device is later
    re-pointed to a different number. We deliberately do NOT fall back to
    deriving the number from the live device→number registry: that map is
    mutable, so deriving it would silently re-attribute every historical
    session on a device to whatever number the device points to TODAY (the
    bug this function exists to prevent). Legacy sessions that only recorded a
    device id are therefore reported as UNATTRIBUTED_DEVICE until a
    correct-environment backfill stamps their number.
    """
    return (session.get("onboardingNumber") or "").strip() or UNATTRIBUTED_DEVICE


def resolve_target_number(global_device: str | None) -> str | None:
    """Map a selected filter value (a bridge device id, as the dashboard still
    sends) to the immutable number the whole screen should be scoped to.

    - None / ""              → None  (no scoping; all numbers combined)
    - UNATTRIBUTED_DEVICE    → UNATTRIBUTED_DEVICE  (match sessions with no number)
    - a configured device    → its number digits
    - a device with no digits configured, or an unknown id → None, and callers
      fall back to a raw device-id match so the filter still does *something*
      sensible for an exotic config.
    """
    if not global_device:
        return None
    if global_device == UNATTRIBUTED_DEVICE:
        return UNATTRIBUTED_DEVICE
    num = global_numbers.number_for_device(global_device)
    if num:
        return num
    # Not a configured device id. The "not configured" dropdown bucket passes
    # the raw number digits as its filter value (there is no device to name),
    # so accept an all-digits value as the target number directly.
    if global_device.isdigit():
        return global_device
    return None


def global_number_options(
    session_counts: dict[str, int] | None = None,
    account_counts: dict[str, int] | None = None,
) -> list[dict]:
    """The global numbers the dashboard can filter by.

    Always lists every CONFIGURED number (even with zero traffic, so the team
    can see a newly added line exists), plus an "unattributed" bucket when older
    sessions carry no device. Counts are optional and purely informational.
    """
    # Counts are keyed by immutable NUMBER (or UNATTRIBUTED_DEVICE for sessions
    # with no captured number) — see session_number(). The filter key exposed to
    # the frontend stays the bridge ``deviceId`` for backward compatibility; the
    # backend resolves it to a number (resolve_target_number).
    session_counts = session_counts or {}
    account_counts = account_counts or {}
    options: list[dict] = []
    seen_numbers: set[str] = set()
    for device_id, number in global_numbers.registry():
        num_key = number or ""
        seen_numbers.add(num_key)
        options.append({
            "deviceId": device_id,
            "number": number or None,
            "label": number or device_id,
            "sessions": session_counts.get(num_key, 0),
            "accounts": account_counts.get(num_key, 0),
        })
    # Surface traffic on numbers that are no longer configured (a number whose
    # device was renamed/removed) rather than hiding it — otherwise the counts
    # would silently not add up.
    for num_key in sorted(set(session_counts) | set(account_counts)):
        if num_key in seen_numbers or num_key == UNATTRIBUTED_DEVICE or not num_key:
            continue
        options.append({
            # No configured device backs this number; expose the number itself
            # as the filter value (deviceId stays a non-null, unique, selectable
            # key — resolve_target_number accepts raw digits).
            "deviceId": num_key,
            "number": num_key,
            "label": f"{num_key} (not configured)",
            "sessions": session_counts.get(num_key, 0),
            "accounts": account_counts.get(num_key, 0),
        })
    unattributed = session_counts.get(UNATTRIBUTED_DEVICE, 0)
    unattributed_accounts = account_counts.get(UNATTRIBUTED_DEVICE, 0)
    if unattributed or unattributed_accounts:
        options.append({
            "deviceId": UNATTRIBUTED_DEVICE,
            "number": None,
            "label": "Not recorded (before per-number capture)",
            "sessions": unattributed,
            "accounts": unattributed_accounts,
        })
    return options


# ── Onboarding funnel ─────────────────────────────────────────────────────────

# Ordered funnel stages. A session counts in the deepest stage it has reached;
# funnel counts are cumulative (stage N includes everyone at stage >= N).
#
# The order mirrors the REAL flow: the business doc is created at
# details-confirmation, BEFORE WhatsApp pairing — so "completed" (business
# created / onboarded) comes 3rd and "whatsapp_paired" is the deepest stage.
# Pairing is judged by the business doc's own waSessionId (the same source as
# the accounts table's paired column), never inferred from merely having a
# business — an owner who created a business but abandoned at the QR step is
# Onboarded, NOT Paired.
FUNNEL_STAGES = ("started", "details_collected", "completed", "whatsapp_paired")

# currentStep → stage, covering BOTH state machines seen in production
# (current conversational machine + retired awaiting_* machine).
# No step maps to "completed": that stage means "business doc created" and is
# derived from the business itself (funnel_stage's businessId bump + the
# overview's business cross-check), not from a conversation step.
# calendar_setup/call_forwarding/complete only exist after a successful pair,
# so they map to the deepest stage.
_STEP_STAGE = {
    # current machine
    "conversing": "started",
    "location_request": "started",
    "places_pick": "started",
    "website_confirm": "started",
    "recepte_confirm": "started",
    "referral_offer": "details_collected",
    "pairing": "details_collected",
    "pairing_mode_choice": "details_collected",
    "pairing_qr_active": "details_collected",
    "pairing_scam_warning": "details_collected",
    "calendar_setup": "whatsapp_paired",
    "call_forwarding": "whatsapp_paired",
    "complete": "whatsapp_paired",
    "post_onboarding": "whatsapp_paired",
    # retired machine (docs still exist in production)
    "awaiting_website": "started",
    "awaiting_business_name": "started",
    "awaiting_services": "started",
    "awaiting_confirm": "details_collected",
    "awaiting_pairing_done": "details_collected",
    "awaiting_calendar": "whatsapp_paired",
    "awaiting_forwarding": "whatsapp_paired",
}


def funnel_stage(session: dict) -> str:
    """Deepest funnel stage an onboarding session has reached."""
    step = (session.get("currentStep") or "").strip()
    stage = _STEP_STAGE.get(step, "started")
    # A session that already produced a business doc is at least Onboarded,
    # whatever step it is parked on (pairing state stays whatever the step
    # says — the overview additionally cross-checks it against the business
    # doc's waSessionId, the authoritative pairing source).
    if stage in ("started", "details_collected") and session.get("businessId"):
        stage = "completed"
    return stage


def is_registration_session(session: dict) -> bool:
    """True when the session belongs to a prospective SMB owner actually
    REGISTERING — the population the onboarding funnel measures.

    onboarding_sessions also holds demo sessions (``mode``/``temporaryMode``/
    ``salesPhase`` == "demo"): the AI roleplays a customer BOOKING conversation
    so the visitor can see the product. Those are end-customer simulations,
    not registrations, and must not inflate the funnel. A session currently in
    demo mode still counts when it shows real registration progress — an owner
    who paused onboarding to try the demo (``resumeOnboardingAfterDemo``/
    ``onboardingContextBeforeDemo``), has extracted ``businessData``, created
    a business, or already advanced past the conversational stage.
    """
    in_demo = (
        session.get("mode") == "demo"
        or session.get("temporaryMode") == "demo"
        or session.get("salesPhase") == "demo"
    )
    if not in_demo:
        return True
    if session.get("businessId") or session.get("businessData"):
        return True
    if session.get("resumeOnboardingAfterDemo") or session.get("onboardingContextBeforeDemo"):
        return True
    step = (session.get("currentStep") or "").strip()
    return _STEP_STAGE.get(step, "started") != "started"


# ── Acquisition-by-channel (which marketing channel onboarding prospects came
# from — Meta ads, website, organic, …). Reuses the same FUNNEL_STAGES so each
# channel shows the same started→details→paired(connected)→completed steps. ──

_ACQ_STAGE_LABELS = {
    "started": "Started chatting",
    "details_collected": "Business details collected",
    "completed": "Onboarded",
    "whatsapp_paired": "Connected to platform",
}


def session_channel(session: dict) -> str | None:
    """Canonical acquisition channel for an onboarding session, or None when the
    session pre-dates attribution capture (no attribution field)."""
    attr = session.get("attribution")
    if isinstance(attr, dict) and attr.get("channel"):
        return str(attr["channel"])
    return None


def _build_acquisition(
    channel_records: dict[str, list[tuple[str, dict]]],
    created_by_channel: dict[str, int],
) -> list[dict]:
    """Turn per-channel (stage, session-entry) records into the dashboard's
    acquisition blocks — one cumulative mini-funnel per channel.

    Mirrors the main funnel's rules: counts are cumulative (reaching a deep stage
    counts for all earlier stages), 'completed' is cross-checked against businesses
    of that channel created in range, and monotonicity is enforced so a channel's
    funnel never inverts.
    """
    out: list[dict] = []
    for channel, records in channel_records.items():
        counts = {s: 0 for s in FUNNEL_STAGES}
        per_stage: dict[str, list[dict]] = {s: [] for s in FUNNEL_STAGES}
        for stage, entry in records:
            for s in FUNNEL_STAGES[: FUNNEL_STAGES.index(stage) + 1]:
                counts[s] += 1
            per_stage[stage].append(entry)

        # Authoritative 'completed' from businesses of this channel created in
        # range (sessions may be deleted after onboarding finishes).
        counts["completed"] = max(counts["completed"], created_by_channel.get(channel, 0))
        # Enforce funnel monotonicity (started ≥ details ≥ onboarded ≥ paired).
        counts["completed"] = max(counts["completed"], counts["whatsapp_paired"])
        counts["details_collected"] = max(counts["details_collected"], counts["completed"])
        counts["started"] = max(counts["started"], counts["details_collected"])

        # Cumulative per-stage session lists (a completed owner also appears under
        # every earlier stage) — the 'started' bucket is therefore everyone.
        cumulative: dict[str, list[dict]] = {}
        for i, sk in enumerate(FUNNEL_STAGES):
            merged: list[dict] = []
            for s in FUNNEL_STAGES[i:]:
                merged.extend(per_stage[s])
            cumulative[sk] = sorted(merged, key=lambda x: x["startedAt"] or "", reverse=True)

        out.append({
            "channel": channel,
            "label": CHANNEL_LABELS.get(channel, channel.replace("_", " ").title()),
            "total": counts["started"],
            "stages": [
                {"stage": s, "label": _ACQ_STAGE_LABELS[s], "count": counts[s]}
                for s in FUNNEL_STAGES
            ],
            "sessions": cumulative["started"],  # all prospects for this channel
        })

    # Paid ad channels first (the client's focus), then by volume desc.
    out.sort(key=lambda c: (0 if str(c["channel"]).endswith("_ads") else 1, -c["total"]))
    return out


def _session_activity_dt(session: dict) -> datetime | None:
    ts = session.get("timestamps") or {}
    return _parse_dt(ts.get("lastActivityAt")) or _parse_dt(ts.get("startedAt"))


def _session_started_dt(session: dict) -> datetime | None:
    ts = session.get("timestamps") or {}
    return _parse_dt(ts.get("startedAt")) or _parse_dt(ts.get("lastActivityAt"))


# ── Normalizers ───────────────────────────────────────────────────────────────

def _booking_service(b: dict) -> str | None:
    # Production split: 246 docs use `service`, 163 use `serviceName`, 0 both.
    return b.get("serviceName") or b.get("service") or None


def _booking_dt(b: dict) -> datetime | None:
    return _parse_dt(b.get("datetime") or b.get("dateTime") or b.get("date"))


def normalize_booking(raw: dict, doc_id: str, business_id: str) -> dict:
    dt = _booking_dt(raw)
    return {
        "id": doc_id,
        "businessId": business_id,
        "customerName": raw.get("customerName") or raw.get("customer_name") or None,
        "customerPhone": raw.get("customerPhone") or raw.get("customer_phone") or None,
        "service": _booking_service(raw),
        "partySize": raw.get("partySize") or raw.get("party_size") or None,
        "datetime": _iso(dt),
        "status": raw.get("status") or "unknown",
        "source": raw.get("source") or raw.get("channel") or None,
        "notes": raw.get("notes") or raw.get("specialRequests") or None,
        "createdAt": _iso(_parse_dt(raw.get("createdAt")) or dt),
    }


def _normalize_transcript(raw: dict) -> list[dict]:
    """Return [{role, text, ts?}] from any production transcript shape.

    Voice logs: transcript = [{ts, role, text}] (or a raw "AI: … Customer: …"
    string on 3 legacy docs). WhatsApp logs: messages = [{role, content}].
    """
    transcript = raw.get("transcript")
    if isinstance(transcript, list) and transcript:
        out = []
        for turn in transcript:
            if not isinstance(turn, dict):
                continue
            out.append({
                "role": turn.get("role") or "user",
                "text": turn.get("text") or turn.get("content") or "",
                "ts": _iso(_parse_dt(turn.get("ts"))),
            })
        return out
    if isinstance(transcript, str) and transcript.strip():
        out = []
        for chunk in transcript.replace("Customer:", "\nCustomer:").replace("AI:", "\nAI:").split("\n"):
            chunk = chunk.strip()
            if not chunk:
                continue
            role = "assistant" if chunk.startswith(("AI:", "Assistant:")) else "user"
            out.append({"role": role, "text": chunk.split(":", 1)[-1].strip(), "ts": None})
        return out
    messages = raw.get("messages")
    if isinstance(messages, list):
        return [
            {
                "role": m.get("role") or "user",
                "text": m.get("content") or m.get("text") or "",
                "ts": None,
            }
            for m in messages
            if isinstance(m, dict)
        ]
    return []


def _onboarding_turns(session: dict) -> list[dict]:
    """The owner↔Sofia turns stored on an onboarding session.

    ``conversationHistory`` is ``[{role, content}]`` (no per-turn timestamps —
    the pipeline never recorded them), so ``ts`` is always None. Non-string
    content (rare tool payloads) is skipped rather than rendered as junk.
    """
    history = session.get("conversationHistory")
    if not isinstance(history, list):
        return []
    turns: list[dict] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        text = msg.get("content")
        if text is None:
            text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        turns.append({
            "role": "assistant" if msg.get("role") == "assistant" else "user",
            "text": text,
            "ts": None,
        })
    return turns


def _transcript_turns(owner_phone: str | None) -> list[dict]:
    """Turns from the append-only ``onboarding_transcripts`` archive.

    The archive records every inbound/outbound message centrally (with real
    per-turn timestamps), unlike ``conversationHistory`` which the structured
    onboarding steps never appended to. Empty list on any failure so callers
    can fall back to the session history.
    """
    if not owner_phone:
        return []
    try:
        from app.services.onboarding_transcript import list_transcript
        entries = list_transcript(owner_phone)
    except Exception:
        return []
    turns: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        text = e.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        turns.append({
            "role": "assistant" if e.get("role") == "assistant" else "user",
            "text": text,
            "ts": e.get("ts") or None,
        })
    return turns


def _build_onboarding_chat(session: dict | None, owner_phone: str | None) -> dict | None:
    """Package the onboarding conversation for the dashboard, or None.

    Returned even when the owner never paired — that is exactly the case the
    team needs to read. None only when there is no session or it holds no
    renderable messages.

    Prefers the transcript archive (complete, timestamped). Sessions that ran
    before the archive existed fall back to ``conversationHistory``; for
    sessions spanning the cutover, whichever source holds MORE turns wins.
    """
    archive_turns = _transcript_turns(owner_phone)
    session_turns = _onboarding_turns(session) if session else []
    turns = archive_turns if len(archive_turns) >= len(session_turns) else session_turns
    if not turns:
        return None
    session = session or {}
    device_id = session_device(session)
    _num = session_number(session)
    started = _session_started_dt(session)
    return {
        "ownerPhone": owner_phone,
        "currentStep": session.get("currentStep") or None,
        "language": session.get("language") or None,
        "startedAt": _iso(started),
        "lastActivityAt": _iso(_session_activity_dt(session)),
        "messageCount": len(turns),
        "ownerMessageCount": sum(1 for t in turns if t["role"] == "user"),
        "onboardingDeviceId": device_id,
        # Immutable captured number (consistent with the profile header and the
        # overview); NOT a live device→number lookup that drifts after a re-point.
        "onboardingNumber": None if _num == UNATTRIBUTED_DEVICE else _num,
        "transcriptSource": "archive" if turns is archive_turns else "session",
        "turns": turns,
    }


def get_onboarding_chat(phone: str) -> dict | None:
    """The owner↔Sofia onboarding conversation for one phone, or None.

    Reads the append-only ``onboarding_transcripts`` archive (every inbound
    owner message AND every outbound Sofia message, templates included),
    falling back to the session's ``conversationHistory`` for pre-archive
    sessions. Works for prospects who never created a business AND for those
    whose onboarding_sessions doc was later deleted — the archive survives it.
    Returns the same shape as the business-detail ``onboardingChat`` field.
    """
    if not phone or not phone.strip():
        return None
    session = fs.get_onboarding_session(phone)
    return _build_onboarding_chat(session, phone.strip())


def _conversation_channel(raw: dict) -> str:
    if raw.get("channel"):
        return raw["channel"]
    source = raw.get("source") or ""
    if "voice" in source or raw.get("callSid"):
        return "voice"
    return "unknown"


def normalize_conversation(raw: dict, doc_id: str, business_id: str) -> dict:
    started = _parse_dt(raw.get("startedAt") or raw.get("createdAt"))
    return {
        "id": doc_id,
        "businessId": business_id,
        "channel": _conversation_channel(raw),
        "customerPhone": raw.get("customerPhone") or None,
        "customerName": raw.get("callerName") or raw.get("customerName") or None,
        "startedAt": _iso(started),
        "endedAt": _iso(_parse_dt(raw.get("endedAt"))),
        "durationSeconds": raw.get("duration") if isinstance(raw.get("duration"), (int, float)) else None,
        "status": raw.get("status") or None,
        "outcome": raw.get("outcome") or None,
        "summary": raw.get("summary") or None,
        "bookingId": raw.get("bookingId") or None,
        "messageCount": len(raw.get("transcript") or raw.get("messages") or []) or None,
        "live": False,
    }


def normalize_live_conversation(raw: dict, doc_id: str, business_id: str) -> dict:
    """A customer_conversations doc (live WhatsApp AI context) shaped like a
    conversation row so both sources can share one table."""
    last = _parse_dt(raw.get("lastMessageAt"))
    return {
        "id": f"live-{doc_id}",
        "businessId": business_id,
        "channel": "whatsapp",
        "customerPhone": raw.get("customerPhone") or doc_id,
        "customerName": raw.get("customerName") or None,
        "startedAt": _iso(last),  # only the last-activity timestamp is stored
        "endedAt": None,
        "durationSeconds": None,
        "status": "active",
        "outcome": None,
        "summary": None,
        "bookingId": None,
        "messageCount": len(raw.get("messages") or []) or None,
        "live": True,
        "aiPaused": bool(raw.get("aiPaused")),
    }


def normalize_complaint(raw: dict, doc_id: str, business_id: str) -> dict:
    customer = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}
    return {
        "id": doc_id,
        "businessId": business_id,
        "text": raw.get("complaint") or raw.get("message") or "",
        "type": raw.get("complaintType") or None,
        "customerPhone": customer.get("phone") or raw.get("customerPhone") or None,
        "customerName": customer.get("name") or None,
        "bookingId": raw.get("bookingId") or None,
        "status": raw.get("status") or "open",
        "createdAt": _iso(_parse_dt(raw.get("createdAt"))),
    }


def normalize_kb_entry(raw: dict, doc_id: str, business_id: str) -> dict:
    return {
        "id": doc_id,
        "businessId": business_id,
        "shortCode": raw.get("shortCode") or None,
        "question": raw.get("question") or "",
        "answer": raw.get("answer") or None,
        "status": raw.get("status") or "pending_review",
        "useCount": raw.get("useCount") or 0,
        "createdAt": _iso(_parse_dt(raw.get("createdAt"))),
        "confirmedAt": _iso(_parse_dt(raw.get("confirmedAt"))),
    }


def _csat_points(customer_convos: Iterable[dict], customers: Iterable[dict]) -> list[dict]:
    """CSAT data points from both persistence spots (verified empty in
    production today — this returns [] until ratings arrive)."""
    points: list[dict] = []
    for c in customer_convos:
        rating = c.get("csatRating")
        if isinstance(rating, (int, float)) and 1 <= rating <= 5:
            points.append({
                "rating": float(rating),
                "at": _iso(_parse_dt(c.get("csatRatedAt"))),
                "customerPhone": c.get("customerPhone") or c.get("id"),
            })
    for cust in customers:
        for h in cust.get("csatHistory") or []:
            if isinstance(h, dict) and isinstance(h.get("rating"), (int, float)):
                points.append({
                    "rating": float(h["rating"]),
                    "at": _iso(_parse_dt(h.get("at"))),
                    "customerPhone": cust.get("phone") or cust.get("id"),
                })
    # Dedupe (same rating event can exist in both spots), newest last.
    seen: set[tuple] = set()
    unique = []
    for p in sorted(points, key=lambda p: p["at"] or ""):
        key = (p["customerPhone"], p["at"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _daily_series(
    start: datetime,
    end: datetime,
    series: dict[str, dict[str, int]],
) -> list[dict]:
    """Expand per-day counters into a dense day-by-day list over [start, end].

    ``series`` maps output field name -> {"YYYY-MM-DD": count}. Days with no
    events are emitted as 0 so charts get a continuous axis.
    """
    out: list[dict] = []
    day = start.date()
    last = end.date()
    while day <= last:
        key = day.isoformat()
        row: dict = {"date": key}
        for name, counts in series.items():
            row[name] = counts.get(key, 0)
        out.append(row)
        day += timedelta(days=1)
    return out


# ── Business profile row ──────────────────────────────────────────────────────

def _trial_days_left(biz: dict, now: datetime) -> int | None:
    ends = _parse_dt(biz.get("trialEndsAt"))
    if ends is None:
        return None
    return (ends - now).days


def business_profile(biz_id: str, biz: dict, now: datetime) -> dict:
    """Normalized profile fields shared by the accounts table and the
    detail header. Only fields verified to exist in production."""
    created = _parse_dt(biz.get("createdAt"))
    return {
        "id": biz_id,
        "name": biz.get("name") or "(unnamed)",
        "ownerName": biz.get("ownerName") or None,
        "ownerPhone": biz.get("ownerPhone") or None,
        "businessType": biz.get("businessType") or None,
        "address": biz.get("address") or None,
        "plan": biz.get("plan") or None,
        "status": biz.get("status") or None,
        "primaryLanguage": biz.get("primaryLanguage") or None,
        "createdAt": _iso(created),
        "trialEndsAt": _iso(_parse_dt(biz.get("trialEndsAt"))),
        "trialDaysLeft": _trial_days_left(biz, now),
        "whatsappPaired": bool(biz.get("waSessionId")),
        "waPhoneNumber": biz.get("waPhoneNumber") or None,
        "isTest": is_test_business(biz_id, biz),
        # Global-number attribution denormalized onto the business doc at
        # creation (onboarding_service) so it survives session deletion.
        # None on businesses created before this capture existed — callers
        # fall back to the owner's onboarding session for those.
        "onboardingDeviceId": (biz.get("onboardingDeviceId") or "").strip() or None,
        "onboardingNumber": (
            "".join(ch for ch in str(biz.get("onboardingNumber") or "") if ch.isdigit())
            or None
        ),
    }


# ── Firestore reads (all read-only) ──────────────────────────────────────────

def _stream_collection(name: str) -> list[tuple[str, dict]]:
    return [(doc.id, doc.to_dict() or {}) for doc in fs._db().collection(name).stream()]


def _stream_group(
    sub: str,
    limit: int = _GROUP_SCAN_LIMIT,
    fields: list[str] | None = None,
) -> list[tuple[str, str, dict]]:
    """Collection-group scan → [(business_id, doc_id, data)].

    Only docs directly under businesses/{id}/{sub} are returned; anything
    else (or malformed paths) is skipped. Pass ``fields`` to run a
    PROJECTION query — aggregates only need a handful of fields, and
    skipping heavyweight payloads (conversation transcripts especially)
    cuts transfer time by an order of magnitude.
    """
    query = fs._db().collection_group(sub)
    if fields:
        query = query.select(fields)
    out: list[tuple[str, str, dict]] = []
    for doc in query.limit(limit).stream():
        parts = doc.reference.path.split("/")
        if len(parts) == 4 and parts[0] == "businesses" and parts[2] == sub:
            out.append((parts[1], doc.id, doc.to_dict() or {}))
    return out


def _stream_subcollection(
    business_id: str,
    sub: str,
    limit: int,
    fields: list[str] | None = None,
    order_desc_by: str | None = None,
) -> list[tuple[str, dict]]:
    query = (
        fs._db().collection("businesses").document(business_id).collection(sub)
    )
    if fields:
        query = query.select(fields)
    if order_desc_by:
        query = query.order_by(
            order_desc_by, direction=fs.fb_firestore.Query.DESCENDING,
        )
    return [(doc.id, doc.to_dict() or {}) for doc in query.limit(limit).stream()]


# ── Platform overview ─────────────────────────────────────────────────────────

def get_platform_overview(
    start: datetime,
    end: datetime,
    include_test: bool = False,
    global_device: str | None = None,
    funnel_start: datetime | None = None,
    funnel_end: datetime | None = None,
) -> dict:
    """Funnel + growth + aggregate KPIs + accounts table for Screen 1.

    ``global_device`` scopes the ENTIRE screen to prospects/businesses that came
    in on one global onboarding number. The dashboard still passes it as a bridge
    session id (e.g. "smba", or UNATTRIBUTED_DEVICE); internally we resolve it to
    that number's IMMUTABLE phone digits (resolve_target_number) and match each
    session on the number it captured at onboarding time — never on the mutable
    device pointer, so re-pointing a device to a new number can no longer make
    old sessions leak into the new number's view. None = all numbers combined
    (the default, and exactly the previous behaviour).

    ``funnel_start``/``funnel_end`` scope ONLY the onboarding funnel (and the
    demo-session count shown beside it). Both None — the default — means ALL
    TIME: every onboarding session ever, including sessions too old to carry a
    startedAt. Everything else on the screen stays on the main ``start``/``end``
    window, so the funnel can be explored over a different period without
    disturbing the KPIs, growth, acquisition or accounts table.
    """
    global_device = (global_device or "").strip() or None
    funnel_all_time = funnel_start is None
    cache_key = (
        start.isoformat(), end.isoformat(), include_test, global_device,
        funnel_start.isoformat() if funnel_start else None,
        funnel_end.isoformat() if funnel_end else None,
    )
    cached = _cache_get(_overview_cache, cache_key)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)

    # All reads are independent — fetch them concurrently, and as PROJECTION
    # queries wherever the overview only aggregates (it never renders
    # transcripts, messages, or profile blobs from these scans).
    fetch_start = time.monotonic()
    loaded = _parallel({
        "businesses": lambda: _stream_collection("businesses"),
        "sessions": lambda: _stream_collection("onboarding_sessions"),
        "bookings": lambda: _stream_group(
            "bookings",
            # customerPhone/customer_phone let us attribute a WhatsApp conversation
            # to a booking (booking-conversion now spans WhatsApp, not just voice).
            fields=["createdAt", "datetime", "dateTime", "date", "status",
                    "customerPhone", "customer_phone"],
        ),
        "conversations": lambda: _stream_group(
            "conversations",
            fields=["startedAt", "createdAt", "outcome"],
        ),
        "complaints": lambda: _stream_group("complaints", fields=["status"]),
        "kb_entries": lambda: _stream_group("knowledge", fields=["status"]),
        "live_convos": lambda: _stream_group(
            "customer_conversations",
            fields=["lastMessageAt", "csatRating", "csatRatedAt"],
        ),
        "customers": lambda: _stream_group(
            "customers", fields=["lastSeen", "csatHistory"],
        ),
    })
    logger.info(
        "[ANALYTICS] overview parallel fetch took %.2fs",
        time.monotonic() - fetch_start,
    )

    # Businesses + profiles
    all_businesses = loaded["businesses"]
    profiles: dict[str, dict] = {bid: business_profile(bid, b, now) for bid, b in all_businesses}

    # ── Global-number attribution ─────────────────────────────────────────────
    # Two sources, in priority order:
    #   1. The business doc's own denormalized onboardingNumber/DeviceId
    #      (stamped at creation — survives session deletion).
    #   2. The owner's onboarding session (legacy businesses created before the
    #      stamp existed; the session can be wiped, hence source 1).
    # Attribution/filtering key on the immutable NUMBER, never the mutable
    # device pointer — see session_number().
    sessions = loaded["sessions"]
    number_by_owner: dict[str, str] = {}
    device_by_owner: dict[str, str] = {}
    for _sess_doc_id, _sess in sessions:
        _owner = (_sess.get("ownerPhone") or _sess_doc_id or "").strip()
        if _owner:
            number_by_owner[_owner] = session_number(_sess)
            device_by_owner[_owner] = session_device(_sess)

    def account_number(profile: dict) -> str:
        return number_by_owner.get(profile.get("ownerPhone") or "", UNATTRIBUTED_DEVICE)

    def account_device(profile: dict) -> str:
        return device_by_owner.get(profile.get("ownerPhone") or "", UNATTRIBUTED_DEVICE)

    for _bid, _p in profiles.items():
        # Business-doc stamp wins (survives session deletion); session fallback.
        if not _p["onboardingDeviceId"]:
            _p["onboardingDeviceId"] = account_device(_p)
        # Public field: real digits, or None when neither the business doc nor
        # the owner's session captured a number. This IS the matching key (no
        # internal sentinel field is added to the profile, so nothing extra
        # leaks into the accounts payload).
        if not _p["onboardingNumber"]:
            _num = account_number(_p)
            _p["onboardingNumber"] = None if _num == UNATTRIBUTED_DEVICE else _num

    # What the selected filter value scopes the screen to (immutable number).
    target_number = resolve_target_number(global_device)

    def _profile_matches(p: dict) -> bool:
        if not global_device:
            return True
        if target_number is None:
            return p["onboardingDeviceId"] == global_device  # exotic config fallback
        if target_number == UNATTRIBUTED_DEVICE:
            return p["onboardingNumber"] is None
        return p["onboardingNumber"] == target_number

    # Test filter first, so the per-number counts in the dropdown reflect the
    # same population the table shows (minus the number filter itself).
    if include_test:
        base_visible_ids = set(profiles)
    else:
        base_visible_ids = {bid for bid, p in profiles.items() if not p["isTest"]}
    device_account_counts: dict[str, int] = defaultdict(int)
    for bid in base_visible_ids:
        _num = profiles[bid]["onboardingNumber"]
        device_account_counts[_num if _num is not None else UNATTRIBUTED_DEVICE] += 1

    if global_device:
        visible_ids = {
            bid for bid in base_visible_ids
            if _profile_matches(profiles[bid])
        }
    else:
        visible_ids = base_visible_ids

    def _visible(biz_id: str) -> bool:
        return biz_id in visible_ids

    # Onboarding sessions → funnel (sessions started in range; when a session
    # has no startedAt at all we include it rather than lose it). ONLY owner
    # registration sessions count — demo/booking-roleplay sessions are the
    # end-customer simulation and are excluded (tracked separately).
    biz_by_owner: dict[str, dict] = {}
    for bid, p in profiles.items():
        if p.get("ownerPhone") and not p.get("isTest"):
            biz_by_owner[p["ownerPhone"]] = p

    stage_counts = {s: 0 for s in FUNNEL_STAGES}
    # Per-global-number session counts for the number picker. Counted BEFORE the
    # global_device filter so the picker always shows every number's true volume.
    device_session_counts: dict[str, int] = defaultdict(int)
    # Per-stage session detail lists for the drill-down modal.
    # Each entry holds enough info to identify and contact the owner.
    stage_sessions: dict[str, list[dict]] = {s: [] for s in FUNNEL_STAGES}
    # Per-acquisition-channel records: channel -> [(stage, session-entry)] used to
    # build the "acquisition by channel" section (Meta ads, website, organic …).
    channel_records: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    active_sessions = 0
    excluded_demo_sessions = 0

    def _in_funnel_window(dt: datetime | None) -> bool:
        """Is this session inside the FUNNEL's own window?

        All-time (the default) takes everything, including sessions with no
        startedAt — with no window to attribute them to, "all data" genuinely
        means all of them. A concrete range keeps the dated-activity-only rule
        below, so a timeless doc can never land in every range at once.
        """
        if funnel_all_time:
            return True
        return _in_range(dt, funnel_start, funnel_end)

    for doc_id, sess in sessions:
        # A date-ranged funnel must be built from DATED activity only. A session
        # with no startedAt/lastActivityAt cannot be attributed to any window, so
        # it must NOT be counted "today" (or in every range). Previously the guard
        # was `started is not None and not _in_range(...)`, which let null-timestamp
        # docs fall through into every range and inflate the count (e.g. "5 started
        # today" when only 1 person actually started — the other 4 were timeless
        # QA/legacy docs). `_in_range(None, ...)` is False, so this skips both
        # out-of-range and timeless docs.
        started = _session_started_dt(sess)
        # Two independent windows: the main one (acquisition + the per-number
        # picker) and the funnel's own (which defaults to all time).
        in_main = _in_range(started, start, end)
        in_funnel = _in_funnel_window(started)
        if not in_main and not in_funnel:
            continue
        # QA/test onboarding sessions (client-test-*, qa_*, *demo*) are not real
        # onboarders; the include_test toggle only filtered businesses before, so
        # these leaked into the funnel. Always exclude them here.
        if is_test_session(doc_id, sess):
            continue
        sess_device = session_device(sess)
        sess_number = session_number(sess)
        if target_number is not None:
            device_match = not global_device or sess_number == target_number
        else:
            device_match = not global_device or sess_device == global_device
        if not is_registration_session(sess):
            # Reported next to the funnel, so it follows the funnel's window.
            if in_funnel and device_match:
                excluded_demo_sessions += 1
            continue
        # Unfiltered per-number volume (drives the number picker) — main window.
        # Keyed by immutable number so the picker counts match the filter result.
        if in_main:
            device_session_counts[sess_number] += 1
        # Scope to one global number when asked.
        if not device_match:
            continue
        if in_funnel:
            active_sessions += 1
        stage = funnel_stage(sess)

        # Cross-reference with the business collection: when a business exists
        # for this owner, the business DOC is authoritative for the last two
        # stages — Onboarded always (the doc exists), Paired only when the doc
        # actually holds a waSessionId. Same source as the accounts table's
        # paired column, so the funnel and accounts can never disagree about
        # pairing (business creation happens BEFORE pairing in the flow, so a
        # created-but-unpaired business is Onboarded, not Paired — regardless
        # of what step the session is parked on). The session's OWN businessId
        # wins over the owner-phone map: an owner who re-onboards has several
        # businesses under one phone, and this session must be judged against
        # the business it actually created.
        owner_phone = sess.get("ownerPhone")
        _sess_biz_id = sess.get("businessId")
        if _sess_biz_id and _sess_biz_id in profiles and not profiles[_sess_biz_id]["isTest"]:
            biz_data = profiles[_sess_biz_id]
        else:
            biz_data = biz_by_owner.get(owner_phone or "")
        if biz_data:
            stage = "whatsapp_paired" if biz_data.get("whatsappPaired") else "completed"

        # cumulative: reaching a deep stage counts for all earlier stages
        # (funnel bars follow the FUNNEL window)
        if in_funnel:
            for s in FUNNEL_STAGES[: FUNNEL_STAGES.index(stage) + 1]:
                stage_counts[s] += 1

        # Record owner in their deepest stage bucket for the drill-down modal.
        # The onboarding_sessions DOC ID is the owner phone/JID — use it as the
        # identifier fallback (previously the loop discarded doc_id and read a
        # non-existent sess['id'] field, rendering these rows as "unknown").
        biz_name = (biz_data or {}).get("name") or (
            (sess.get("businessData") or {}).get("businessName")
        )
        attribution = sess.get("attribution") if isinstance(sess.get("attribution"), dict) else {}
        entry = {
            "phone": owner_phone or sess.get("id") or doc_id,
            "name": sess.get("pushName") or (biz_data or {}).get("ownerName"),
            "businessName": biz_name,
            "currentStep": sess.get("currentStep"),
            "startedAt": started.isoformat() if started else None,
            "businessId": (biz_data or {}).get("id"),
            # Which global onboarding number this prospect came in on (immutable
            # captured number; device id kept for routing/debug only).
            "onboardingDeviceId": sess_device,
            "onboardingNumber": None if sess_number == UNATTRIBUTED_DEVICE else sess_number,
            # Acquisition context (present only for attributed sessions).
            "channel": attribution.get("channel"),
            "adId": attribution.get("adId"),
            "campaign": attribution.get("campaign"),
        }
        # Drill-down list behind each funnel bar — follows the funnel window.
        if in_funnel:
            stage_sessions[stage].append(entry)

        # Group into the acquisition-by-channel section (skip unattributed
        # sessions predating attribution capture). That card is NOT part of the
        # funnel, so it stays on the main window.
        if in_main:
            channel = session_channel(sess)
            if channel:
                channel_records[channel].append((stage, entry))

    # "Completed" is authoritative from businesses created in range (sessions
    # may be deleted after onboarding finishes). The funnel ALWAYS excludes
    # test businesses regardless of the include_test toggle: bulk-created QA
    # accounts never went through the onboarding pipeline, and counting them
    # here would show a completed stage larger than the stages before it.
    businesses_created = [
        p for bid, p in profiles.items()
        if _visible(bid) and _in_range(_parse_dt(p["createdAt"]), start, end)
    ]
    funnel_completed_profiles = [
        p for p in profiles.values()
        if not p["isTest"]
        # This cross-check feeds the FUNNEL, so it uses the funnel's window —
        # otherwise an all-time funnel would be capped by a 30-day business count.
        and _in_funnel_window(_parse_dt(p["createdAt"]))
        # Respect the global-number filter, or "completed" would count owners
        # who onboarded on a DIFFERENT number and invert the scoped funnel.
        and _profile_matches(p)
    ]

    # Some of the above are "completed" ONLY via this cross-check: no session
    # in the loop above represents them — the owner's onboarding_sessions doc
    # was deleted, was filtered out by the number scope, or (an owner who
    # re-onboarded) now points at a DIFFERENT business under the same phone.
    # Keyed by BUSINESS id, not owner phone: the funnel counts onboarding
    # journeys, and one owner can legitimately produce several businesses.
    # Being Onboarded necessarily means having passed through
    # started/details_collected too, so count each of them into those stages
    # as well — otherwise those bars would undercount relative to "completed"
    # and relative to the drill-down rows built below (which cascade into
    # every earlier stage's list). Paired is NOT implied by Onboarded: it
    # comes from the business doc's own pairing state, the same source as
    # the accounts table.
    session_backed_business_ids = {
        entry["businessId"]
        for entries in stage_sessions.values()
        for entry in entries
        if entry.get("businessId")
    }
    reconstructed_profiles = [
        p for p in funnel_completed_profiles
        if p["id"] not in session_backed_business_ids
    ]
    for p in reconstructed_profiles:
        stage_counts["started"] += 1
        stage_counts["details_collected"] += 1
        stage_counts["completed"] += 1
        if p.get("whatsappPaired"):
            stage_counts["whatsapp_paired"] += 1
    stage_counts["completed"] = max(stage_counts["completed"], len(funnel_completed_profiles))

    # Authoritative per-channel "completed": businesses carrying an
    # attribution.channel that were created in range (visible only). Feeds the
    # acquisition section's completed step, mirroring the global funnel logic.
    created_by_channel: dict[str, int] = defaultdict(int)
    for bid, b in all_businesses:
        if not _visible(bid):
            continue
        attr = b.get("attribution")
        ch = attr.get("channel") if isinstance(attr, dict) else None
        if ch and _in_range(_parse_dt(profiles[bid]["createdAt"]), start, end):
            created_by_channel[ch] += 1
    acquisition = {"byChannel": _build_acquisition(channel_records, created_by_channel)}

    # Ensure standard funnel monotonicity (Started >= Details Collected >=
    # Onboarded >= WhatsApp Paired) to prevent funnel inversion when
    # completed sessions were deleted.
    stage_counts["completed"] = max(stage_counts["completed"], stage_counts["whatsapp_paired"])
    stage_counts["details_collected"] = max(stage_counts["details_collected"], stage_counts["completed"])
    stage_counts["started"] = max(stage_counts["started"], stage_counts["details_collected"])

    # Per-stage completion (% of everyone who started) and drop-off (how many
    # left between this stage and the previous one) — the "who is leaving and
    # where" view. Drops are clamped at 0: "completed" can legitimately exceed
    # a session-derived stage when finished sessions were already deleted.
    started_total = stage_counts["started"]
    labels = {
        "started": "Started chatting",
        "details_collected": "Business details collected",
        "completed": "Onboarded (business created)",
        "whatsapp_paired": "WhatsApp paired",
    }
    # Drill-down rows for the reconstructed completions counted above — built
    # straight from the business record since no onboarding_sessions doc
    # survives for these owners. Appended to the deepest stage the business
    # doc proves (paired when it holds a waSessionId, otherwise Onboarded);
    # the cumulative merge below cascades each row into every earlier
    # stage's list too.
    biz_raw_by_id = dict(all_businesses)
    for p in reconstructed_profiles:
        raw_attribution = biz_raw_by_id.get(p["id"], {}).get("attribution")
        attribution = raw_attribution if isinstance(raw_attribution, dict) else {}
        _recon_stage = "whatsapp_paired" if p.get("whatsappPaired") else "completed"
        stage_sessions[_recon_stage].append({
            "phone": p["ownerPhone"],
            "name": p.get("ownerName"),
            "businessName": p.get("name"),
            "currentStep": None,
            "startedAt": p.get("createdAt"),
            "businessId": p.get("id"),
            "onboardingDeviceId": p.get("onboardingDeviceId"),
            "onboardingNumber": p.get("onboardingNumber"),
            "channel": attribution.get("channel"),
            "adId": attribution.get("adId"),
            "campaign": attribution.get("campaign"),
            # No live onboarding_sessions doc backs this row — rebuilt from
            # the business record. Surfaced to the UI so it can be labeled
            # instead of looking like an ordinary session.
            "reconstructed": True,
        })

    # Make stage_sessions cumulative to match stage_counts.
    # A session in the "completed" bucket must also appear when the user clicks
    # "WhatsApp paired", "Business details collected", or "Started chatting"
    # because stage_counts is cumulative too. Build a new dict so we don't
    # mutate the deepest-bucket data while iterating.
    cumulative_stage_sessions: dict[str, list[dict]] = {}
    for i, stage_key in enumerate(FUNNEL_STAGES):
        merged: list[dict] = []
        for s in FUNNEL_STAGES[i:]:      # this stage + every deeper stage
            merged.extend(stage_sessions[s])
        cumulative_stage_sessions[stage_key] = sorted(
            merged, key=lambda x: x["startedAt"] or "", reverse=True
        )
    stage_sessions = cumulative_stage_sessions

    funnel = []
    prev_count: int | None = None
    for stage_key in FUNNEL_STAGES:
        count = stage_counts[stage_key]
        dropped = max(prev_count - count, 0) if prev_count is not None else 0
        funnel.append({
            "stage": stage_key,
            "label": labels[stage_key],
            "count": count,
            "pctOfStarted": round(count / started_total, 4) if started_total else None,
            "dropped": dropped,
            "dropPct": (
                round(dropped / prev_count, 4)
                if prev_count not in (None, 0) else None
            ),
            # Per-user details for the dashboard drill-down modal.
            # Already sorted most-recent first by the cumulative merge above.
            "sessions": stage_sessions.get(stage_key, []),
        })
        prev_count = count

    # Growth: new businesses per day across the range
    day_counts: dict[str, int] = defaultdict(int)
    for p in businesses_created:
        day_counts[p["createdAt"][:10]] += 1
    growth = _daily_series(start, end, {"newBusinesses": day_counts})

    # Collection-group scans (prefetched above), bucketed per business
    bookings = loaded["bookings"]
    conversations = loaded["conversations"]
    complaints = loaded["complaints"]
    kb_entries = loaded["kb_entries"]
    live_convos = loaded["live_convos"]
    customers = loaded["customers"]

    per_biz: dict[str, dict] = defaultdict(lambda: {
        "bookings": 0, "conversations": 0, "openComplaints": 0,
        "pendingKnowledgeGaps": 0, "lastCustomerActivityAt": None,
    })

    def _bump_activity(biz_id: str, dt: datetime | None) -> None:
        if dt is None:
            return
        cur = per_biz[biz_id]["lastCustomerActivityAt"]
        if cur is None or dt.isoformat() > cur:
            per_biz[biz_id]["lastCustomerActivityAt"] = dt.isoformat()

    conv_by_day: dict[str, int] = defaultdict(int)
    book_by_day: dict[str, int] = defaultdict(int)

    total_bookings = 0
    bookings_by_status: dict[str, int] = defaultdict(int)
    # (biz_id, normalized customer phone) pairs that booked in range — lets us
    # credit a WhatsApp conversation as "converted" when its customer booked.
    booked_keys_in_range: set[tuple[str, str]] = set()
    for biz_id, _, b in bookings:
        if biz_id not in profiles or not _visible(biz_id):
            continue
        created = _parse_dt(b.get("createdAt")) or _booking_dt(b)
        _bump_activity(biz_id, created)
        if _in_range(created, start, end):
            per_biz[biz_id]["bookings"] += 1
            total_bookings += 1
            bookings_by_status[b.get("status") or "unknown"] += 1
            book_by_day[created.date().isoformat()] += 1
            _bphone = b.get("customerPhone") or b.get("customer_phone")
            if _bphone:
                booked_keys_in_range.add((biz_id, fs._clean_phone(str(_bphone))))

    total_conversations = 0
    outcomes: dict[str, int] = defaultdict(int)
    for biz_id, _, c in conversations:
        if biz_id not in profiles or not _visible(biz_id):
            continue
        started = _parse_dt(c.get("startedAt") or c.get("createdAt"))
        _bump_activity(biz_id, started)
        if _in_range(started, start, end):
            per_biz[biz_id]["conversations"] += 1
            total_conversations += 1
            conv_by_day[started.date().isoformat()] += 1
            if c.get("outcome"):
                outcomes[c["outcome"]] += 1

    # WhatsApp conversations carry no `outcome` field (only the voice pipeline
    # sets one). So booking-conversion used to measure voice ONLY — and reads
    # empty whenever there are no recent voice calls. We now also treat each live
    # WhatsApp chat active in range as a "resolved" conversation, converted when
    # that customer booked in range (matched by phone). See the aggregate below.
    whatsapp_resolved = 0
    whatsapp_converted = 0
    for biz_id, doc_id, c in live_convos:
        if biz_id not in profiles or not _visible(biz_id):
            continue
        last = _parse_dt(c.get("lastMessageAt"))
        _bump_activity(biz_id, last)
        # Live WhatsApp chats have no closed conversation log yet — count the
        # ones active in range so WhatsApp activity isn't invisible.
        if _in_range(last, start, end):
            per_biz[biz_id]["conversations"] += 1
            total_conversations += 1
            conv_by_day[last.date().isoformat()] += 1
            whatsapp_resolved += 1
            # The customer_conversations doc id is the customer phone (see
            # normalize_live_conversation); fall back to the stored field.
            _wphone = c.get("customerPhone") or doc_id
            if _wphone and (biz_id, fs._clean_phone(str(_wphone))) in booked_keys_in_range:
                whatsapp_converted += 1

    open_complaints = 0
    for biz_id, _, comp in complaints:
        if biz_id not in profiles or not _visible(biz_id):
            continue
        if (comp.get("status") or "open") == "open":
            per_biz[biz_id]["openComplaints"] += 1
            open_complaints += 1

    pending_gaps = 0
    for biz_id, _, entry in kb_entries:
        if biz_id not in profiles or not _visible(biz_id):
            continue
        if entry.get("status") in ("pending_review", "awaiting_answer"):
            per_biz[biz_id]["pendingKnowledgeGaps"] += 1
            pending_gaps += 1

    # CSAT (verified empty in production today — stays honest when it fills up)
    csat_values = []
    for biz_id, doc_id, c in live_convos:
        if not _visible(biz_id):
            continue
        rated = _parse_dt(c.get("csatRatedAt"))
        if isinstance(c.get("csatRating"), (int, float)) and _in_range(rated, start, end):
            csat_values.append(float(c["csatRating"]))
    for biz_id, _, cust in customers:
        if not _visible(biz_id):
            continue
        _bump_activity(biz_id, _parse_dt(cust.get("lastSeen")))
        for h in cust.get("csatHistory") or []:
            if isinstance(h, dict) and isinstance(h.get("rating"), (int, float)):
                if _in_range(_parse_dt(h.get("at")), start, end):
                    csat_values.append(float(h["rating"]))

    outcome_total = sum(outcomes.values())
    booked = outcomes.get("booked", 0)

    # Unified booking conversion across BOTH channels:
    #   resolved  = voice conversations with an outcome + WhatsApp chats in range
    #   converted = voice 'booked' outcomes            + WhatsApp chats whose
    #               customer booked in range (matched by phone)
    resolved_conversations = outcome_total + whatsapp_resolved
    converted_conversations = booked + whatsapp_converted
    booking_conversion = (
        round(converted_conversations / resolved_conversations, 4)
        if resolved_conversations else None
    )

    # Owner-activity proxy: last message the owner sent on the platform
    # onboarding number (onboarding_sessions doc id == owner phone).
    owner_activity: dict[str, str] = {}
    for phone, sess in sessions:
        dt = _session_activity_dt(sess)
        if dt:
            owner_activity[phone] = dt.isoformat()

    accounts = []
    for bid, p in profiles.items():
        if not _visible(bid):
            continue
        counts = per_biz.get(bid) or {
            "bookings": 0, "conversations": 0, "openComplaints": 0,
            "pendingKnowledgeGaps": 0, "lastCustomerActivityAt": None,
        }
        accounts.append({
            **p,
            "bookingsInRange": counts["bookings"],
            "conversationsInRange": counts["conversations"],
            "openComplaints": counts["openComplaints"],
            "pendingKnowledgeGaps": counts["pendingKnowledgeGaps"],
            "lastCustomerActivityAt": counts["lastCustomerActivityAt"],
            "lastOwnerActivityAt": owner_activity.get(p["ownerPhone"] or ""),
        })
    accounts.sort(key=lambda a: a["createdAt"] or "", reverse=True)

    result = {
        "range": {"from": _iso(start), "to": _iso(end)},
        "includeTest": include_test,
        # Global-number picker: every configured number (+ any legacy bucket),
        # with unfiltered volumes, and which one is currently selected.
        "globalNumbers": global_number_options(
            dict(device_session_counts), dict(device_account_counts),
        ),
        "globalDevice": global_device,
        # The funnel has its own window (all time by default) — echo it so the
        # UI can label what the bars actually cover.
        "funnelRange": {
            "allTime": funnel_all_time,
            "from": _iso(funnel_start),
            "to": _iso(funnel_end),
        },
        "funnel": funnel,
        "acquisition": acquisition,
        "activeOnboardingSessions": active_sessions,
        "excludedDemoSessions": excluded_demo_sessions,
        "growth": growth,
        "activity": _daily_series(
            start, end,
            {"conversations": conv_by_day, "bookings": book_by_day},
        ),
        "aggregate": {
            "totalBusinesses": len(accounts),
            "newBusinessesInRange": len(businesses_created),
            "whatsappPaired": sum(1 for a in accounts if a["whatsappPaired"]),
            "totalConversations": total_conversations,
            "totalBookings": total_bookings,
            "bookingsByStatus": dict(bookings_by_status),
            "conversationOutcomes": dict(outcomes),
            # converted ÷ resolved conversations, across voice + WhatsApp
            # (see resolved_conversations / converted_conversations above)
            "bookingConversion": booking_conversion,
            "resolvedConversations": resolved_conversations,
            "convertedConversations": converted_conversations,
            "avgCsat": _avg(csat_values),
            "csatResponses": len(csat_values),
            "openComplaints": open_complaints,
            "pendingKnowledgeGaps": pending_gaps,
        },
        "accounts": accounts,
    }
    _cache_put(_overview_cache, cache_key, result)
    return result


# ── Business detail (Screen 2) ────────────────────────────────────────────────

def get_business_detail(
    business_id: str,
    start: datetime,
    end: datetime,
) -> dict | None:
    """Everything the drill-down screen needs for one business.

    Auth-agnostic and self-contained on purpose: a future SMB-owner-facing
    endpoint can call this under its own auth without changes here.
    Returns None when the business doc does not exist.
    """
    cache_key = (business_id, start.isoformat(), end.isoformat())
    cached = _cache_get(_detail_cache, cache_key)
    if cached is not None:
        return cached

    # The business doc and all subcollections are independent reads — fetch
    # them concurrently. Conversations use a TWO-TIER fetch: a projection
    # query (no transcripts) over everything for counts/outcomes/daily
    # activity, and full docs for only the newest ~300 — all the table can
    # ever show — so transcripts for a 1,000-conversation tenant no longer
    # ride along just to be counted.
    fetch_start = time.monotonic()
    loaded = _parallel({
        "biz": lambda: fs.get_business_by_id(business_id),
        "bookings_raw": lambda: _stream_subcollection(business_id, "bookings", _DETAIL_FETCH_LIMIT),
        "convos_meta": lambda: _stream_subcollection(
            business_id, "conversations", _DETAIL_FETCH_LIMIT,
            fields=["startedAt", "createdAt", "outcome"],
        ),
        "convos_recent": lambda: _stream_subcollection(
            business_id, "conversations", _DETAIL_CONVERSATIONS_LIMIT + 100,
            order_desc_by="startedAt",
        ),
        "live_raw": lambda: _stream_subcollection(business_id, "customer_conversations", 200),
        "customers_raw": lambda: _stream_subcollection(business_id, "customers", 500),
        "complaints_raw": lambda: _stream_subcollection(business_id, "complaints", _DETAIL_COMPLAINTS_LIMIT),
        "kb_raw": lambda: _stream_subcollection(business_id, "knowledge", _DETAIL_KB_LIMIT),
    })
    logger.info(
        "[ANALYTICS] detail parallel fetch (%s) took %.2fs",
        business_id, time.monotonic() - fetch_start,
    )
    biz = loaded["biz"]
    if biz is None:
        return None
    now = datetime.now(timezone.utc)
    profile = business_profile(business_id, biz, now)

    # Owner-activity proxy (see overview) + onboarding session state if present
    session = fs.get_onboarding_session(profile["ownerPhone"]) if profile["ownerPhone"] else None
    if session:
        profile["lastOwnerActivityAt"] = _iso(_session_activity_dt(session))
        profile["onboardingStep"] = session.get("currentStep") or None
    else:
        profile["lastOwnerActivityAt"] = None
        profile["onboardingStep"] = None
    # Which global onboarding number this owner talks to us on. Priority matches
    # the overview: the business doc's own stamp first (survives session
    # deletion), then the IMMUTABLE number captured on the session
    # (session_number). NEVER a live device→number lookup — otherwise this
    # screen would show a different number than the overview for the same
    # business once a device is re-pointed.
    if not profile["onboardingDeviceId"]:
        _detail_dev = session_device(session) if session else UNATTRIBUTED_DEVICE
        profile["onboardingDeviceId"] = None if _detail_dev == UNATTRIBUTED_DEVICE else _detail_dev
    if not profile["onboardingNumber"]:
        _detail_num = session_number(session) if session else UNATTRIBUTED_DEVICE
        profile["onboardingNumber"] = None if _detail_num == UNATTRIBUTED_DEVICE else _detail_num

    # ── The owner's own chat with Sofia on the global number ─────────────────
    # This is the conversation that ONBOARDED them (or stalled part-way). It
    # lives on the onboarding session, not in the business's conversations
    # subcollection, so it was previously invisible in the dashboard even though
    # it is the most useful record for a business that never finished pairing.
    onboarding_chat = _build_onboarding_chat(session, profile["ownerPhone"])

    # Extra header facts (verified fields only)
    profile["address"] = biz.get("address") or None
    profile["timezone"] = biz.get("timezone") or None
    profile["services"] = [
        {
            "name": s.get("name"),
            "price": s.get("price"),
            "duration": s.get("duration") or s.get("durationMinutes"),
        }
        for s in (biz.get("services") or [])
        if isinstance(s, dict)
    ]
    # hours: hoursRaw is free text, hoursText (seen on Guimas) is a list of
    # per-day strings — normalize both to list[str] for one frontend shape.
    hours_raw = biz.get("hoursRaw") or biz.get("hoursText") or None
    if isinstance(hours_raw, str):
        profile["hours"] = [hours_raw]
    elif isinstance(hours_raw, list):
        profile["hours"] = [str(h) for h in hours_raw if h]
    else:
        profile["hours"] = None
    profile["openingDays"] = biz.get("openingDays") or None
    profile["automations"] = biz.get("automations") or None

    # Bookings (range-scoped by booking datetime, newest first)
    bookings_raw = loaded["bookings_raw"]
    bookings = []
    status_counts: dict[str, int] = defaultdict(int)
    # Phones that booked in range — used to credit WhatsApp conversations as
    # converted (see the unified booking-conversion computation below).
    booked_phones_in_range: set[str] = set()
    for doc_id, b in bookings_raw:
        norm = normalize_booking(b, doc_id, business_id)
        ref_dt = _parse_dt(norm["datetime"]) or _parse_dt(norm["createdAt"])
        if not _in_range(ref_dt, start, end):
            continue
        bookings.append(norm)
        status_counts[norm["status"]] += 1
        if norm.get("customerPhone"):
            booked_phones_in_range.add(fs._clean_phone(str(norm["customerPhone"])))
    bookings.sort(key=lambda b: b["datetime"] or b["createdAt"] or "", reverse=True)
    bookings_in_range = len(bookings)
    bookings_truncated = (
        bookings_in_range > _DETAIL_BOOKINGS_LIMIT
        or len(bookings_raw) >= _DETAIL_FETCH_LIMIT
    )
    # Daily series over everything in range (before the table cap)
    book_by_day: dict[str, int] = defaultdict(int)
    for b in bookings:
        ref = b["datetime"] or b["createdAt"]
        if ref:
            book_by_day[ref[:10]] += 1
    bookings = bookings[:_DETAIL_BOOKINGS_LIMIT]

    # Conversations — counts/outcomes/daily activity from the projection scan
    # (covers EVERYTHING in range), table rows + transcripts from the full
    # fetch of the newest ~300. Legacy docs with no startedAt (a few dozen)
    # are still counted via the meta scan but can't appear in the table
    # (order_by drops docs missing the field) — an accepted trade-off.
    convos_meta = loaded["convos_meta"]
    live_raw = loaded["live_raw"]

    conversations_in_range = 0
    outcome_counts: dict[str, int] = defaultdict(int)
    conv_by_day: dict[str, int] = defaultdict(int)
    for _, c in convos_meta:
        started = _parse_dt(c.get("startedAt") or c.get("createdAt"))
        if not _in_range(started, start, end):
            continue
        conversations_in_range += 1
        conv_by_day[started.date().isoformat()] += 1
        if c.get("outcome"):
            outcome_counts[c["outcome"]] += 1

    conversations = []
    transcripts: dict[str, list[dict]] = {}
    for doc_id, c in loaded["convos_recent"]:
        norm = normalize_conversation(c, doc_id, business_id)
        if not _in_range(_parse_dt(norm["startedAt"]), start, end):
            continue
        conversations.append(norm)
        transcripts[norm["id"]] = _normalize_transcript(c)

    # WhatsApp chats carry no `outcome` — treat each active-in-range chat as a
    # resolved conversation, converted when that customer booked in range.
    whatsapp_resolved = 0
    whatsapp_converted = 0
    for doc_id, c in live_raw:
        norm = normalize_live_conversation(c, doc_id, business_id)
        last = _parse_dt(norm["startedAt"])
        if not _in_range(last, start, end):
            continue
        conversations.append(norm)
        transcripts[norm["id"]] = _normalize_transcript(c)
        # live chats have no closed log — count them alongside the meta scan
        conversations_in_range += 1
        conv_by_day[last.date().isoformat()] += 1
        whatsapp_resolved += 1
        _wphone = norm.get("customerPhone") or doc_id
        if _wphone and fs._clean_phone(str(_wphone)) in booked_phones_in_range:
            whatsapp_converted += 1

    # All-time context so the UI can say "data exists outside this range"
    # instead of showing a bare empty state (counts are bounded by the fetch
    # cap, which is far above current per-business volumes).
    _all_booking_dts = [
        d for _, b in bookings_raw
        if (d := (_parse_dt(b.get("createdAt")) or _booking_dt(b))) is not None
    ]
    _all_conv_dts = [
        d for _, c in convos_meta
        if (d := _parse_dt(c.get("startedAt") or c.get("createdAt"))) is not None
    ] + [
        d for _, c in live_raw
        if (d := _parse_dt(c.get("lastMessageAt"))) is not None
    ]
    all_time = {
        "bookings": len(bookings_raw),
        "conversations": len(convos_meta) + len(live_raw),
        "lastBookingAt": _iso(max(_all_booking_dts)) if _all_booking_dts else None,
        "lastConversationAt": _iso(max(_all_conv_dts)) if _all_conv_dts else None,
    }

    conversations.sort(key=lambda c: c["startedAt"] or "", reverse=True)
    conversations_truncated = (
        conversations_in_range > _DETAIL_CONVERSATIONS_LIMIT
        or len(convos_meta) >= _DETAIL_FETCH_LIMIT
    )
    outcome_total = sum(outcome_counts.values())
    # Unified conversion (voice outcomes + WhatsApp phone-matched), same as overview.
    detail_resolved = outcome_total + whatsapp_resolved
    detail_converted = outcome_counts.get("booked", 0) + whatsapp_converted
    detail_booking_conversion = (
        round(detail_converted / detail_resolved, 4) if detail_resolved else None
    )
    conversations = conversations[:_DETAIL_CONVERSATIONS_LIMIT]
    transcripts = {c["id"]: transcripts.get(c["id"], []) for c in conversations}

    # CSAT trend (both persistence spots)
    customers_raw = [c for _, c in loaded["customers_raw"]]
    live_dicts = [dict(c, id=doc_id) for doc_id, c in live_raw]
    csat_all = _csat_points(live_dicts, customers_raw)
    csat_in_range = [
        p for p in csat_all
        if _in_range(_parse_dt(p["at"]), start, end)
    ]

    # Complaints + knowledge gaps: point-in-time (not range-filtered)
    complaints = [
        normalize_complaint(c, doc_id, business_id)
        for doc_id, c in loaded["complaints_raw"]
    ]
    complaints.sort(key=lambda c: c["createdAt"] or "", reverse=True)

    kb = [
        normalize_kb_entry(e, doc_id, business_id)
        for doc_id, e in loaded["kb_raw"]
    ]
    kb.sort(key=lambda e: e["createdAt"] or "", reverse=True)
    kb_by_status: dict[str, int] = defaultdict(int)
    for e in kb:
        kb_by_status[e["status"]] += 1

    result = {
        "range": {"from": _iso(start), "to": _iso(end)},
        "profile": profile,
        "activity": _daily_series(
            start, end,
            {"conversations": conv_by_day, "bookings": book_by_day},
        ),
        "allTime": all_time,
        "summary": {
            "bookingsInRange": bookings_in_range,
            "bookingsTruncated": bookings_truncated,
            "bookingsByStatus": dict(status_counts),
            "conversationsInRange": conversations_in_range,
            "conversationsTruncated": conversations_truncated,
            "conversationOutcomes": dict(outcome_counts),
            "bookingConversion": detail_booking_conversion,
            "avgCsat": _avg([p["rating"] for p in csat_in_range]),
            "csatResponses": len(csat_in_range),
            "openComplaints": sum(1 for c in complaints if c["status"] == "open"),
            "pendingKnowledgeGaps": kb_by_status.get("pending_review", 0) + kb_by_status.get("awaiting_answer", 0),
            "totalCustomers": len(customers_raw),
        },
        "bookings": bookings,
        "conversations": conversations,
        "transcripts": transcripts,
        # The owner's own onboarding chat with Sofia on the global number
        # (null when there is no session or it has no messages).
        "onboardingChat": onboarding_chat,
        "csatTrend": csat_in_range,
        "complaints": complaints,
        "knowledgeGaps": kb,
        "knowledgeByStatus": dict(kb_by_status),
    }
    _cache_put(_detail_cache, cache_key, result)
    return result
