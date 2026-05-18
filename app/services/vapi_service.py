"""VAPI Service — Firestore backed

Handles all server-side logic triggered by VAPI webhook messages.

VAPI handles (we do NOT code):
  - Receiving the phone call
  - Speech-to-text & text-to-speech
  - Language detection
  - Running the AI conversation (LLM)
  - Calling our tools via HTTP webhook

We handle (this file):
  - Tool dispatch: createBooking | logComplaint | getAvailableSlots | flagSpam
  - End-of-call: save transcript, upsert customer, notify owner
  - Dynamic assistant config per business (assistant-request)
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytz

from app import firestore as fs
from app.config import settings
from app.integrations.google_calendar import google_calendar
from app.integrations.calendar_sync import sync_create as _cal_create, sync_update as _cal_update, sync_delete as _cal_delete
from app.services.notification_service import notifications
import logging

logger = logging.getLogger(__name__)


# ── Tool result helpers ──────────────────────────────────────────────────────

def _ok(msg: str) -> str:
    return msg


def _err(msg: str) -> str:
    return f"ERROR: {msg}"


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _phones_match(a: str, b: str) -> bool:
    a_norm = _normalize_phone(a)
    b_norm = _normalize_phone(b)
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    # Handle country code variations.
    return a_norm.endswith(b_norm[-10:]) or b_norm.endswith(a_norm[-10:])


def _extract_date_yyyy_mm_dd(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if len(value) >= 10:
        maybe = value[:10]
        try:
            return datetime.strptime(maybe, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return ""
    return ""


# NATO phonetic alphabet mapping (digits pass through as-is)
_NATO = {
    "A": "Alpha",   "B": "Bravo",   "C": "Charlie", "D": "Delta",
    "E": "Echo",    "F": "Foxtrot", "G": "Golf",    "H": "Hotel",
    "I": "India",   "J": "Juliet",  "K": "Kilo",    "L": "Lima",
    "M": "Mike",    "N": "November","O": "Oscar",   "P": "Papa",
    "Q": "Quebec",  "R": "Romeo",   "S": "Sierra",  "T": "Tango",
    "U": "Uniform", "V": "Victor",  "W": "Whiskey", "X": "X-ray",
    "Y": "Yankee",  "Z": "Zulu",
}


def _speak_booking_id(booking_id: str) -> str:
    """Return a spoken form of a booking ID using both variants.

    Variant A — character-by-character: "B K 7 A 9 1 F 2"
    Variant B — NATO phonetic:          "Bravo Kilo 7 Alpha 9 1 Foxtrot 2"
    """
    chars = list(booking_id.upper())
    variant_a = " ".join(chars)
    variant_b = " ".join(_NATO.get(ch, ch) for ch in chars)
    return f"{variant_a} — that is {variant_b}"


def _resolve_business(args: dict[str, Any], call_info: dict) -> dict | None:
    phone_number_id = call_info.get("phoneNumberId", "")
    business_id = args.get("businessId", "")

    business = fs.get_business_by_id(business_id) if business_id else None
    if not business and phone_number_id:
        business = get_business_by_vapi_number(phone_number_id)
    if not business and settings.VAPI_DEFAULT_BUSINESS_ID:
        business = fs.get_business_by_id(settings.VAPI_DEFAULT_BUSINESS_ID)
    return business


def _generate_short_booking_id(business_id: str) -> str:
    for _ in range(8):
        candidate = f"BK{uuid.uuid4().hex[:6].upper()}"
        if not fs.get_booking(candidate, business_id):
            return candidate
    return f"BK{uuid.uuid4().hex[:8].upper()}"


def _serialize_booking_for_voice(booking: dict) -> dict:
    result = dict(booking or {})
    result["id"] = booking.get("id")
    result["businessId"] = booking.get("businessId")
    return result


def _clean_none_values(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None and v != ""}


# ── Business lookup ──────────────────────────────────────────────────────────

def get_business_by_vapi_number(phone_number_id: str) -> dict | None:
    return fs.get_business_by_vapi_number_id(phone_number_id)


def get_or_create_customer(
    business_id: str,
    phone: str,
    name: str,
    language: str,
) -> tuple[dict, bool]:
    return fs.upsert_customer(business_id, phone, {"name": name, "language": language})


# Generic placeholder names that should never be treated as a real customer name.
_PLACEHOLDER_NAMES: frozenset[str] = frozenset({
    "customer", "customer name", "guest", "unknown", "caller",
    "name", "user", "client", "patient", "member",
})


def _is_real_name(name: str | None) -> bool:
    """Return True only when name is a non-empty, non-placeholder string."""
    if not name:
        return False
    return name.strip().lower() not in _PLACEHOLDER_NAMES



def tool_create_booking(args: dict[str, Any], call_info: dict) -> str:
    # callerPhone = the actual phone number the caller is calling FROM (from VAPI call metadata)
    # customerPhone = the number the booking is being made FOR (may differ if booking for someone else)
    caller_phone = str(
        call_info.get("customer", {}).get("number", "")
        or args.get("callerPhone", "")
    ).strip()
    customer_phone = str(
        args.get("customerPhone")
        or caller_phone
    ).strip()
    customer_name = str(args.get("customerName") or "").strip()
    service_name = args.get("serviceName", "Appointment")
    raw_dt = args.get("dateTime") or args.get("datetime", "")
    duration = _to_int(args.get("durationMinutes", 60), 60)
    special_requests = args.get("specialRequests") or args.get("specialRequest") or "NA"
    notes = args.get("notes") or special_requests
    source = args.get("source") or "vapi-voice"
    party_size_raw = args.get("partySize", 1)
    language = args.get("language", "en")

    try:
        party_size = max(int(party_size_raw), 1)
    except (TypeError, ValueError):
        party_size = 1

    if not customer_phone:
        return _err("customerPhone is required")
    if not raw_dt:
        return _err("dateTime is required")
    if not _is_real_name(customer_name):
        return _err(
            "customerName is required. Please ask the caller for their full name "
            "before proceeding to booking."
        )

    try:
        booking_dt = datetime.fromisoformat(raw_dt)
    except ValueError:
        return _err(f"Invalid dateTime format: {raw_dt}")

    business = _resolve_business(args, call_info)
    if not business:
        return _err("Business not found. Provide businessId or a valid VAPI phoneNumberId.")

    # ── Resolve business timezone and make booking_dt timezone-aware ──────────
    biz_tz_name = business.get("timezone") or "UTC"
    if biz_tz_name == "UTC":
        owner_phone = business.get("ownerPhone", "")
        if owner_phone:
            try:
                from app.services.onboarding_service import _infer_timezone_from_phone
                inferred = _infer_timezone_from_phone(owner_phone)
                if inferred and inferred != "UTC":
                    biz_tz_name = inferred
            except Exception:
                pass
    try:
        biz_tz = pytz.timezone(biz_tz_name)
    except Exception:
        biz_tz = pytz.UTC
        biz_tz_name = "UTC"

    if booking_dt.tzinfo is None:
        booking_dt = biz_tz.localize(booking_dt)
        logger.info("[TZ] booking naive → localized to %s: %s", biz_tz_name, booking_dt.isoformat())
    booking_dt_utc = booking_dt.astimezone(pytz.UTC)
    booking_dt_local = booking_dt.astimezone(biz_tz)
    # ────────────────────────────────────────────────────────────────────────

    # The AI sometimes passes the business's own VAPI phone as callerPhone.
    # Detect this and correct: callerPhone must be the person calling, not the business line.
    business_phone = business.get("phoneNumber") or business.get("ownerPhone") or ""
    if business_phone and _phones_match(caller_phone, business_phone):
        caller_phone = customer_phone
        print(f"[vapi_service] callerPhone was business phone — corrected to {caller_phone!r}")

    # Capacity check: reject if adding this party would exceed slotsPerHour headcount
    try:
        capacity_per_hour = max(1, int(business.get("slotsPerHour") or 1))
    except (TypeError, ValueError):
        capacity_per_hour = 1

    date_str = booking_dt.date().isoformat()
    day_bookings = _list_day_bookings(business["id"], date_str)
    booked_headcount = sum(
        _get_party_size(b)
        for b in day_bookings
        if _slot_overlaps_booking(booking_dt, duration, b)
    )
    if booked_headcount + party_size > capacity_per_hour:
        remaining = max(0, capacity_per_hour - booked_headcount)
        return _err(
            f"Sorry, that time slot only has capacity for {remaining} more "
            f"{'person' if remaining == 1 else 'people'}. "
            f"Please choose a different time."
        )

    # Only store a real name — never store generic placeholders in Firestore
    safe_name = customer_name if _is_real_name(customer_name) else ""
    customer, is_new = get_or_create_customer(
        business["id"], customer_phone, safe_name, language
    )

    booking_id = _generate_short_booking_id(business["id"])
    booking_data = {
        "id": booking_id,
        "businessId": business["id"],
        "customerPhone": customer_phone,
        "callerPhone": caller_phone or customer_phone,
        "customerName": safe_name or customer.get("name", ""),
        "serviceName": service_name,
        "serviceDuration": duration,
        "partySize": party_size,
        "datetime": booking_dt_utc.isoformat(),
        "source": source,
        "specialRequests": special_requests,
        "status": "confirmed",
        "notes": notes,
        "confirmedAt": datetime.utcnow().isoformat(),
    }

    # ── Near-duplicate check: prevent double bookings for same customer + slot ──
    # Guards against Claude emitting two create_booking calls in one turn even
    # after the in-process guard in _get_ai_response (belt-and-suspenders).
    existing_booking = fs.find_near_duplicate_booking(
        business_id=business["id"],
        customer_phone=customer_phone,
        booking_dt=booking_dt_utc,
        window_minutes=10,
    )
    if existing_booking:
        logger.warning(
            "[DUPLICATE-BOOKING-GUARD] Near-duplicate booking detected for "
            "customer=%s business=%s at %s — returning existing booking_id=%s",
            customer_phone, business["id"], booking_dt_utc.isoformat(), existing_booking.get("id"),
        )
        existing_id = existing_booking.get("id", booking_id)
        formatted_dt = booking_dt_local.strftime("%B %d, %Y at %I:%M %p")
        return (
            f"Booking confirmed! ID: {existing_id}\n"
            f"Service: {service_name}\n"
            f"Date/Time: {formatted_dt}\n"
            f"Name: {customer_name}"
        )

    # ── Atomic capacity check + create (DB FIRST — prevents orphan calendar events) ──
    slots_per_hour = int(business.get("slotsPerHour") or 0)
    try:
        if slots_per_hour > 0:
            fs.try_create_booking_with_capacity_check(booking_data, slots_per_hour)
        else:
            fs.create_booking(booking_data)
    except fs.SlotFullError as slot_err:
        next_hint = (
            f" The next available slot is around *{slot_err.next_available}*."
            if slot_err.next_available
            else " Please ask for an alternative time."
        )
        return _err(
            f"Sorry, the {slot_err.requested_slot} slot is fully booked "
            f"(maximum {slots_per_hour} booking(s) per hour).{next_hint}"
        )
    print(f"[BOOKING CREATED] {booking_id} for business={business['id']} customer={customer_phone} at {booking_dt_utc.isoformat()} (local {booking_dt_local.isoformat()})")
    # Sync to Google Calendar AFTER DB save — prevents orphan calendar events
    calendar_event_id = _cal_create(booking_data, business)

    # Fire-and-forget WhatsApp confirmation to customer.
    # Skip when source is "whatsapp" — the customer_ai_service already sends
    # Claude's natural-language reply as the confirmation, so firing this
    # automated template as well would cause the customer to receive two
    # confirmation messages for the same booking.
    if source != "whatsapp":
        try:
            from app.services.automation.booking_automation import send_booking_confirmation
            asyncio.get_event_loop().create_task(send_booking_confirmation(booking_data, business))
        except Exception as _auto_err:
            logger.warning("[Automation] booking confirmation skipped: %s", _auto_err)

    # Log booking confirmation
    logger.info(
        "[BOOKING] Added booking %s for business=%s customer=%s phone=%s datetime=%s source=%s",
        booking_id,
        business.get('name', business['id']),
        customer_name,
        customer_phone,
        booking_dt_utc.isoformat(),
        source,
    )

    formatted_dt = booking_dt_local.strftime("%B %d, %Y at %I:%M %p")

    # Owner WhatsApp notification — always sent regardless of source so the
    # owner knows when a customer books via voice (VAPI) or any other channel.
    try:
        from app.services.automation.whatsapp_notifier import send_to_owner
        new_tag = "🆕 New customer" if is_new else "🔄 Returning customer"
        owner_msg = (
            f"📅 *New booking!*\n"
            f"{new_tag}\n"
            f"Name: {customer_name}\n"
            f"Phone: {customer_phone}\n"
            f"Service: {service_name}\n"
            f"When: {formatted_dt}\n"
            f"Booking ID: {booking_id}"
        )
        asyncio.get_event_loop().create_task(send_to_owner(business, owner_msg))
    except Exception as _notify_err:
        logger.warning("[Booking] Owner notification skipped: %s", _notify_err)

    cal_note = " Calendar event created." if calendar_event_id else ""
    spoken_id = _speak_booking_id(booking_id)
    return _ok(
        f"Booking confirmed for {customer_name} on {formatted_dt} for {service_name}. "
        f"Your booking ID is {spoken_id}.{cal_note}"
    )


def check_booking_payload(args: dict[str, Any], call_info: dict) -> dict[str, Any]:
    business = _resolve_business(args, call_info)
    if not business:
        return {"error": "Business not found"}

    # callerPhone = who is actually calling (for ownership filtering)
    # customerPhone = whose bookings to look up (may be a different number)
    caller_phone = str(
        args.get("callerPhone")
        or args.get("phone")
        or call_info.get("customer", {}).get("number", "")
        or ""
    ).strip()
    customer_phone = str(
        args.get("customerPhone")
        or caller_phone
    ).strip()

    phone = customer_phone  # look up bookings under this number
    if not phone:
        return {"error": "phone number is required"}

    date_hint = args.get("date") or args.get("datetime") or args.get("dateTime") or ""
    target_date = _extract_date_yyyy_mm_dd(str(date_hint))

    bookings: list[dict] = []
    seen_ids: set[str] = set()

    def _add_doc(doc) -> None:
        if doc.id not in seen_ids:
            seen_ids.add(doc.id)
            item = doc.to_dict() or {}
            item["id"] = doc.id
            item["businessId"] = business["id"]
            bookings.append(item)

    # Query by callerPhone — ALL bookings this caller personally made
    # (regardless of which customerPhone they booked for)
    own_lookup = _phones_match(caller_phone, customer_phone)  # caller checking their own number
    try:
        for doc in (
            fs._db()
            .collection("businesses")
            .document(business["id"])
            .collection("bookings")
            .where(filter=fs.FieldFilter("callerPhone", "==", caller_phone))
            .order_by("datetime", direction=fs.fb_firestore.Query.DESCENDING)
            .limit(50)
            .stream()
        ):
            if doc.id not in seen_ids:
                item = doc.to_dict() or {}
                # When checking own number: include all. When checking a specific other number: filter.
                if own_lookup or _phones_match(str(item.get("customerPhone", "")), customer_phone):
                    seen_ids.add(doc.id)
                    item["id"] = doc.id
                    item["businessId"] = business["id"]
                    bookings.append(item)
    except Exception:
        pass

    # Query by customerPhone — include ONLY legacy bookings (no callerPhone stored)
    # and bookings where the caller IS the customer (callerPhone == customerPhone).
    # Exclude new bookings where callerPhone exists but belongs to someone else.
    try:
        for doc in (
            fs._db()
            .collection("businesses")
            .document(business["id"])
            .collection("bookings")
            .where(filter=fs.FieldFilter("customerPhone", "==", phone))
            .order_by("datetime", direction=fs.fb_firestore.Query.DESCENDING)
            .limit(50)
            .stream()
        ):
            if doc.id not in seen_ids:
                item = doc.to_dict() or {}
                stored_caller = str(item.get("callerPhone") or "").strip()
                # Include if: no callerPhone stored (legacy) OR callerPhone matches this caller
                if not stored_caller or _phones_match(stored_caller, caller_phone):
                    seen_ids.add(doc.id)
                    item["id"] = doc.id
                    item["businessId"] = business["id"]
                    bookings.append(item)
    except Exception:
        pass

    if not bookings:
        # Fallback: scan recent bookings for phone format differences
        all_recent = fs.list_bookings(business["id"], limit=300)
        for b in all_recent:
            b_id = b.get("id", "")
            if b_id not in seen_ids:
                stored_caller = str(b.get("callerPhone") or "").strip()
                stored_customer = str(b.get("customerPhone", ""))
                caller_match = _phones_match(stored_caller, caller_phone) if stored_caller else False
                customer_match = _phones_match(stored_customer, phone)
                legacy = customer_match and not stored_caller
                # Own lookup: all caller's bookings; specific lookup: must match customerPhone too
                if (caller_match and (own_lookup or customer_match)) or legacy:
                    seen_ids.add(b_id)
                    bookings.append(b)

    if target_date:
        bookings = [b for b in bookings if _extract_date_yyyy_mm_dd(str(b.get("datetime", ""))) == target_date]

    if not bookings:
        return {
            "businessId": business["id"],
            "phone": phone,
            "date": target_date or None,
            "totalBookings": 0,
            "booking": None,
            "bookings": [],
        }

    # Prefer latest active booking first.
    bookings.sort(
        key=lambda x: (
            str(x.get("status", "")).lower() == "cancelled",
            str(x.get("datetime", "")),
        ),
        reverse=True,
    )
    bookings.sort(key=lambda x: str(x.get("status", "")).lower() == "cancelled")
    booking = bookings[0]

    # Return all matched bookings for VAPI disambiguation.
    all_bookings = [_serialize_booking_for_voice(item) for item in bookings]
    return {
        "businessId": business["id"],
        "phone": phone,
        "date": target_date or _extract_date_yyyy_mm_dd(str(booking.get("datetime", ""))),
        "totalBookings": len(all_bookings),
        "booking": _serialize_booking_for_voice(booking),
        "bookings": all_bookings,
    }


def reschedule_booking_payload(args: dict[str, Any], call_info: dict) -> dict[str, Any]:
    business = _resolve_business(args, call_info)
    if not business:
        return {"error": "Business not found"}

    booking_id = str(args.get("bookingId") or args.get("id") or "").strip()
    if not booking_id:
        return {"error": "bookingId is required"}

    booking = fs.get_booking(booking_id, business["id"])
    if not booking:
        return {"error": f"Booking {booking_id} not found"}

    # callerPhone = who is actually calling (authorization check)
    # customerPhone = whose booking is being modified (may differ)
    caller_phone = str(
        args.get("callerPhone")
        or args.get("phone")
        or ""
    ).strip()
    # Verify the caller owns this booking (matches either stored callerPhone or customerPhone)
    if caller_phone and not (
        _phones_match(caller_phone, str(booking.get("callerPhone", "")))
        or _phones_match(caller_phone, str(booking.get("customerPhone", "")))
    ):
        return {"error": "You are not authorised to modify this booking"}

    name = str(args.get("customerName") or args.get("name") or "").strip()
    # Do NOT reject on name mismatch — phone is the identity check.
    # The caller may want to correct/update their name during rescheduling,
    # or the booking may have been saved with an empty name.

    raw_new_dt = (
        args.get("rescheduleDateTime")
        or args.get("rescheduleDate")
        or args.get("dateTime")
        or args.get("datetime")
        or ""
    )
    if not raw_new_dt:
        return {"error": "reschedule datetime is required"}

    raw_new_dt = str(raw_new_dt).strip()
    if len(raw_new_dt) == 10:
        # If only date was provided, preserve previous time when possible.
        existing_start = _parse_slot_datetime(booking.get("datetime"))
        if existing_start:
            raw_new_dt = f"{raw_new_dt}T{existing_start.strftime('%H:%M:%S')}"
        else:
            raw_new_dt = f"{raw_new_dt}T09:00:00"

    try:
        new_dt = datetime.fromisoformat(raw_new_dt.replace("Z", "+00:00"))
    except ValueError:
        return {"error": f"Invalid datetime format: {raw_new_dt}"}

    # Determine new party size (use updated value or fall back to existing booking)
    new_party_size = booking.get("partySize") or 1
    if args.get("partySize") is not None:
        new_party_size = max(_to_int(args.get("partySize"), 1), 1)

    # Capacity check for the target slot
    try:
        capacity_per_hour = max(1, int(business.get("slotsPerHour") or 1))
    except (TypeError, ValueError):
        capacity_per_hour = 1

    if capacity_per_hour > 1:
        duration = _to_int(booking.get("serviceDuration") or args.get("durationMinutes", 60), 60)
        date_str = new_dt.date().isoformat()
        day_bookings = _list_day_bookings(business["id"], date_str)
        # Count headcount at the target slot, EXCLUDING this booking (it's moving)
        other_headcount = sum(
            _get_party_size(b)
            for b in day_bookings
            if b.get("id") != booking_id and _slot_overlaps_booking(new_dt, duration, b)
        )
        if other_headcount + new_party_size > capacity_per_hour:
            remaining = max(0, capacity_per_hour - other_headcount)
            return {
                "error": (
                    f"Sorry, that time slot only has capacity for {remaining} more "
                    f"{'person' if remaining == 1 else 'people'}. "
                    f"Your party of {new_party_size} won't fit. "
                    f"Please choose a different time or reduce the party size."
                )
            }

    updates: dict[str, Any] = {
        "datetime": new_dt.isoformat(),
        "updatedAt": datetime.utcnow().isoformat(),
        "status": "confirmed",
    }

    if args.get("customerName"):
        updates["customerName"] = args.get("customerName")
    if args.get("customerPhone"):
        updates["customerPhone"] = args.get("customerPhone")
    if args.get("serviceName"):
        updates["serviceName"] = args.get("serviceName")
    if args.get("durationMinutes") is not None or args.get("duration") is not None:
        updates["serviceDuration"] = _to_int(args.get("durationMinutes", args.get("duration", 60)), 60)
    if args.get("partySize") is not None:
        updates["partySize"] = new_party_size
    if args.get("specialRequests") is not None:
        updates["specialRequests"] = args.get("specialRequests")
    if args.get("notes") is not None:
        updates["notes"] = args.get("notes")

    updated = fs.update_booking(booking_id, updates, business["id"])
    if not updated:
        return {"error": f"Failed to reschedule booking {booking_id}"}

    # Sync Google Calendar — update the existing event with the new time
    calendar_event_id = booking.get("calendarEventId")
    if calendar_event_id:
        duration = _to_int(
            updates.get("serviceDuration") or booking.get("serviceDuration") or 60, 60
        )
        _cal_update(
            booking_id=booking_id,
            event_id=calendar_event_id,
            business=business,
            start_dt=new_dt,
            duration_minutes=duration,
            customer_name=updates.get("customerName") or booking.get("customerName", ""),
            service_name=updates.get("serviceName") or booking.get("serviceName", ""),
            notes=updates.get("notes") or booking.get("notes", ""),
        )

    # Owner WhatsApp notification
    try:
        from app.services.automation.whatsapp_notifier import send_to_owner
        customer_name_str = updates.get("customerName") or booking.get("customerName", "Unknown")
        customer_phone_str = booking.get("customerPhone") or booking.get("callerPhone", "")
        service_name_str = updates.get("serviceName") or booking.get("serviceName", "Appointment")
        try:
            new_dt_fmt = new_dt.strftime("%B %d, %Y at %I:%M %p")
        except Exception:
            new_dt_fmt = str(new_dt)
        asyncio.get_event_loop().create_task(send_to_owner(
            business,
            f"🔄 *Booking rescheduled*\n"
            f"Customer: {customer_name_str}\n"
            f"Phone: {customer_phone_str}\n"
            f"Service: {service_name_str}\n"
            f"New time: {new_dt_fmt}\n"
            f"Booking ID: {booking_id}",
        ))
    except Exception as _notify_err:
        logger.warning("[Booking] Reschedule owner notification skipped: %s", _notify_err)

    return {
        "businessId": business["id"],
        "booking": _serialize_booking_for_voice(updated),
    }


def cancel_booking_payload(args: dict[str, Any], call_info: dict) -> dict[str, Any]:
    business = _resolve_business(args, call_info)
    if not business:
        return {"error": "Business not found"}

    booking_id = str(args.get("bookingId") or args.get("id") or "").strip()
    if not booking_id:
        return {"error": "bookingId is required"}

    booking = fs.get_booking(booking_id, business["id"])
    if not booking:
        return {"error": f"Booking {booking_id} not found"}

    # callerPhone = who is actually calling (authorization check)
    # customerPhone = whose booking is being cancelled (may differ)
    caller_phone = str(
        args.get("callerPhone")
        or args.get("phone")
        or ""
    ).strip()
    # Verify the caller owns this booking (matches either stored callerPhone or customerPhone)
    if caller_phone and not (
        _phones_match(caller_phone, str(booking.get("callerPhone", "")))
        or _phones_match(caller_phone, str(booking.get("customerPhone", "")))
    ):
        return {"error": "You are not authorised to cancel this booking"}

    updates = {
        "status": "cancelled",
        "cancelledAt": datetime.utcnow().isoformat(),
        "updatedAt": datetime.utcnow().isoformat(),
    }
    updated = fs.update_booking(booking_id, updates, business["id"])
    if not updated:
        return {"error": f"Failed to cancel booking {booking_id}"}

    # Delete the Google Calendar event if one exists (with retry + sync status)
    calendar_event_id = booking.get("calendarEventId")
    if calendar_event_id:
        _cal_delete(booking_id=booking_id, event_id=calendar_event_id, business=business)

    # Owner WhatsApp notification
    try:
        from app.services.automation.whatsapp_notifier import send_to_owner
        customer_name_str = booking.get("customerName", "Unknown")
        customer_phone_str = booking.get("customerPhone") or booking.get("callerPhone", "")
        service_name_str = booking.get("serviceName", "Appointment")
        original_dt = _parse_slot_datetime(booking.get("datetime"))
        formatted_cancel_dt = original_dt.strftime("%B %d, %Y at %I:%M %p") if original_dt else "scheduled time"
        asyncio.get_event_loop().create_task(send_to_owner(
            business,
            f"❌ *Booking cancelled*\n"
            f"Customer: {customer_name_str}\n"
            f"Phone: {customer_phone_str}\n"
            f"Service: {service_name_str}\n"
            f"Was: {formatted_cancel_dt}\n"
            f"Booking ID: {booking_id}",
        ))
    except Exception as _notify_err:
        logger.warning("[Booking] Cancel owner notification skipped: %s", _notify_err)

    return {
        "businessId": business["id"],
        "booking": _serialize_booking_for_voice(updated),
    }


# ── Tool: updateBooking ──────────────────────────────────────────────────────

def update_booking_payload(args: dict[str, Any], call_info: dict) -> dict[str, Any]:
    """Update mutable fields on an existing booking (partySize, specialRequests, notes).
    Does NOT change the date/time or re-check capacity — the customer already holds the slot.
    """
    business = _resolve_business(args, call_info)
    if not business:
        return {"error": "Business not found"}

    booking_id = str(args.get("bookingId") or args.get("id") or "").strip()
    if not booking_id:
        return {"error": "bookingId is required"}

    booking = fs.get_booking(booking_id, business["id"])
    if not booking:
        return {"error": f"Booking {booking_id} not found"}

    if booking.get("status") == "cancelled":
        return {"error": "Cannot update a cancelled booking"}

    phone = str(
        args.get("customerPhone")
        or args.get("callerPhone")
        or args.get("phone")
        or args.get("number")
        or ""
    )
    if phone and not _phones_match(phone, str(booking.get("customerPhone", ""))):
        return {"error": "phone number does not match booking"}

    updates: dict[str, Any] = {"updatedAt": datetime.utcnow().isoformat()}
    if args.get("partySize") is not None:
        updates["partySize"] = max(_to_int(args.get("partySize"), 1), 1)
    if args.get("specialRequests") is not None:
        updates["specialRequests"] = str(args["specialRequests"])
    if args.get("notes") is not None:
        updates["notes"] = str(args["notes"])
    if args.get("serviceName"):
        updates["serviceName"] = str(args["serviceName"])

    if len(updates) == 1:  # only updatedAt — nothing to change
        return {"error": "No updatable fields provided"}

    updated = fs.update_booking(booking_id, updates, business["id"])
    if not updated:
        return {"error": f"Failed to update booking {booking_id}"}

    # Sync Google Calendar when service name, notes, or other fields change
    calendar_event_id = booking.get("calendarEventId")
    if calendar_event_id:
        existing_dt_raw = booking.get("datetime", "")
        try:
            existing_dt = datetime.fromisoformat(
                str(existing_dt_raw).replace("Z", "+00:00")
            )
            _cal_update(
                booking_id=booking_id,
                event_id=calendar_event_id,
                business=business,
                start_dt=existing_dt,
                duration_minutes=_to_int(
                    updates.get("serviceDuration") or booking.get("serviceDuration") or 60, 60
                ),
                customer_name=updates.get("customerName") or booking.get("customerName", ""),
                service_name=updates.get("serviceName") or booking.get("serviceName", ""),
                notes=updates.get("notes") or booking.get("notes", ""),
            )
        except ValueError:
            print(f"[CalendarSync] Could not parse datetime for booking {booking_id}: {existing_dt_raw}")

    return {
        "businessId": business["id"],
        "booking": _serialize_booking_for_voice(updated),
    }


# ── Tool: logComplaint ───────────────────────────────────────────────────────

def create_complaint_payload(args: dict[str, Any], call_info: dict) -> dict[str, Any]:
    business = _resolve_business(args, call_info)
    if not business:
        return {"error": "Business not found"}
    business_id = business["id"]

    complaint_type_raw = str(
        args.get("complaintType")
        or args.get("type")
        or args.get("complaintBasis")
        or args.get("basis")
        or "general"
    ).strip().lower()
    complaint_type = "appointment" if complaint_type_raw in {"appointment", "appointment_basis", "appointment-based"} else "general"

    booking_id = str(args.get("bookingId") or args.get("booking_id") or "").strip()
    if complaint_type == "appointment" and not booking_id:
        return {"error": "bookingId is required for appointment complaints"}

    complaint_text = str(
        args.get("complaint")
        or args.get("complaintText")
        or args.get("text")
        or ""
    ).strip()
    if not complaint_text:
        return {"error": "complaint text is required"}

    customer_phone = str(
        args.get("customerPhone")
        or args.get("callerPhone")
        or args.get("phone")
        or call_info.get("customer", {}).get("number", "")
        or ""
    ).strip()
    customer_name = str(args.get("customerName") or args.get("name") or "Customer").strip() or "Customer"

    source = str(args.get("source") or "vapi").strip() or "vapi"
    status = str(args.get("status") or "open").strip() or "open"

    customer_details = _clean_none_values({
        "name": customer_name,
        "phone": customer_phone,
        "email": args.get("customerEmail"),
        "language": args.get("language"),
    })

    complaint_data: dict[str, Any] = {
        "businessId": business_id,
        "msgType": "comnplaint",
        "complaintType": complaint_type,
        "source": source,
        "complaint": complaint_text,
        "customer": customer_details,
        "status": status,
    }
    if booking_id:
        complaint_data["bookingId"] = booking_id

    created = fs.create_business_complaint(complaint_data)

    return {
        "complaintId": created.get("id"),
        "businessId": business_id,
        "complaintType": complaint_type,
        "bookingId": created.get("bookingId"),
        "source": source,
        "customer": created.get("customer", {}),
        "status": created.get("status"),
    }

def tool_log_complaint(args: dict[str, Any], call_info: dict) -> str:
    customer_phone = args.get("customerPhone") or call_info.get("customer", {}).get("number", "")
    customer_name = args.get("customerName", "Customer")
    complaint_text = args.get("complaintText", "")
    category = args.get("category", "other")
    language = args.get("language", "en")

    if not complaint_text:
        return _err("complaintText is required")

    business = _resolve_business(args, call_info)
    if not business:
        return _err("Business not found")

    if customer_phone:
        customer, _ = get_or_create_customer(
            business["id"], customer_phone, customer_name, language
        )
        flags: list = customer.get("flags") or []
        if "complaint" not in flags:
            flags.append("complaint")
            fs._db().collection("customers").document(customer["id"]).update({"flags": flags})

    fs.create_complaint({
        "businessId": business["id"],
        "customerPhone": customer_phone,
        "customerName": customer_name,
        "text": complaint_text,
        "category": category,
        "sentiment": "negative",
        "language": language,
        "status": "open",
    })

    owner_phone = business.get("ownerPhone") or (
        business.get("adminPhones", [None])[0] if business.get("adminPhones") else None
    )
    if owner_phone:
        notifications.notify_owner_complaint(
            owner_phone=owner_phone,
            customer_name=customer_name,
            customer_phone=customer_phone,
            complaint_text=complaint_text,
            category=category,
        )

    if customer_phone:
        notifications.acknowledge_complaint_to_customer(
            customer_phone=customer_phone,
            customer_name=customer_name,
            business_name=business.get("name", ""),
            language=language,
        )

    return _ok("Complaint recorded. The business owner has been notified and we will follow up.")


# ── Tool: getAvailableSlots ──────────────────────────────────────────────────

def _parse_slot_datetime(value: Any) -> datetime | None:
    """Parse a datetime value and return it as a naive local-time datetime
    in the configured BUSINESS_TIMEZONE (default Europe/Lisbon).
    Naive datetimes are assumed to already be in local time.
    """
    biz_tz = ZoneInfo(settings.BUSINESS_TIMEZONE)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(biz_tz).replace(tzinfo=None)
        return dt
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(biz_tz).replace(tzinfo=None)
            return dt
        except ValueError:
            return None
    return None


def _list_day_bookings(business_id: str, date: str) -> list[dict]:
    day_start = datetime.strptime(date, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    bookings: list[dict] = []

    try:
        docs = (
            fs._db().collection("businesses").document(business_id).collection("bookings")
            .where(filter=fs.FieldFilter("datetime", ">=", day_start.isoformat()))
            .where(filter=fs.FieldFilter("datetime", "<", day_end.isoformat()))
            .stream()
        )
        for doc in docs:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            bookings.append(data)
    except Exception:
        bookings = fs.list_bookings(business_id, limit=300)

    return bookings


def _get_party_size(booking: dict) -> int:
    """Return the partySize of a booking as an integer (minimum 1)."""
    try:
        return max(1, int(booking.get("partySize") or 1))
    except (TypeError, ValueError):
        return 1


def _slot_overlaps_booking(slot_start: datetime, duration_minutes: int, booking: dict) -> bool:
    if (booking.get("status") or "").lower() == "cancelled":
        return False

    # Normalize to naive local time so comparison with _parse_slot_datetime output is safe.
    # _parse_slot_datetime always returns naive datetimes in settings.BUSINESS_TIMEZONE.
    # Callers such as tool_create_booking pass timezone-aware datetimes (pytz-localized);
    # converting both sides to the same naive reference prevents the
    # "can't compare offset-naive and offset-aware datetimes" TypeError.
    if slot_start.tzinfo is not None:
        _tz = ZoneInfo(settings.BUSINESS_TIMEZONE)
        slot_start = slot_start.astimezone(_tz).replace(tzinfo=None)

    booking_start = _parse_slot_datetime(booking.get("datetime"))
    if not booking_start:
        return False

    booking_duration = booking.get("serviceDuration") or booking.get("durationMinutes") or 60
    try:
        booking_duration = int(booking_duration)
    except (TypeError, ValueError):
        booking_duration = 60

    slot_end = slot_start + timedelta(minutes=duration_minutes)
    booking_end = booking_start + timedelta(minutes=max(booking_duration, 1))
    return slot_start < booking_end and slot_end > booking_start


def _resolve_earliest_slot_datetime(args: dict[str, Any], target_date: str) -> datetime | None:
    raw_earliest = args.get("earliestDateTime") or args.get("datetime") or args.get("dateTime")
    earliest_dt = _parse_slot_datetime(raw_earliest)

    if earliest_dt and earliest_dt.date().isoformat() == target_date:
        return earliest_dt

    today = datetime.now().date().isoformat()
    if target_date == today:
        return datetime.now()

    return None


def get_available_slots_payload(args: dict[str, Any], call_info: dict) -> dict[str, Any]:
    import math
    date = args.get("date", "")

    try:
        duration = int(args.get("durationMinutes", 60))
    except (TypeError, ValueError):
        duration = 60

    # Accept partySize so we only show slots that can fit the whole group
    try:
        requested_party = max(1, int(args.get("partySize") or 1))
    except (TypeError, ValueError):
        requested_party = 1

    if not date:
        return {"error": "date is required (YYYY-MM-DD format)"}

    business = _resolve_business(args, call_info)
    if not business:
        return {"error": "Business not found"}


    # ── Step 1: Check opening day ────────────────────────────────────────────
    # openingDays is an array of full day names e.g. ["Monday", "Tuesday", ...]
    # Default: Mon-Fri when not configured.
    _DEFAULT_OPENING_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    raw_opening_days = business.get("openingDays")
    # Ensure it's a non-empty list of strings; fall back to default if not.
    if isinstance(raw_opening_days, list) and raw_opening_days:
        effective_opening_days = [str(d).strip() for d in raw_opening_days if d]
    else:
        effective_opening_days = _DEFAULT_OPENING_DAYS

    requested_weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
    is_open_day = requested_weekday in effective_opening_days

    print(
        f"[getAvailableSlots] date={date} weekday={requested_weekday} "
        f"openingDays(raw)={raw_opening_days} effective={effective_opening_days} "
        f"is_open_day={is_open_day}"
    )

    # ── Step 2: If closed day → return immediately, no slot generation ───────
    if not is_open_day:
        return {
            "businessId": business["id"],
            "date": date,
            "durationMinutes": duration,
            "partySize": requested_party,
            "capacity": 0,
            "slotRemaining": {},
            "earliestDateTime": None,
            "slotSource": "closed-day",
            "closedDay": True,
            "requestedWeekday": requested_weekday,
            "openingDays": effective_opening_days,
            "totalCandidates": 0,
            "slots": [],
            "totalAvailable": 0,
        }

    # ── Step 3: Generate candidate slots from business hours ─────────────────
    # We intentionally do NOT use Google Calendar free/busy here because all
    # businesses share a single calendar — its events would block slots for
    # every business.  Instead we check only THIS business's Firestore bookings
    # (done below in the conflict filter), which is the correct per-business check.
    candidate_slots = _default_slots(date, business.get("hours"))
    slot_source = "default-hours"

    # slotsPerHour = person capacity: total headcount allowed per hour slot.
    # e.g. slotsPerHour=40 means 40 people can be booked in the same 1-hour slot.
    # A party of 4 uses 4 of those 40 slots.
    try:
        capacity_per_hour = max(1, int(business.get("slotsPerHour") or 1))
    except (TypeError, ValueError):
        capacity_per_hour = 1

    day_bookings = _list_day_bookings(business["id"], date)
    earliest_slot_dt = _resolve_earliest_slot_datetime(args, date)
    filtered_slots: list[str] = []
    slot_remaining: dict[str, int] = {}  # remaining headcount per available slot

    for slot in candidate_slots:
        slot_start = _parse_slot_datetime(slot)
        if not slot_start:
            continue
        if earliest_slot_dt and slot_start < earliest_slot_dt:
            continue
        # Sum partySize of existing non-cancelled bookings that overlap this slot
        booked_headcount = sum(
            _get_party_size(booking)
            for booking in day_bookings
            if _slot_overlaps_booking(slot_start, duration, booking)
        )
        remaining = capacity_per_hour - booked_headcount
        # Slot is available only if the requested party can fit in remaining capacity
        if booked_headcount + requested_party <= capacity_per_hour:
            iso = slot_start.isoformat()
            filtered_slots.append(iso)
            slot_remaining[iso] = remaining

    return {
        "businessId": business["id"],
        "date": date,
        "durationMinutes": duration,
        "partySize": requested_party,
        "capacity": capacity_per_hour,
        "slotRemaining": slot_remaining,
        "earliestDateTime": earliest_slot_dt.isoformat() if earliest_slot_dt else None,
        "slotSource": slot_source,
        "closedDay": False,
        "requestedWeekday": requested_weekday,
        "openingDays": effective_opening_days,
        "totalCandidates": len(candidate_slots),
        "slots": filtered_slots,
        "totalAvailable": len(filtered_slots),
    }

def tool_get_available_slots(args: dict[str, Any], call_info: dict) -> str:
    payload = get_available_slots_payload(args, call_info)
    if payload.get("error"):
        return _err(payload["error"])

    slots = payload.get("slots", [])
    date = payload.get("date", "")
    capacity = payload.get("capacity", 1)
    slot_remaining = payload.get("slotRemaining", {})
    show_remaining = capacity > 1  # Only annotate when venue has multi-person capacity

    if payload.get("closedDay"):
        weekday = payload.get("requestedWeekday", date)
        open_days = payload.get("openingDays", [])
        open_days_str = ", ".join(open_days) if open_days else "their regular business days"
        return _ok(
            f"The business is closed on {weekday}s. "
            f"They are open on: {open_days_str}. "
            f"Please ask the customer to choose a different day."
        )

    if not slots:
        return _ok(f"No available slots on {date}. Please try another date.")

    readable = []
    for s in slots[:6]:
        try:
            time_str = datetime.fromisoformat(s).strftime("%I:%M %p").lstrip("0")
            if show_remaining and s in slot_remaining:
                rem = slot_remaining[s]
                time_str += f" ({rem} {'spot' if rem == 1 else 'spots'} remaining)"
            readable.append(time_str)
        except ValueError:
            readable.append(s)

    return _ok(f"Available times on {date}: {', '.join(readable)}.")

def _default_slots(date: str, hours: dict | str | None) -> list[str]:
    """Generate one candidate slot per hour within business operating hours.

    Accepts hours as:
      - str  e.g. "Mon-Thu 9:00-18:00" — the time range is parsed via regex
      - dict e.g. {"start": "09:00", "end": "18:00"} — legacy format
      - None — defaults to 09:00-18:00
    """
    _DEFAULT_START, _DEFAULT_END = 9, 18
    day = datetime.strptime(date, "%Y-%m-%d")

    if isinstance(hours, str) and hours:
        # Parse time range from strings like "Mon-Thu 9:00-18:00"
        m = re.search(r'(\d+):\d+\s*-\s*(\d+):\d+', hours)
        if m:
            start_h, end_h = int(m.group(1)), int(m.group(2))
        else:
            start_h, end_h = _DEFAULT_START, _DEFAULT_END
    elif isinstance(hours, dict):
        start_h = int((hours.get("start") or "09:00").split(":")[0])
        end_h   = int((hours.get("end")   or "18:00").split(":")[0])
    else:
        start_h, end_h = _DEFAULT_START, _DEFAULT_END

    return [day.replace(hour=h_, minute=0, second=0, microsecond=0).isoformat()
            for h_ in range(start_h, end_h)]


# ── Tool: checkPhone ─────────────────────────────────────────────────────────

def check_phone_payload(args: dict[str, Any], call_info: dict | None = None) -> dict[str, Any]:
    """Validate a phone number via Twilio Lookup v2 and enrich with customer data.

    Returns a dict with:
      - valid (bool)
      - phone (E.164 string, normalised)
      - national_format
      - country_code
      - calling_country_code
      - isReturningCaller (bool) — True if this phone has a record in the business's customer list
      - customerName (str | None) — existing name if returning caller
      - error (only if lookup failed / invalid)
    """
    # Prefer explicit 'phone' or 'callerPhone' parameters.
    # 'phoneNumber' is also accepted BUT only when it is a plain string — VAPI
    # injects its own 'phoneNumber' field as a dict (account phone object), so
    # we must discard it when it is a dict.
    _phone_number_field = args.get("phoneNumber")
    _phone_number_str = _phone_number_field if isinstance(_phone_number_field, str) else ""
    _raw = (
        args.get("phone")
        or args.get("callerPhone")
        or args.get("customerPhone")
        or _phone_number_str
        or ""
    )
    if isinstance(_raw, dict):
        _raw = ""  # reject VAPI's account phoneNumber object
    raw_phone = str(_raw).strip()

    if not raw_phone:
        return {
            "valid": False,
            "error": (
                "No phone number provided. "
                "Please pass the caller's phone number as the 'phone' parameter and retry."
            ),
        }

    if not raw_phone.startswith("+"):
        raw_phone = "+" + raw_phone

    # useCallingNumber=true → caller confirmed their calling number; skip Twilio,
    # just check Firestore to see if they are a returning customer.
    use_calling_number = args.get("useCallingNumber", False)
    if isinstance(use_calling_number, str):
        use_calling_number = use_calling_number.lower() in ("true", "yes", "1")

    if use_calling_number:
        print(f"[checkPhone] useCallingNumber=true — skipping Twilio, Firestore lookup only for {raw_phone}")
        result: dict[str, Any] = {
            "valid": True,
            "phone": raw_phone,
            "isReturningCaller": False,
            "customerName": None,
        }
        business = _resolve_business(args, call_info or {})
        if business:
            customer = fs.get_customer_by_phone(business["id"], raw_phone)
            if customer and _is_real_name(customer.get("name")):
                result["isReturningCaller"] = True
                result["customerName"] = customer["name"]
                print(f"[checkPhone] returning caller (no Twilio): {customer['name']}")
        return result

    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException
        from app.config import settings as _settings

        client = Client(_settings.TWILIO_ACCOUNT_SID, _settings.TWILIO_AUTH_TOKEN)
        lookup = client.lookups.v2.phone_numbers(raw_phone).fetch()

        print(f"[checkPhone] {raw_phone} → valid={lookup.valid} country={lookup.country_code}")

        result = {
            "valid": lookup.valid,
            "phone": lookup.phone_number,
            "national_format": lookup.national_format,
            "country_code": lookup.country_code,
            "calling_country_code": lookup.calling_country_code,
            "isReturningCaller": False,
            "customerName": None,
        }

        # Look up existing customer record for this business
        if lookup.valid:
            business = _resolve_business(args, call_info or {})
            if business:
                customer = fs.get_customer_by_phone(business["id"], lookup.phone_number)
                if customer and _is_real_name(customer.get("name")):
                    result["isReturningCaller"] = True
                    result["customerName"] = customer["name"]
                    print(f"[checkPhone] returning caller: {customer['name']}")

        return result

    except Exception as exc:
        err = str(exc)
        print(f"[checkPhone] lookup failed for {raw_phone}: {err}")
        # Twilio 20404 = number not found / invalid format
        if "20404" in err or "not found" in err.lower() or "Unable to fetch" in err:
            return {"valid": False, "phone": raw_phone, "error": "Phone number is invalid or unrecognised"}
        return {"valid": False, "phone": raw_phone, "error": f"Lookup error: {err}"}


# ── Tool: flagSpam ───────────────────────────────────────────────────────────

def tool_flag_spam(args: dict[str, Any], call_info: dict) -> str:
    caller_phone = call_info.get("customer", {}).get("number", "unknown")
    reason = args.get("reason", "")

    business = _resolve_business(args, call_info)
    if business:
        owner_phone = business.get("ownerPhone") or (
            business.get("adminPhones", [None])[0] if business.get("adminPhones") else None
        )
        if owner_phone:
            notifications.notify_owner_spam(
                owner_phone=owner_phone,
                caller_phone=caller_phone,
                reason=reason,
            )

    return _ok("Call identified as spam. Ending call.")


# ── End-of-call report ───────────────────────────────────────────────────────

def handle_end_of_call_report(payload: dict) -> None:
    call = payload.get("call", {})
    phone_number_id = call.get("phoneNumberId", "")
    customer_phone = call.get("customer", {}).get("number", "")
    call_id = call.get("id", str(uuid.uuid4()))

    transcript_raw = payload.get("transcript", "")
    summary = payload.get("summary", "")
    ended_reason = payload.get("endedReason", "")
    started_at_str = call.get("startedAt")
    ended_at_str = call.get("endedAt")

    business = get_business_by_vapi_number(phone_number_id)
    if not business and settings.VAPI_DEFAULT_BUSINESS_ID:
        business = fs.get_business_by_id(settings.VAPI_DEFAULT_BUSINESS_ID)
    if not business:
        return

    try:
        started_at = datetime.fromisoformat(started_at_str) if started_at_str else datetime.utcnow()
        ended_at = datetime.fromisoformat(ended_at_str) if ended_at_str else datetime.utcnow()
        duration = int((ended_at - started_at).total_seconds())
    except Exception:
        started_at = ended_at = datetime.utcnow()
        duration = 0

    transcript_list = _parse_transcript(transcript_raw)
    outcome = _infer_outcome(ended_reason, summary, transcript_list)

    recent_booking = fs.get_recent_booking_for_customer(business["id"], customer_phone)
    booking_id = recent_booking["id"] if recent_booking else None

    fs.create_conversation({
        "id": call_id,
        "businessId": business["id"],
        "customerPhone": customer_phone,
        "callSid": call_id,
        "channel": "voice",
        "status": "completed",
        "outcome": outcome,
        "startedAt": started_at.isoformat(),
        "endedAt": ended_at.isoformat(),
        "duration": duration,
        "transcript": transcript_list,
        "summary": summary,
        "bookingId": booking_id,
        "bookingConfirmed": booking_id is not None,
        "read": False,
    })

    if customer_phone:
        fs.upsert_customer(business["id"], customer_phone, {
            "lastSeen": ended_at.isoformat(),
        })
        existing = fs.get_customer_by_phone(business["id"], customer_phone)
        if existing:
            current_visits = existing.get("totalVisits") or 0
            fs._db().collection("customers").document(existing["id"]).update({
                "totalVisits": current_visits + 1,
            })


def _parse_transcript(raw: str) -> list[dict]:
    if not raw:
        return []
    lines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith(("AI:", "Assistant:")):
            role, text = "assistant", line.split(":", 1)[-1].strip()
        elif ":" in line:
            role, text = "user", line.split(":", 1)[-1].strip()
        else:
            role, text = "user", line
        lines.append({"role": role, "text": text})
    return lines


def _infer_outcome(ended_reason: str, summary: str, transcript: list[dict]) -> str:
    text = (summary + " " + ended_reason).lower()
    if any(w in text for w in ["book", "appointment", "scheduled", "confirmed"]):
        return "booked"
    if any(w in text for w in ["complaint", "issue", "problem", "unhappy"]):
        return "complaint"
    if any(w in text for w in ["spam", "robot", "scam"]):
        return "spam"
    if "missed" in text or "no-answer" in text:
        return "missed"
    return "completed"


# ── Dynamic assistant config ─────────────────────────────────────────────────

def build_assistant_config(call_info: dict) -> dict:
    phone_number_id = call_info.get("phoneNumberId", "")
    business = get_business_by_vapi_number(phone_number_id)

    if not business:
        return {"assistantId": settings.VAPI_DEFAULT_ASSISTANT_ID}

    if business.get("vapiAssistantId"):
        return {"assistantId": business["vapiAssistantId"]}

    services: list = business.get("services") or []
    services_text = ""
    if services:
        names = [s.get("name", "") for s in services if s.get("name")]
        if names:
            services_text = "Services offered: " + ", ".join(names) + "."

    lang = business.get("primaryLanguage", "en")

    # Opening days
    _DEFAULT_OPENING_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    opening_days: list[str] | None = business.get("openingDays")
    effective_opening_days = opening_days if opening_days else _DEFAULT_OPENING_DAYS
    opening_days_text = "Open on: " + ", ".join(effective_opening_days) + "."

    # Opening hours — string ("Mon-Thu 9:00-18:00") or absent → default
    hours_raw = business.get("hours")
    if isinstance(hours_raw, str) and hours_raw.strip():
        opening_hours_text = f"Opening hours: {hours_raw.strip()}."
    elif isinstance(hours_raw, dict):
        start = hours_raw.get("start") or "09:00"
        end   = hours_raw.get("end")   or "18:00"
        opening_hours_text = f"Opening hours: {start} - {end}."
    else:
        opening_hours_text = "Opening hours: 9:00 - 18:00 (default)."

    system_prompt = (
        f"You are the AI receptionist for {business.get('name', 'this business')}. "
        f"{services_text} "
        f"{opening_days_text} "
        f"{opening_hours_text} "
        f"Your primary language is {lang}. "
        "Detect the caller's language and respond in the same language. "
        "Your goals are: 1) Book appointments, 2) Handle complaints politely, "
        "3) Identify and hang up on spam/robocalls. "
        "Use the provided tools to create bookings, log complaints, check available slots, "
        "and flag spam. Always confirm the customer's name, preferred service, and desired date/time "
        "before creating a booking."
    )

    return {
        "assistant": {
            "name": f"{business.get('name', 'Business')} Receptionist",
            "model": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "systemPrompt": system_prompt,
                "temperature": 0.4,
            },
            "voice": {
                "provider": "11labs",
                "voiceId": "EXAVITQu4vr4xnSDxMaL",
            },
            "firstMessage": (
                f"Hello, thank you for calling {business.get('name', '')}. How can I help you today?"
                if lang == "en"
                else f"Olá, obrigado por ligar para {business.get('name', '')}. Em que posso ajudá-lo?"
            ),
            "endCallMessage": "Thank you for calling. Have a great day!",
        }
    }
    if business.vapi_assistant_id:
        return {"assistantId": business.vapi_assistant_id}

    # Build a dynamic assistant on-the-fly
    services_text = ""
    services: list = business.services or []
    if services:
        names = [s.get("name", "") for s in services if s.get("name")]
        services_text = "Services offered: " + ", ".join(names) + "."

    system_prompt = (
        f"You are the AI receptionist for {business.name}. "
        f"{services_text} "
        f"Your primary language is {business.primary_language}. "
        "Detect the caller's language and respond in the same language. "
        "Your goals are: 1) Book appointments, 2) Handle complaints politely, "
        "3) Identify and hang up on spam/robocalls. "
        "Use the provided tools to create bookings, log complaints, check available slots, "
        "and flag spam. Always confirm the customer's name, preferred service, and desired date/time "
        "before creating a booking."
    )

    return {
        "assistant": {
            "name": f"{business.name} Receptionist",
            "model": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "systemPrompt": system_prompt,
                "temperature": 0.4,
            },
            "voice": {
                "provider": "11labs",
                "voiceId": "EXAVITQu4vr4xnSDxMaL",   # Sarah – multilingual
            },
            "firstMessage": (
                f"Hello, thank you for calling {business.name}. How can I help you today?"
                if business.primary_language == "en"
                else f"Olá, obrigado por ligar para {business.name}. Em que posso ajudá-lo?"
            ),
            "endCallMessage": "Thank you for calling. Have a great day!",
            "serverUrl": "",   # filled by VAPI using your registered server URL
        }
    }
