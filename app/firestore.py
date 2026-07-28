"""Firestore data layer

All collections match the existing smbaicallz Firestore structure:
  businesses, bookings, customers, conversations, complaints

Firebase Admin SDK must be initialised before calling any function here.
Call app.firebase.init_firebase() at startup (already done in main.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from firebase_admin import firestore as fb_firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import logging

logger = logging.getLogger(__name__)


# ── Client ────────────────────────────────────────────────────────────────────

def _db():
    """Return a synchronous Firestore client (thread-safe, reuse across requests)."""
    return fb_firestore.client()


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


# ── businesses ────────────────────────────────────────────────────────────────

def get_business_by_owner(uid: str) -> dict | None:
    docs = (
        _db().collection("businesses")
        .where(filter=FieldFilter("ownerUid", "==", uid))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return data
    return None


def get_business_by_vapi_number_id(phone_number_id: str) -> dict | None:
    docs = (
        _db().collection("businesses")
        .where(filter=FieldFilter("vapiPhoneNumberId", "==", phone_number_id))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return data
    return None


def get_business_by_id(business_id: str) -> dict | None:
    doc = _db().collection("businesses").document(business_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data


def list_active_businesses(limit: int = 100) -> list[dict]:
    """Return all active businesses — used by the automation scheduler."""
    result: list[dict] = []
    try:
        docs = (
            _db().collection("businesses")
            .where(filter=FieldFilter("status", "==", "active"))
            .limit(limit)
            .stream()
        )
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            result.append(data)
    except Exception as exc:
        logger.warning(
            "list_active_businesses: partial results (%d / %d) — %s: %s",
            len(result), limit, type(exc).__name__, exc,
        )
    return result


def list_bookings_by_status(business_id: str, status: str, limit: int = 200) -> list[dict]:
    """Query bookings filtered by status for a given business."""
    docs = (
        _db().collection("businesses").document(business_id).collection("bookings")
        .where(filter=FieldFilter("status", "==", status))
        .limit(limit)
        .stream()
    )
    result = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        result.append(data)
    return result
def set_business(business_id: str, data: dict) -> dict:
    """Create or update a business document"""
    data["id"] = business_id
    _db().collection("businesses").document(business_id).set(data, merge=True)
    return data


# ── bookings ──────────────────────────────────────────────────────────────────
# Stored as subcollection: businesses/{businessId}/bookings/{bookingId}

def list_bookings(business_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    query = (
        _db().collection("businesses").document(business_id).collection("bookings")
        .order_by("datetime", direction=fb_firestore.Query.DESCENDING)
        .limit(limit)
        .offset(offset)
    )
    result = []
    for doc in query.stream():
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        result.append(data)
    return result


def create_booking(data: dict) -> dict:
    business_id = data.get("businessId") or data.get("business_id", "")
    doc_id = data.get("id") or new_id()
    data["id"] = doc_id
    data.setdefault("createdAt", _now_iso())
    _db().collection("businesses").document(business_id).collection("bookings").document(doc_id).set(data)
    print(f"[FIRESTORE] Booking {doc_id} created for business {business_id}")
    return data


class SlotFullError(Exception):
    """Raised by try_create_booking_with_capacity_check when no capacity is left."""
    def __init__(self, requested_slot: str, next_available: str | None = None):
        self.requested_slot = requested_slot
        self.next_available = next_available
        super().__init__(f"Slot {requested_slot} is full. Next available: {next_available}")


def try_create_booking_with_capacity_check(data: dict, slots_per_hour: int) -> dict:
    """Atomically create a booking only if the slot(s) have capacity.

    For parties larger than slotsPerHour the booking automatically spans
    ceil(partySize / slotsPerHour) consecutive hours and each of those hours
    is checked inside the same transaction.

    Example: partySize=6, slotsPerHour=5 → 2-hour booking; hour-1 contributes
    5 people against capacity, hour-2 contributes 1.

    Args:
        data: Full booking dict (must include businessId, id, datetime).
        slots_per_hour: Maximum concurrent people allowed per calendar hour.

    Returns:
        The created booking dict (duration auto-extended when needed).

    Raises:
        SlotFullError: When the requested time window is at capacity.
    """
    import math
    from datetime import datetime, timedelta

    business_id = data.get("businessId") or data.get("business_id", "")
    doc_id = data.get("id") or new_id()
    data["id"] = doc_id
    data.setdefault("createdAt", _now_iso())

    raw_dt = data.get("datetime", "")
    try:
        slot_dt = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
    except ValueError:
        # Cannot parse → fall back to plain create (no capacity check)
        _db().collection("businesses").document(business_id).collection("bookings").document(doc_id).set(data)
        return data

    new_party = int(data.get("partySize") or 1)
    # Number of consecutive hours needed to accommodate the full party.
    # e.g. partySize=6, slotsPerHour=5 → 2 hours.
    num_hours = math.ceil(new_party / slots_per_hour) if slots_per_hour > 0 else 1

    # Auto-extend duration so the booking occupies all needed hours.
    min_duration = num_hours * 60
    current_duration = int(data.get("serviceDuration") or data.get("durationMinutes") or 60)
    if current_duration < min_duration:
        data["serviceDuration"] = min_duration
        data["durationMinutes"] = min_duration

    slot_start = slot_dt.replace(minute=0, second=0, microsecond=0)

    db_client = _db()
    bookings_col = (
        db_client.collection("businesses")
        .document(business_id)
        .collection("bookings")
    )
    new_doc_ref = bookings_col.document(doc_id)

    # Build per-hour capacity windows the booking spans.
    # contribution_i = how many of the new_party "use up" hour i.
    hour_windows: list[tuple] = []
    for i in range(num_hours):
        h_start = slot_start + timedelta(hours=i)
        h_end = h_start + timedelta(hours=1)
        contribution = min(slots_per_hour, new_party - i * slots_per_hour)
        hour_windows.append((h_start, h_end, contribution))

    @fb_firestore.transactional
    def _run(transaction):
        for h_start, h_end, contribution in hour_windows:
            existing_docs = list(
                bookings_col
                .where(filter=FieldFilter("datetime", ">=", h_start.isoformat()))
                .where(filter=FieldFilter("datetime", "<", h_end.isoformat()))
                .where(filter=FieldFilter("status", "in", ["confirmed", "pending"]))
                .stream()
            )
            current_total = sum(
                int((b.to_dict() or {}).get("partySize") or 1)
                for b in existing_docs
            )
            if current_total + contribution > slots_per_hour:
                return h_start  # signal which hour is full
        transaction.set(new_doc_ref, data)
        return None  # signal success

    transaction = db_client.transaction()
    result = _run(transaction)
    if result is not None:
        # Find the next window where ALL num_hours consecutive hours have capacity.
        next_available: str | None = None
        for offset_hours in range(1, 49):
            candidate_start = slot_start + timedelta(hours=offset_hours)
            all_fit = True
            for i in range(num_hours):
                h_start = candidate_start + timedelta(hours=i)
                h_end = h_start + timedelta(hours=1)
                contribution = min(slots_per_hour, new_party - i * slots_per_hour)
                taken = list(
                    bookings_col
                    .where(filter=FieldFilter("datetime", ">=", h_start.isoformat()))
                    .where(filter=FieldFilter("datetime", "<", h_end.isoformat()))
                    .where(filter=FieldFilter("status", "in", ["confirmed", "pending"]))
                    .stream()
                )
                used = sum(int((b.to_dict() or {}).get("partySize") or 1) for b in taken)
                if used + contribution > slots_per_hour:
                    all_fit = False
                    break
            if all_fit:
                next_dt = candidate_start
                next_available = next_dt.strftime("%Y-%m-%d %H:%M")
                break
        raise SlotFullError(requested_slot=slot_dt.strftime("%Y-%m-%d %H:%M"), next_available=next_available)

    return data


def find_near_duplicate_booking(
    business_id: str,
    customer_phone: str,
    booking_dt,
    window_minutes: int = 10,
) -> dict | None:
    """Return an existing booking if one exists for the same customer within window_minutes of booking_dt.

    Uses a single-field equality query (customerPhone) which never requires a
    composite index, then filters by status and datetime window in Python.
    This avoids the Firestore FAILED_PRECONDITION error that occurs when the
    composite index (customerPhone, status, datetime) has not been created.

    Args:
        business_id: Firestore business document ID.
        customer_phone: Customer's phone number.
        booking_dt: datetime of the requested booking (naive or aware).
        window_minutes: How many minutes either side to consider a duplicate.

    Returns:
        The closest matching booking dict (within the window), or None.
    """
    from datetime import timedelta, timezone as _tz
    if isinstance(booking_dt, str):
        booking_dt = datetime.fromisoformat(booking_dt.replace("Z", "+00:00"))

    # Normalise incoming datetime to UTC-aware for safe comparisons.
    if booking_dt.tzinfo is None:
        booking_dt_utc = booking_dt.replace(tzinfo=_tz.utc)
    else:
        booking_dt_utc = booking_dt.astimezone(_tz.utc)

    window = timedelta(minutes=window_minutes)

    bookings_ref = (
        _db()
        .collection("businesses")
        .document(business_id)
        .collection("bookings")
    )

    # Single-field equality query — no composite index required.
    try:
        docs = list(
            bookings_ref
            .where(filter=FieldFilter("customerPhone", "==", customer_phone))
            .limit(50)
            .stream()
        )
    except Exception as exc:
        logger.warning(
            "find_near_duplicate_booking: query failed for business=%s: %s — skipping duplicate check",
            business_id, exc,
        )
        return None

    nearest: tuple[float, dict] | None = None
    for doc in docs:
        data = doc.to_dict() or {}
        status = str(data.get("status") or "").lower()
        if status not in {"confirmed", "pending"}:
            continue
        dt_raw = data.get("datetime")
        if not dt_raw:
            continue
        try:
            dt_val = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00"))
            # Normalise stored datetime to UTC-aware for comparison.
            if dt_val.tzinfo is None:
                dt_val_utc = dt_val.replace(tzinfo=_tz.utc)
            else:
                dt_val_utc = dt_val.astimezone(_tz.utc)
        except ValueError:
            continue
        if abs((dt_val_utc - booking_dt_utc).total_seconds()) > window_minutes * 60:
            continue
        delta = abs((dt_val_utc - booking_dt_utc).total_seconds())
        out = dict(data)
        out["id"] = doc.id
        if nearest is None or delta < nearest[0]:
            nearest = (delta, out)

    if nearest:
        return nearest[1]
    return None


def get_booking(booking_id: str, business_id: str) -> dict | None:
    """Fetch a single booking by its ID within a business subcollection."""
    doc = _db().collection("businesses").document(business_id).collection("bookings").document(booking_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    data["businessId"] = business_id
    return data


def get_booking_across_businesses(booking_id: str) -> dict | None:
    """Fetch a booking by its ID across all businesses using a collection group query."""
    try:
        docs = (
            _db().collection_group("bookings")
            .where(filter=FieldFilter("id", "==", booking_id))
            .limit(1)
            .stream()
        )
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            path_parts = doc.reference.path.split("/")
            if len(path_parts) >= 2:
                data["businessId"] = path_parts[1]
            return data
    except Exception as exc:
        logger.error("get_booking_across_businesses: failed for booking_id=%s: %s", booking_id, exc, exc_info=True)
    return None



def update_booking(booking_id: str, updates: dict, business_id: str = "") -> dict | None:
    if not business_id:
        # Try to find which business this booking belongs to
        updates_only = updates
        # fallback: update by searching all businesses (slow, avoid)
        return None
    ref = _db().collection("businesses").document(business_id).collection("bookings").document(booking_id)
    logger.info("[FIRESTORE] Updating booking %s for business %s with %s", booking_id, business_id, updates)
    ref.update(updates)
    doc = ref.get()
    if not doc.exists:
        logger.warning("[FIRESTORE] Booking %s not found after update for business %s", booking_id, business_id)
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    data["businessId"] = business_id
    logger.info("[FIRESTORE] Booking %s updated for business %s", booking_id, business_id)
    return data



# ── customers ─────────────────────────────────────────────────────────────────
# Stored as subcollection: businesses/{businessId}/customers/{phone}

def list_customers(business_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    query = (
        _db().collection("businesses").document(business_id).collection("customers")
        .limit(limit)
        .offset(offset)
    )
    result = []
    for doc in query.stream():
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        result.append(data)
    return result


def get_customer_by_phone(business_id: str, phone: str) -> dict | None:
    # Customer doc ID is the phone number (cleaned)
    doc = _db().collection("businesses").document(business_id).collection("customers").document(phone).get()
    if doc.exists:
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        return data
    # Fallback: query by phone field
    docs = (
        _db().collection("businesses").document(business_id).collection("customers")
        .where(filter=FieldFilter("phone", "==", phone))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        return data
    return None


def upsert_customer(business_id: str, phone: str, updates: dict) -> tuple[dict, bool]:
    """Create or update a customer. Returns (customer_dict, is_new)."""
    existing = get_customer_by_phone(business_id, phone)
    if existing:
        patch = {k: v for k, v in updates.items() if v is not None}
        if patch:
            _db().collection("businesses").document(business_id).collection("customers").document(existing["id"]).update(patch)
            existing.update(patch)
        return existing, False

    doc_id = phone  # Use phone as doc ID (matches boomreception convention)
    data: dict[str, Any] = {
        "id": doc_id,
        "businessId": business_id,
        "phone": phone,
        "totalVisits": 0,
        "isNewCustomer": True,
        "flags": [],
        "createdAt": _now_iso(),
        **updates,
    }
    _db().collection("businesses").document(business_id).collection("customers").document(doc_id).set(data)
    return data, True


def get_customer_by_referral_code(business_id: str, referral_code: str) -> dict | None:
    """Find a customer within a business by their unique referral code."""
    docs = (
        _db().collection("businesses").document(business_id).collection("customers")
        .where(filter=FieldFilter("referralCode", "==", referral_code))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        return data
    return None


def list_customers_with_expired_discounts(business_id: str) -> list[dict]:
    """Return customers that have a pendingDiscount whose expiry date is in the past.

    Fetches up to 500 customers and filters in Python to avoid requiring a
    composite Firestore index on pendingDiscountExpiresAt.
    """
    now_iso = _now_iso()
    customers = list_customers(business_id, limit=500)
    result = []
    for c in customers:
        if c.get("pendingDiscount") is None:
            continue
        if c.get("pendingDiscountConsumedAt"):
            continue  # already consumed — not an expired one
        exp_raw = c.get("pendingDiscountExpiresAt")
        if exp_raw and str(exp_raw) <= now_iso:
            result.append(c)
    return result


# ── referrals ─────────────────────────────────────────────────────────────────
# Stored as subcollection: businesses/{businessId}/referrals/{referralId}

def create_referral_doc(business_id: str, data: dict) -> dict:
    """Create a new referral document and return it with its generated id."""
    ref = _db().collection("businesses").document(business_id).collection("referrals").document()
    data["id"] = ref.id
    data.setdefault("createdAt", _now_iso())
    ref.set(data)
    return data


def update_referral_doc(business_id: str, referral_id: str, updates: dict) -> None:
    """Patch a referral document."""
    _db().collection("businesses").document(business_id).collection("referrals").document(referral_id).update(updates)


def get_referral_by_referee(business_id: str, referee_phone: str) -> dict | None:
    """Return the most recent active referral where *referee_phone* is the referee.

    Queries by refereePhone only (single-field, no composite index required)
    and filters for active statuses in Python.
    """
    phone_clean = _clean_phone(referee_phone)
    docs = (
        _db().collection("businesses").document(business_id).collection("referrals")
        .where(filter=FieldFilter("refereePhone", "==", phone_clean))
        .limit(5)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        if data.get("status") in ("pending", "refereeVisited"):
            return data
    return None


def get_referral_by_referrer(business_id: str, referrer_phone: str) -> dict | None:
    """Return the most recent referral in 'refereeVisited' state for this referrer.

    Called when a referrer completes a visit to close the referral loop.
    """
    phone_clean = _clean_phone(referrer_phone)
    docs = (
        _db().collection("businesses").document(business_id).collection("referrals")
        .where(filter=FieldFilter("referrerPhone", "==", phone_clean))
        .limit(10)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        if data.get("status") == "refereeVisited":
            return data
    return None


# ── referral owner confirmations ─────────────────────────────────────────────
# Stored as subcollection: businesses/{businessId}/referralOwnerConfirmations/{id}

def create_referral_owner_confirmation(business_id: str, data: dict) -> dict:
    """Create a new owner referral confirmation pending record and return it with its id."""
    ref = (
        _db().collection("businesses").document(business_id)
        .collection("referralOwnerConfirmations").document()
    )
    data["id"] = ref.id
    data.setdefault("createdAt", _now_iso())
    ref.set(data)
    return data


def get_referral_owner_confirmation_by_code(business_id: str, verify_code: str) -> dict | None:
    """Return the active pending confirmation matching a 4-digit verify code."""
    now_iso = _now_iso()
    docs = (
        _db().collection("businesses").document(business_id)
        .collection("referralOwnerConfirmations")
        .where(filter=FieldFilter("verifyCode", "==", verify_code))
        .limit(5)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        if data.get("status") != "pending":
            continue
        expires_at = data.get("expiresAt")
        if expires_at and str(expires_at) < now_iso:
            continue
        return data
    return None


def get_latest_pending_referral_owner_confirmation(business_id: str, owner_phone: str) -> dict | None:
    """Return the most recent non-expired pending confirmation for this owner (for NO replies).

    Filters by ownerPhone only (single-field, no composite index required) and
    checks status / expiry in Python.
    """
    phone_clean = _clean_phone(owner_phone)
    now_iso = _now_iso()
    docs = (
        _db().collection("businesses").document(business_id)
        .collection("referralOwnerConfirmations")
        .where(filter=FieldFilter("ownerPhone", "==", phone_clean))
        .limit(20)
        .stream()
    )
    candidates = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        if data.get("status") != "pending":
            continue
        expires_at = data.get("expiresAt")
        if expires_at and str(expires_at) < now_iso:
            continue
        candidates.append(data)
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.get("createdAt") or "")


def update_referral_owner_confirmation(business_id: str, confirmation_id: str, updates: dict) -> None:
    """Patch a referral owner confirmation document."""
    (
        _db().collection("businesses").document(business_id)
        .collection("referralOwnerConfirmations").document(confirmation_id)
        .update(updates)
    )


def get_pending_visit_confirmation_booking(business_id: str, customer_phone: str) -> dict | None:
    """Return the most recent booking awaiting visit confirmation from this customer.

    A booking is considered pending-confirmation when:
      - visitConfirmationSent == True
      - status is still 'confirmed' or 'pending' (not yet resolved)

    We fetch the last 10 bookings by customerPhone and filter in Python to
    avoid requiring a Firestore composite index.  Only called when the message
    body already looks like a yes/no reply, so the small list scan is acceptable.
    """
    phone_clean = _clean_phone(customer_phone)
    docs = (
        _db().collection("businesses").document(business_id).collection("bookings")
        .where(filter=FieldFilter("customerPhone", "==", phone_clean))
        .order_by("createdAt", direction=fb_firestore.Query.DESCENDING)
        .limit(10)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        if (
            data.get("visitConfirmationSent")
            and data.get("status") in ("confirmed", "pending")
        ):
            return data
    return None


# ── conversations ─────────────────────────────────────────────────────────────
# Stored as subcollection: businesses/{businessId}/conversations/{id}

def create_conversation(data: dict) -> dict:
    business_id = data.get("businessId", "")
    doc_id = data.get("id") or new_id()
    data["id"] = doc_id
    data.setdefault("createdAt", _now_iso())
    if business_id:
        _db().collection("businesses").document(business_id).collection("conversations").document(doc_id).set(data)
    else:
        _db().collection("conversations").document(doc_id).set(data)
    return data


def get_recent_booking_for_customer(business_id: str, customer_phone: str) -> dict | None:
    docs = (
        _db().collection("businesses").document(business_id).collection("bookings")
        .where(filter=FieldFilter("customerPhone", "==", customer_phone))
        .order_by("createdAt", direction=fb_firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        return data
    return None


# ── complaints ────────────────────────────────────────────────────────────────

def create_business_complaint(data: dict) -> dict:
    business_id = data.get("businessId") or data.get("business_id", "")
    if not business_id:
        raise ValueError("businessId is required")

    doc_id = data.get("id") or new_id()
    data["id"] = doc_id
    data["businessId"] = business_id
    data.setdefault("createdAt", _now_iso())
    _db().collection("businesses").document(business_id).collection("complaints").document(doc_id).set(data)
    return data

def create_complaint(data: dict) -> dict:
    doc_id = data.get("id") or new_id()
    data["id"] = doc_id
    data.setdefault("createdAt", _now_iso())
    _db().collection("complaints").document(doc_id).set(data)
    return data


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean_phone(phone: str) -> str:
    """Strip +, spaces, dashes, and @domain suffixes from a phone number. Returns digits only.

    Handles:
      - 917696794756@s.whatsapp.net  → 917696794756
      - 134544296509456@lid           → 134544296509456
      - +91-769-679-4756              → 91769679475
    """
    # Strip @domain suffix (@s.whatsapp.net, @lid, @g.us, etc.)
    phone = phone.split("@")[0] if "@" in phone else phone
    return phone.lstrip("+").replace(" ", "").replace("-", "")


def _normalize_lead_data(data: dict, collection: str) -> dict:
    """Normalize field names from any recepte-related collection to our standard format.

    Different Firestore collections (website_leads, recepte_leads, leads) may use
    slightly different key names.  This maps everything to the standard set the
    onboarding flow expects: businessName, name, type, city, url, phone, country.
    The original dict is NOT mutated; a copy is returned.
    """
    if not data:
        return data
    result = dict(data)

    # businessName ──────────────────────────────────────────────────────────
    if not result.get("businessName"):
        for alt in ("business_name", "BusinessName", "business", "companyName",
                    "company_name", "company"):
            if result.get(alt):
                result["businessName"] = result[alt]
                break

    # type (business category / vertical) ───────────────────────────────────
    if not result.get("type"):
        for alt in ("businessType", "business_type", "category", "industryType",
                    "industry", "vertical"):
            if result.get(alt):
                result["type"] = result[alt]
                break

    # city / location ────────────────────────────────────────────────────────
    if not result.get("city"):
        for alt in ("location", "cityName", "address_city", "region",
                    "district", "area"):
            if result.get(alt):
                result["city"] = result[alt]
                break

    # url / website ──────────────────────────────────────────────────────────
    if not result.get("url"):
        for alt in ("website", "websiteUrl", "siteUrl", "websiteURL",
                    "site", "web"):
            if result.get(alt):
                result["url"] = result[alt]
                break

    # name (owner / contact name) ────────────────────────────────────────────
    if not result.get("name"):
        for alt in ("ownerName", "owner_name", "fullName", "full_name",
                    "contactName", "contact_name", "userName"):
            if result.get(alt):
                result["name"] = result[alt]
                break

    result["_source_collection"] = collection
    return result


# ── onboarding sessions ──────────────────────────────────────────────────────
# Collection: onboarding_sessions  —  doc ID = phone (digits, no +)

def get_onboarding_session(phone: str) -> dict | None:
    phone_clean = _clean_phone(phone)
    doc = _db().collection("onboarding_sessions").document(phone_clean).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data


def _expand_dotted_keys(data: dict) -> dict:
    """Expand dot-notation keys into nested dicts for set().

    update() interprets ``"timestamps.lastActivityAt"`` as a nested path, but
    set() stores it as a LITERAL field name containing a dot — which produced
    corrupt session docs (seen in prod) whenever the first write for a phone
    carried a dotted key.
    """
    out: dict = {}
    for key, value in data.items():
        if "." not in key:
            out[key] = value
            continue
        node = out
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[parts[-1]] = value
    return out


def upsert_onboarding_session(phone: str, data: dict) -> dict:
    """Create or update an onboarding session.

    For existing docs, uses update() which supports Firestore dot-notation
    for nested fields (e.g. ``discovery.name``).  For new docs, uses set()
    with dotted keys expanded to real nested fields first.
    """
    phone_clean = _clean_phone(phone)
    ref = _db().collection("onboarding_sessions").document(phone_clean)
    doc = ref.get()

    if doc.exists:
        ref.update(data)
    else:
        ref.set(_expand_dotted_keys(data))

    result = ref.get().to_dict()
    result["id"] = phone_clean
    return result


def delete_onboarding_session(phone: str) -> None:
    phone_clean = _clean_phone(phone)
    _db().collection("onboarding_sessions").document(phone_clean).delete()


def claim_webhook_once(dedup_key: str, ttl_seconds: int = 300) -> bool:
    """Atomically claim a webhook for one-time processing (cross-instance safe).

    Returns True if this call is the FIRST to claim ``dedup_key`` (the caller
    should process the message), or False if it was already claimed (a duplicate
    delivery — the caller must skip it).

    Why this exists: the webhook handler's in-process ``_seen_message_ids`` cache
    only guards ONE backend instance. Both the bridge and the backend run several
    Cloud Run instances, so the same WhatsApp message can be delivered to
    different backend instances (multiple bridge instances, or a bridge retry) and
    slip past the in-memory cache — producing duplicate AI replies. Firestore's
    ``create()`` is atomic across all instances, so exactly one claimant wins.

    Fail-OPEN: on any Firestore error this returns True (process), so a Firestore
    hiccup never drops a real message — the in-memory cache still guards
    same-instance retries.

    ``expireAt`` is written as a Timestamp so an (optional) Firestore TTL policy
    on the ``webhook_dedup`` collection can auto-purge old rows and keep it
    bounded. Rows are tiny, so it is safe to run without a policy too.
    """
    import hashlib
    from google.api_core import exceptions as _gexc

    if not dedup_key:
        return True
    doc_id = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()
    now = datetime.utcnow()
    ref = _db().collection("webhook_dedup").document(doc_id)
    try:
        ref.create({
            "key": dedup_key,
            "createdAt": now.isoformat(),
            "expireAt": now + timedelta(seconds=ttl_seconds),
        })
        return True
    except _gexc.AlreadyExists:
        return False
    except Exception as exc:
        logger.warning("[DEDUP] claim_webhook_once failed for %r (fail-open): %s", dedup_key, exc)
        return True


def list_onboarding_sessions_active_between(
    start_iso: str, end_iso: str, limit: int = 1000
) -> list[dict]:
    """Return onboarding sessions whose last inbound owner message falls within
    ``[start_iso, end_iso]`` (ISO-8601 UTC strings).

    Used by the drop-off follow-up sweep to find owners who went quiet mid-flow
    without streaming the entire (ever-growing) collection. ``lastInboundAt`` is
    a top-level field stamped on every inbound owner message, so this bounded
    range scan only touches recently-active sessions. Sessions created before
    that field existed simply won't match — they get picked up on their next
    message. A range on a single field uses Firestore's automatic single-field
    index, so no composite index needs provisioning.
    """
    docs = (
        _db().collection("onboarding_sessions")
        .where(filter=FieldFilter("lastInboundAt", ">=", start_iso))
        .where(filter=FieldFilter("lastInboundAt", "<=", end_iso))
        .limit(limit)
        .stream()
    )
    out: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        out.append(data)
    return out


# Collection: demo_sessions  —  doc ID = phone (digits, no +)
# Isolated from onboarding_sessions so a Salão Bella demo on the dedicated demo
# number can never collide with a real onboarding session for the same phone.

def get_demo_session(phone: str) -> dict | None:
    phone_clean = _clean_phone(phone)
    doc = _db().collection("demo_sessions").document(phone_clean).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data


def upsert_demo_session(phone: str, data: dict) -> dict:
    phone_clean = _clean_phone(phone)
    ref = _db().collection("demo_sessions").document(phone_clean)
    doc = ref.get()
    if doc.exists:
        ref.update(data)
    else:
        ref.set(data)
    result = ref.get().to_dict()
    result["id"] = phone_clean
    return result


def delete_demo_session(phone: str) -> None:
    phone_clean = _clean_phone(phone)
    _db().collection("demo_sessions").document(phone_clean).delete()


# ── WhatsApp device send-state (outbound guard) ─────────────────────────────
# Collection: wa_device_state  —  doc ID = bridge device/session ID.
# Tracks the per-number proactive daily counter and the 463 circuit-breaker
# cooldown used by app/services/outbound_guard.py.

def get_wa_device_state(device_id: str) -> dict | None:
    doc = _db().collection("wa_device_state").document(device_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data


def upsert_wa_device_state(device_id: str, data: dict) -> None:
    _db().collection("wa_device_state").document(device_id).set(data, merge=True)


def get_wa_daily_count(device_id: str, day_key: str) -> int:
    """Proactive messages sent by this device on day_key (YYYYMMDD)."""
    doc = _db().collection("wa_device_state").document(f"{device_id}_{day_key}").get()
    if not doc.exists:
        return 0
    return int((doc.to_dict() or {}).get("proactiveCount") or 0)


def increment_wa_daily_count(device_id: str, day_key: str) -> None:
    """Atomically bump the per-device daily proactive counter.

    Uses a per-device-per-day doc + Firestore Increment: no read-modify-write
    race, no midnight-reset logic, safe across multiple backend instances.
    """
    from google.cloud.firestore_v1 import Increment

    _db().collection("wa_device_state").document(f"{device_id}_{day_key}").set(
        {"proactiveCount": Increment(1), "date": day_key}, merge=True
    )


# ── business lookup by owner phone ───────────────────────────────────────────

def get_business_by_owner_phone(phone: str) -> dict | None:
    phone_clean = _clean_phone(phone)
    docs = (
        _db().collection("businesses")
        .where(filter=FieldFilter("ownerPhone", "==", phone_clean))
        .where(filter=FieldFilter("status", "==", "active"))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return data
    return None


def get_business_by_phone_number(phone: str) -> dict | None:
    """Look up a business by its customer-facing phoneNumber field.

    Falls back to ownerPhone if not found by phoneNumber.
    """
    phone_clean = _clean_phone(phone)

    # Try phoneNumber field first
    docs = (
        _db().collection("businesses")
        .where(filter=FieldFilter("phoneNumber", "==", phone_clean))
        .where(filter=FieldFilter("status", "==", "active"))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return data

    # Fallback: ownerPhone
    return get_business_by_owner_phone(phone)


def create_business_doc(data: dict) -> str:
    """Create a new business document.  Returns the auto-generated doc ID."""
    ref = _db().collection("businesses").document()
    data["id"] = ref.id
    data.setdefault("createdAt", _now_iso())
    ref.set(data)
    return ref.id


def update_business_doc(business_id: str, updates: dict) -> None:
    _db().collection("businesses").document(business_id).update(updates)


def get_business_by_stripe_customer_id(stripe_customer_id: str) -> dict | None:
    """Find a business by its Stripe Customer ID.

    Used when routing Stripe webhook events back to the correct business.
    """
    docs = (
        _db().collection("businesses")
        .where(filter=FieldFilter("stripeCustomerId", "==", stripe_customer_id))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return data
    return None
def merge_business_doc(business_id: str, updates: dict) -> dict:
    """Merge business fields without overwriting the full document."""
    ref = _db().collection("businesses").document(business_id)
    payload = dict(updates or {})
    payload.setdefault("updatedAt", _now_iso())
    ref.set(payload, merge=True)
    doc = ref.get()
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return data


# ── call counters ─────────────────────────────────────────────────────────────

def _today_in_tz(tz: str | None):
    """Return today's date in the given IANA timezone, falling back to the
    server's local date if the timezone is missing or invalid."""
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:
            logger.warning("increment_call_counters: invalid timezone %r, using local date", tz)
    return datetime.now().date()


def increment_call_counters(business_id: str, tz: str | None = None) -> dict:
    """Atomically increment the per-business daily and weekly call counters.

    Maintains four fields on the business document:
      - daily_counter (int, default 0)
      - daily_counter_date (str, ISO date "YYYY-MM-DD")
      - weekly_counter (int, default 0)
      - weekly_counter_date_range (dict {"start": ISO date, "end": ISO date})

    Timezone:
      "Today" is resolved per business. Resolution order is:
        1. the explicit *tz* argument (if provided),
        2. the business document's `timezone` field (e.g. "Asia/Kolkata"),
        3. settings.BUSINESS_TIMEZONE (the env default).
      An invalid/unknown timezone falls back to the server's local date.

    Behaviour:
      Daily — if daily_counter_date is today, increment daily_counter; otherwise
      (different day or missing) reset daily_counter to 0 and set the date to today
      before incrementing (so it becomes 1).

      Weekly — if today falls within weekly_counter_date_range [start, end] inclusive,
      increment weekly_counter; otherwise (out of range or missing) start a fresh
      7-day range beginning today and reset weekly_counter to 0 before incrementing.

    Missing counter fields are created with their default/initial values. The whole
    read-modify-write runs inside a Firestore transaction so simultaneous calls for
    the same business don't lose counts.
    """
    from app.config import settings

    db = _db()
    ref = db.collection("businesses").document(business_id)
    transaction = db.transaction()

    @fb_firestore.transactional
    def _apply(txn) -> dict:
        snapshot = ref.get(transaction=txn)
        data = (snapshot.to_dict() or {}) if getattr(snapshot, "exists", False) else {}

        # Resolve the timezone for this business and compute "today" within it,
        # using the doc snapshot we already read (no extra read, no race).
        resolved_tz = tz or data.get("timezone") or settings.BUSINESS_TIMEZONE
        today = _today_in_tz(resolved_tz)
        today_iso = today.isoformat()
        week_end_iso = (today + timedelta(days=6)).isoformat()
        logger.info(
            "increment_call_counters: business=%s tz=%s today=%s",
            business_id, resolved_tz, today_iso,
        )

        # ── daily counter ──
        daily_counter = int(data.get("daily_counter", 0) or 0)
        daily_date = data.get("daily_counter_date") or ""
        if daily_date == today_iso:
            daily_counter += 1
        else:
            # New day or missing → reset to 0, then increment to 1.
            daily_counter = 1
            daily_date = today_iso

        # ── weekly counter ──
        weekly_counter = int(data.get("weekly_counter", 0) or 0)
        week_range = data.get("weekly_counter_date_range")
        start = week_range.get("start") if isinstance(week_range, dict) else None
        end = week_range.get("end") if isinstance(week_range, dict) else None
        in_range = bool(start and end and start <= today_iso <= end)
        if in_range:
            weekly_counter += 1
        else:
            # Today is outside the range or it is missing → start a fresh
            # 7-day window beginning today and reset the weekly counter.
            week_range = {"start": today_iso, "end": week_end_iso}
            weekly_counter = 1

        updates = {
            "daily_counter": daily_counter,
            "daily_counter_date": daily_date,
            "weekly_counter": weekly_counter,
            "weekly_counter_date_range": week_range,
            "updatedAt": _now_iso(),
        }
        txn.set(ref, updates, merge=True)
        return updates

    return _apply(transaction)


# ── owners ────────────────────────────────────────────────────────────────────

def create_owner_doc(phone: str, data: dict) -> None:
    phone_clean = _clean_phone(phone)
    data.setdefault("createdAt", _now_iso())
    _db().collection("owners").document(phone_clean).set(data, merge=True)


# ── business lookup by waSessionId ────────────────────────────────────────────

def list_businesses_with_wa_session() -> list[dict]:
    """Return every active business that has a WhatsApp bridge session attached.

    Used by background sweeps (CSAT prompts, reminders, daily summaries) that
    must iterate the whole tenant set. ``status==active`` is enforced so we
    don't message customers of dormant / churned tenants.
    """
    docs = (
        _db().collection("businesses")
        .where(filter=FieldFilter("status", "==", "active"))
        .stream()
    )
    out: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        if not data.get("waSessionId"):
            continue
        data["id"] = doc.id
        out.append(data)
    return out


def get_business_by_wa_session_id(session_id: str) -> dict | None:
    """Find a business by its WhatsApp bridge session ID (device_id)."""
    docs = (
        _db().collection("businesses")
        .where(filter=FieldFilter("waSessionId", "==", session_id))
        .where(filter=FieldFilter("status", "==", "active"))
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        return data
    return None


# ── customer conversations (WhatsApp AI) ──────────────────────────────────────

def list_paused_conversations(business_id: str) -> list[dict]:
    """Return all customer conversations where AI is currently paused, most-recent first."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    docs = (
        _db().collection("businesses")
        .document(business_id)
        .collection("customer_conversations")
        .where(filter=FieldFilter("aiPaused", "==", True))
        .stream()
    )
    result = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        result.append(data)
    result.sort(key=lambda d: d.get("aiPausedAt") or "", reverse=True)
    return result


def get_customer_conversation(business_id: str, customer_phone: str) -> dict | None:
    """Get conversation history for a customer chatting with a business."""
    phone_clean = _clean_phone(customer_phone)
    doc = (
        _db().collection("businesses")
        .document(business_id)
        .collection("customer_conversations")
        .document(phone_clean)
        .get()
    )
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data


def upsert_customer_conversation(business_id: str, customer_phone: str, data: dict) -> dict:
    """Create or update a customer conversation record."""
    phone_clean = _clean_phone(customer_phone)
    ref = (
        _db().collection("businesses")
        .document(business_id)
        .collection("customer_conversations")
        .document(phone_clean)
    )
    doc = ref.get()
    if doc.exists:
        ref.update(data)
    else:
        ref.set(data)
    result = ref.get().to_dict()
    result["id"] = phone_clean
    return result


# ── recepte.co leads ──────────────────────────────────────────────────────────
# Collection: recepte_leads — doc ID = cleaned phone (digits, no +)

def save_recepte_lead(lead: dict) -> dict:
    """Save/upsert a lead submitted from the recepte.co website.

    The lead must include at least a ``phone`` field.  Phone is cleaned to
    digits-only and used as the document ID.  Returns the saved lead dict.
    """
    phone = _clean_phone(lead.get("phone", ""))
    if not phone:
        raise ValueError("phone is required in lead data")
    lead_copy = dict(lead)
    lead_copy["phone"] = phone
    lead_copy.setdefault("createdAt", _now_iso())
    lead_copy["updatedAt"] = _now_iso()
    _db().collection("recepte_leads").document(phone).set(lead_copy, merge=True)
    logger.info(
        "[FIRESTORE] Saved recepte lead for phone %s (%s)",
        phone,
        lead_copy.get("businessName"),
    )
    return lead_copy


def get_recepte_lead_by_phone(phone: str) -> dict | None:
    """Fetch a recepte.co lead by phone number.

    Lookup order:
      1. ``recepte_leads`` collection (our own ingestion endpoint).
      2. ``website_leads`` collection (recepte.co production site stores leads here).
         Queries by phone with both ``+91XXXXXXXXXX`` and ``91XXXXXXXXXX`` formats
         and returns the most-recently-created record.
      3. ``leads`` collection written by the trackLead Cloud Function.

    Handles both plain phone numbers and WhatsApp JID format
    (e.g. ``918427791370@s.whatsapp.net``).
    """
    # Strip WhatsApp JID suffix if present (e.g. "918427791370@s.whatsapp.net")
    if "@" in phone:
        phone = phone.split("@")[0]
    phone_clean = _clean_phone(phone)  # digits only, e.g. "917015057282"
    phone_plus = f"+{phone_clean}"     # e.g. "+917015057282" — format used by recepte.co

    # 1. Primary: our own recepte_leads collection (doc ID = digits-only phone)
    doc = _db().collection("recepte_leads").document(phone_clean).get()
    if doc.exists:
        data = doc.to_dict()
        data["_collection"] = "recepte_leads"
        logger.info("[FIRESTORE] recepte lead found in recepte_leads for %s", phone_clean)
        return data

    # 2. Fallback: website_leads collection — recepte.co production site
    #    Phones stored as "+917015057282"; try both formats to be safe.
    #    Fetch all matches and sort by createdAt desc in Python to avoid index requirement.
    for phone_fmt in (phone_plus, phone_clean):
        docs = (
            _db()
            .collection("website_leads")
            .where(filter=FieldFilter("phone", "==", phone_fmt))
            .stream()
        )
        matches = []
        for wl_doc in docs:
            data = wl_doc.to_dict()
            matches.append(data)
        
        if matches:
            # Sort by createdAt descending (latest first) and return the first record
            matches.sort(
                key=lambda x: x.get("createdAt", ""),
                reverse=True
            )
            raw = matches[0]
            raw["_collection"] = "website_leads"
            result = _normalize_lead_data(raw, "website_leads")
            logger.info(
                "[FIRESTORE] recepte lead found in website_leads for %s (fmt=%s)",
                phone_clean,
                phone_fmt,
            )
            logger.info("[RECEPTE] Lead found in website_leads for %s (stored as %r)", phone_clean, phone_fmt)
            logger.debug(
                "[RECEPTE] Normalized fields: businessName=%r type=%r city=%r url=%r",
                result.get('businessName'),
                result.get('type'),
                result.get('city'),
                result.get('url'),
            )
            return result

    # 3. Last resort: `leads` collection written by the trackLead Cloud Function
    for phone_fmt in (phone_plus, phone_clean):
        docs = (
            _db().collection("leads")
            .where(filter=FieldFilter("phone", "==", phone_fmt))
            .limit(1)
            .stream()
        )
        for doc in docs:
            raw = doc.to_dict()
            raw["_collection"] = "leads"
            return _normalize_lead_data(raw, "leads")

    return None


def delete_recepte_lead(phone: str) -> None:
    """Remove a recepte.co lead after successful onboarding (housekeeping)."""
    phone_clean = _clean_phone(phone)
    _db().collection("recepte_leads").document(phone_clean).delete()
    logger.info("[FIRESTORE] Deleted recepte lead for phone %s", phone_clean)


def get_website_lead_by_phone(phone: str) -> dict | None:
    """Fetch a lead for a brand-new user, checking website_leads first then recepte_leads.

    Priority order (per product requirements):
      1. ``website_leads`` — populated by the recepte.co production website form.
         Tries both ``+91XXXXXXXXXX`` and ``91XXXXXXXXXX`` phone formats.
      2. ``recepte_leads`` — populated by our own ``/api/v1/recepte/lead`` endpoint.

    Used by the cold-start onboarding path (any user who messages WhatsApp for
    the first time) to detect pre-existing registration data automatically,
    without requiring the "I want to activate recepte for X" activation message.
    """
    if "@" in phone:
        phone = phone.split("@")[0]
    phone_clean = _clean_phone(phone)   # digits only, e.g. "917015057282"
    phone_plus  = f"+{phone_clean}"     # e.g. "+917015057282"

    print(f"[LEAD-LOOKUP] Checking website_leads for phone={phone_clean}")
    logger.info("[LEAD-LOOKUP] Checking website_leads for phone=%s", phone_clean)

    # ── 1. website_leads (recepte.co production site) ─────────────────────
    for phone_fmt in (phone_plus, phone_clean):
        docs = (
            _db()
            .collection("website_leads")
            .where(filter=FieldFilter("phone", "==", phone_fmt))
            .stream()
        )
        matches = []
        for wl_doc in docs:
            matches.append(wl_doc.to_dict())

        if matches:
            matches.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
            raw = matches[0]
            raw["_collection"] = "website_leads"
            result = _normalize_lead_data(raw, "website_leads")
            print(
                f"[LEAD-LOOKUP] Found in website_leads for {phone_clean} "
                f"(stored as {phone_fmt!r}): businessName={result.get('businessName')!r}"
            )
            logger.info(
                "[LEAD-LOOKUP] Found in website_leads for %s (fmt=%s): businessName=%r",
                phone_clean, phone_fmt, result.get("businessName"),
            )
            return result

    # ── 2. recepte_leads (our own ingestion endpoint) ─────────────────────
    print(f"[LEAD-LOOKUP] Not in website_leads, checking recepte_leads for phone={phone_clean}")
    logger.info("[LEAD-LOOKUP] Not in website_leads, checking recepte_leads for %s", phone_clean)

    doc = _db().collection("recepte_leads").document(phone_clean).get()
    if doc.exists:
        data = doc.to_dict()
        data["_collection"] = "recepte_leads"
        print(
            f"[LEAD-LOOKUP] Found in recepte_leads for {phone_clean}: "
            f"businessName={data.get('businessName')!r}"
        )
        logger.info(
            "[LEAD-LOOKUP] Found in recepte_leads for %s: businessName=%r",
            phone_clean, data.get("businessName"),
        )
        return data

    print(f"[LEAD-LOOKUP] No lead found for phone={phone_clean}")
    logger.info("[LEAD-LOOKUP] No lead found for %s", phone_clean)
    return None


# -- Global KB (system/recepte_kb) --------------------------------------------

def get_global_kb() -> dict | None:
    """Return the Global Recepte KB document from Firestore.

    Stored at `system/recepte_kb` as `{content: "...", updatedAt: "..."}`.
    Returns None when the document does not exist.
    """
    doc = _db().collection("system").document("recepte_kb").get()
    if not doc.exists:
        return None
    return doc.to_dict()


def set_global_kb(content: str) -> None:
    """Write or overwrite the Global KB document in Firestore.

    Args:
        content: Plain-text KB content to store.
    """
    _db().collection("system").document("recepte_kb").set({
        "content": content,
        "updatedAt": _now_iso(),
    })


# ── Website AI Assistant (Chatwoot) ──────────────────────────────────────────
# Pure CRUD layer for the website chat pipeline. All higher-level logic lives
# in app.web_assistant.*. Stored at:
#   web_conversations/{chatwoot_conversation_id}            — per-conversation state
#   system/web_assistant_kb/learnings/{auto_id}             — human-approved Q&A pairs

def _web_conversations_col():
    return _db().collection("web_conversations")


def get_web_conversation(conversation_id: str) -> dict | None:
    """Return a website-assistant conversation document.

    Args:
        conversation_id: Stringified Chatwoot conversation id.
    Returns:
        Document dict, or None if it does not exist.
    """
    doc = _web_conversations_col().document(str(conversation_id)).get()
    if not doc.exists:
        return None
    return doc.to_dict()


def save_web_conversation(conversation_id: str, data: dict) -> None:
    """Upsert a website-assistant conversation document (merge=True)."""
    payload = dict(data)
    payload["updatedAt"] = _now_iso()
    payload.setdefault("createdAt", _now_iso())
    _web_conversations_col().document(str(conversation_id)).set(payload, merge=True)


def _web_learnings_col():
    return (
        _db()
        .collection("system")
        .document("web_assistant_kb")
        .collection("learnings")
    )


def add_web_learning(data: dict) -> dict:
    """Persist a new human-approved Q&A learning for the website assistant.

    Args:
        data: At minimum {question, answer}. Caller adds metadata.
    Returns:
        The stored document including its generated `id`.
    """
    doc_id = data.get("id") or new_id()
    payload = dict(data)
    payload["id"] = doc_id
    payload.setdefault("createdAt", _now_iso())
    _web_learnings_col().document(doc_id).set(payload)
    return payload


def list_web_learnings(limit: int = 30) -> list[dict]:
    """Return the most recently created learnings, newest first."""
    try:
        docs = (
            _web_learnings_col()
            .order_by("createdAt", direction=fb_firestore.Query.DESCENDING)
            .limit(int(limit))
            .stream()
        )
        return [d.to_dict() for d in docs]
    except Exception as exc:
        logger.warning("[WEB-KB] list_web_learnings failed: %s", exc)
        return []


def find_web_learning_by_question_hash(question_hash: str) -> dict | None:
    """Lookup an existing learning by deterministic question hash (dedup)."""
    if not question_hash:
        return None
    try:
        docs = (
            _web_learnings_col()
            .where(filter=FieldFilter("questionHash", "==", question_hash))
            .limit(1)
            .stream()
        )
        for d in docs:
            return d.to_dict()
    except Exception as exc:
        logger.warning("[WEB-KB] find_web_learning_by_question_hash failed: %s", exc)
    return None


# ── per-business knowledge base ───────────────────────────────────────────────
# Subcollection: businesses/{businessId}/knowledge/{entryId}
#
# Each entry captures a question a customer asked that the AI could not answer,
# along with the owner's confirmed answer (when provided). High-level lifecycle
# logic — normalization, dedup, lifecycle transitions — lives in
# `app.services.knowledge_base_service`. This module is the pure CRUD layer.

def _kb_col(business_id: str):
    return (
        _db().collection("businesses").document(business_id).collection("knowledge")
    )


def create_business_kb_entry(business_id: str, data: dict) -> dict:
    """Create a new KB entry. The caller (knowledge_base_service) is
    responsible for setting status, intent guard, normalization, and short code.
    """
    doc_id = data.get("id") or new_id()
    data["id"] = doc_id
    data["businessId"] = business_id
    data.setdefault("createdAt", _now_iso())
    data.setdefault("updatedAt", _now_iso())
    _kb_col(business_id).document(doc_id).set(data)
    return data


def get_business_kb_entry(business_id: str, entry_id: str) -> dict | None:
    doc = _kb_col(business_id).document(entry_id).get()
    if not doc.exists:
        return None
    out = doc.to_dict()
    out["id"] = doc.id
    out["businessId"] = business_id
    return out


def get_business_kb_entry_by_code(business_id: str, short_code: str) -> dict | None:
    """Look up a pending entry by its human-facing short code (e.g. 'KB-A3F7').

    Used when an owner replies 'YES KB-A3F7 <answer>' / 'NO KB-A3F7' so we can
    match the reply to the exact pending entry.
    """
    docs = (
        _kb_col(business_id)
        .where(filter=FieldFilter("shortCode", "==", short_code))
        .limit(1)
        .stream()
    )
    for doc in docs:
        out = doc.to_dict()
        out["id"] = doc.id
        out["businessId"] = business_id
        return out
    return None


def find_business_kb_by_normalized(
    business_id: str,
    normalized_question: str,
    statuses: list[str] | None = None,
) -> dict | None:
    """Return the most recent entry matching the normalized question, optionally
    filtered by status. Used for dedup — so we don't ask the owner the same
    question twice or re-answer a question already in pending/confirmed/rejected.
    """
    query = _kb_col(business_id).where(
        filter=FieldFilter("questionNormalized", "==", normalized_question)
    )
    docs = list(query.stream())
    if statuses:
        docs = [d for d in docs if (d.to_dict() or {}).get("status") in statuses]
    if not docs:
        return None
    # Sort by createdAt desc — most recent first.
    docs.sort(key=lambda d: (d.to_dict() or {}).get("createdAt", ""), reverse=True)
    out = docs[0].to_dict()
    out["id"] = docs[0].id
    out["businessId"] = business_id
    return out


def list_business_kb_by_status(
    business_id: str,
    status: str,
    limit: int = 200,
) -> list[dict]:
    """List all entries with a given status. Used for retrieval ('confirmed'),
    sweepers ('pending_review' for expiry), and admin tooling.
    """
    docs = (
        _kb_col(business_id)
        .where(filter=FieldFilter("status", "==", status))
        .limit(limit)
        .stream()
    )
    out: list[dict] = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        data["businessId"] = business_id
        out.append(data)
    return out


def list_recent_pending_kb_for_owner(
    business_id: str,
    limit: int = 5,
) -> list[dict]:
    """Return the most-recent pending_review entries. Used as a fallback when
    the owner replies 'YES' with no code — we match the latest pending entry.
    """
    entries = list_business_kb_by_status(business_id, "pending_review", limit=limit * 4)
    entries.sort(key=lambda e: e.get("createdAt", ""), reverse=True)
    return entries[:limit]


def update_business_kb_entry(
    business_id: str,
    entry_id: str,
    updates: dict,
) -> dict | None:
    payload = dict(updates or {})
    payload.setdefault("updatedAt", _now_iso())
    ref = _kb_col(business_id).document(entry_id)
    ref.update(payload)
    doc = ref.get()
    if not doc.exists:
        return None
    out = doc.to_dict()
    out["id"] = doc.id
    out["businessId"] = business_id
    return out
