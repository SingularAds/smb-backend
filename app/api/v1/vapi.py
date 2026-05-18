"""VAPI Webhook Router

Single endpoint that receives messages from VAPI:

  POST /vapi/webhook

VAPI message types handled:
  createBooking       → store booking data to database
  comnplaint          → store complaint data to database
  search_business     → generate a business-specific VAPI prompt

Security:
  VAPI signs each request with a secret token sent in the
  x-vapi-secret header. Set VAPI_SECRET in your .env.
"""

import hmac
import json
import logging
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.services import vapi_service
from app.services.prompt_service import build_default_prompt, _OUT_OF_SCOPE_RULE
from app import firestore as fs

logger = logging.getLogger(__name__)

router = APIRouter()

WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _to_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_date_from_hint(raw_datetime: str, raw_day: str) -> tuple[str, str] | tuple[None, str]:
    """Resolve date from datetime/day strings.

    Supports ISO datetime/date, today, tomorrow, weekday names, and 'next <weekday>'.
    Returns (YYYY-MM-DD, resolution_reason) or (None, reason).
    """
    dt_hint = (raw_datetime or "").strip().lower()
    day_hint = (raw_day or "").strip().lower()
    hint = dt_hint or day_hint

    if not hint:
        return None, "no date hint provided"

    if hint in {"today", "now"}:
        return date.today().isoformat(), "resolved from 'today'"

    if hint == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat(), "resolved from 'tomorrow'"

    # ISO datetime/date support (including trailing Z).
    try:
        normalized = hint.replace("z", "+00:00")
        parsed_dt = datetime.fromisoformat(normalized)
        return parsed_dt.date().isoformat(), "resolved from ISO datetime/date"
    except ValueError:
        pass

    # Plain date support like YYYY-MM-DD in mixed input.
    if len(hint) >= 10:
        maybe_date = hint[:10]
        try:
            parsed_date = datetime.strptime(maybe_date, "%Y-%m-%d").date()
            return parsed_date.isoformat(), "resolved from YYYY-MM-DD"
        except ValueError:
            pass

    # Weekday support: "monday" or "next monday".
    tokens = hint.split()
    weekday_name = next((w for w in WEEKDAY_INDEX if w in tokens or w == hint), None)
    if weekday_name:
        today = date.today()
        target = WEEKDAY_INDEX[weekday_name]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        resolved = today + timedelta(days=delta)
        return resolved.isoformat(), f"resolved from weekday hint '{hint}'"

    return None, f"could not parse date hint '{hint}'"


def _find_next_available_slots(
    start_date_iso: str,
    duration_minutes: int,
    business_id: str,
    call_info: dict,
    max_days: int = 30,
) -> dict:
    """Find the first next date with available slots within max_days."""
    try:
        start_date = datetime.strptime(start_date_iso, "%Y-%m-%d").date()
    except ValueError:
        start_date = date.today()

    for day_offset in range(max_days + 1):
        candidate = start_date + timedelta(days=day_offset)
        payload = vapi_service.get_available_slots_payload(
            {
                "date": candidate.isoformat(),
                "durationMinutes": duration_minutes,
                "businessId": business_id,
            },
            call_info,
        )

        if payload.get("error"):
            return payload
        if payload.get("slots"):
            return payload

    return {"error": f"No available slots found in the next {max_days} days"}


# ── Security ─────────────────────────────────────────────────────────────────

def _verify_vapi_auth(request: Request) -> None:
    """Reject VAPI webhook requests that don't carry the correct auth header."""
    expected_secret = settings.VAPI_AUTHENTICATION_SECRET_KEY.strip()
    if not expected_secret:
        return   # secret not configured → skip validation in dev
    header_name = settings.VAPI_AUTHENTICATION_HEADER_NAME.strip()
    received = request.headers.get(header_name, "")
    if not received or not hmac.compare_digest(received.strip(), expected_secret):
        raise HTTPException(status_code=403, detail="Forbidden: invalid VAPI credentials")


# ── Main webhook ─────────────────────────────────────────────────────────────

@router.post("/webhook")
async def vapi_webhook(
    request: Request,
):
    """
    Central VAPI server webhook.
    Configure this URL in the VAPI dashboard under
    Assistant → Advanced → Server URL  (e.g. https://your-domain.com/vapi/webhook)
    """
    print('---------------------')
    _verify_vapi_auth(request)
    print('-------------------------------------------')
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    print(f"\n{'='*60}")
    print("[VAPI] FULL REQUEST BODY:")
    print(json.dumps(body, indent=2, default=str))
    print(f"{'='*60}")

    # Support direct msgType-based payloads (flat structure)
    if isinstance(body, dict) and body.get("msgType"):
        msg_type = body.get("msgType")
        print(f"[VAPI] msgType payload detected: {msg_type}")
        
        # ── slot checking ────────────────────────────────────────────────────
        if msg_type == "slot":
            print(f"[VAPI] slot → checking available slots")
            raw_dt = str(body.get("datetime", "") or "")
            raw_day = str(body.get("day", "") or body.get("date", "") or "")
            duration = _to_int(body.get("duration", 60), 60)
            business_id = body.get("businessId", "")
            phone_number_id = body.get("phoneNumberId", "")

            print(f"[VAPI] Slot Input: datetime='{raw_dt}' day='{raw_day}' duration={duration}")

            resolved_date, resolve_reason = _resolve_date_from_hint(raw_dt, raw_day)
            if resolved_date:
                print(f"[VAPI] Date Resolution: {resolved_date} ({resolve_reason})")
            else:
                print(f"[VAPI] Date Resolution: no usable date ({resolve_reason}); searching from today")
                resolved_date = date.today().isoformat()

            # Never search in the past.
            # If the AI sent a date with the wrong year (e.g. 2025-05-02 when
            # the user meant 2026-05-02), advance the year until the date is
            # in the future rather than blindly clamping to today.
            today_iso = date.today().isoformat()
            if resolved_date < today_iso:
                try:
                    past_date = date.fromisoformat(resolved_date)
                    advanced = past_date.replace(year=past_date.year + 1)
                    # Keep advancing if still in the past (handles >1 year gap)
                    while advanced.isoformat() < today_iso:
                        advanced = advanced.replace(year=advanced.year + 1)
                    resolved_date = advanced.isoformat()
                    print(f"[VAPI] Date Correction: wrong year detected; advanced to {resolved_date}")
                except (ValueError, OverflowError):
                    print(f"[VAPI] Date Correction: requested {resolved_date} is in the past; using {today_iso}")
                    resolved_date = today_iso

            args = {
                "date": resolved_date,
                "durationMinutes": duration,
                "businessId": business_id,
            }
            # Use the corrected resolved_date for the earliest boundary, not
            # the raw (potentially wrong-year) datetime sent by the AI.
            if raw_dt and resolved_date == date.today().isoformat():
                args["earliestDateTime"] = datetime.now().isoformat()
            elif resolved_date != date.today().isoformat():
                args["earliestDateTime"] = resolved_date

            print(f"[VAPI] Earliest Slot Boundary: {args.get('earliestDateTime', 'none')}")
            call_info = {
                "phoneNumberId": phone_number_id,
                "customer": {"number": body.get("customerPhone", "")},
            }
            
            payload = vapi_service.get_available_slots_payload(args, call_info)

            if payload.get("error"):
                print(f"[VAPI] Slot Error: {payload['error']}")
                return {"status": "error", "error": payload["error"]}

            # If requested date is a closed day or has no slots, find next open date.
            originally_closed = payload.get("closedDay", False)
            originally_requested_date = resolved_date
            originally_requested_weekday = payload.get("requestedWeekday", "")

            if not payload.get("slots"):
                print("[VAPI] No slots on requested date, searching next available date...")
                # Start searching from the day AFTER the requested date so we don't
                # re-check the same closed day again.
                next_start = (date.fromisoformat(resolved_date) + timedelta(days=1)).isoformat()
                payload = _find_next_available_slots(
                    start_date_iso=next_start,
                    duration_minutes=duration,
                    business_id=business_id,
                    call_info=call_info,
                    max_days=30,
                )
                if payload.get("error"):
                    print(f"[VAPI] Slot Error: {payload['error']}")
                    return {"status": "error", "error": payload["error"]}

                # Annotate the payload so VAPI/AI knows the original date was closed
                # and that these slots belong to the next available open day.
                if originally_closed:
                    payload["originalRequestedDate"] = originally_requested_date
                    payload["originalRequestedWeekday"] = originally_requested_weekday
                    payload["originalDateClosed"] = True
                    payload["message"] = (
                        f"{originally_requested_weekday} {originally_requested_date} is a closed day. "
                        f"The next available date is {payload.get('date')} ({payload.get('requestedWeekday', '')}). "
                        f"Inform the customer that {originally_requested_weekday} is not a working day "
                        f"and offer these slots for {payload.get('date')} instead."
                    )
            
            # Print slot checking results
            print(f"\n{'='*70}")
            print("[✓ SLOT AVAILABILITY CHECK]")
            print(f"{'='*70}")
            print(f"  Business ID:       {payload.get('businessId')}")
            print(f"  Date:              {payload.get('date')}")
            print(f"  Duration:          {payload.get('durationMinutes')} minutes")
            print(f"  Slot Source:       {payload.get('slotSource')}")
            print(f"  Total Candidates:  {payload.get('totalCandidates')}")
            print(f"  Total Available:   {payload.get('totalAvailable')}")
            if payload.get('slots'):
                print(f"  Available Slots:   {', '.join(payload.get('slots', [])[:5])}")
            print(f"{'='*70}\n")
            
            return {"status": "ok", "payload": payload}

        # ── createBooking ────────────────────────────────────────────────────
        if msg_type == "createBooking":
            print(f"[VAPI] createBooking → storing booking data to database")
            args = body
            call_info = {
                "phoneNumberId": body.get("phoneNumberId", ""),
                # callerPhone = actual caller (from search_business response), customerPhone = booking target
                "customer": {"number": body.get("callerPhone") or body.get("customerPhone") or ""},
            }
            result = vapi_service.tool_create_booking(args, call_info)
            print(f"[VAPI] Result: {result}")
            return {"status": "ok", "result": result}

        # ── comnplaint / complaint aliases ────────────────────────────────
        if msg_type in {"comnplaint", "complaint", "complain"}:
            print("[VAPI] comnplaint → storing complaint data to database")
            args = body
            call_info = {
                "phoneNumberId": body.get("phoneNumberId", ""),
                "customer": {
                    "number": body.get("customerPhone") or body.get("callerPhone") or body.get("phone") or ""
                },
            }
            payload = vapi_service.create_complaint_payload(args, call_info)
            if payload.get("error"):
                print(f"[VAPI] comnplaint error: {payload['error']}")
                return {"status": "error", "error": payload["error"]}
            print(f"[VAPI] comnplaint success: {payload.get('complaintId')}")
            return {"status": "ok", "payload": payload}

        # ── checkbooking ────────────────────────────────────────────────────
        if msg_type == "checkbooking":
            print("[VAPI] checkbooking → finding booking by phone/date")
            print(
                f"[VAPI] checkbooking input: phone={body.get('phone') or body.get('customerPhone') or body.get('callerPhone')} "
                f"date={body.get('date')} datetime={body.get('datetime')}"
            )
            args = body
            call_info = {
                "phoneNumberId": body.get("phoneNumberId", ""),
                "customer": {"number": body.get("customerPhone") or body.get("callerPhone") or body.get("phone") or ""},
            }
            payload = vapi_service.check_booking_payload(args, call_info)
            if payload.get("error"):
                print(f"[VAPI] checkbooking error: {payload['error']}")
                return {"status": "error", "error": payload["error"], "booking": None}
            print(f"[VAPI] checkbooking found: {payload.get('booking') is not None}")
            return {"status": "ok", "payload": payload}

        # ── reschedule ──────────────────────────────────────────────────────
        if msg_type == "reschedule":
            print("[VAPI] reschedule → updating booking details")
            print(
                f"[VAPI] reschedule input: bookingId={body.get('bookingId')} "
                f"datetime={body.get('rescheduleDateTime') or body.get('rescheduleDate') or body.get('datetime') or body.get('dateTime')}"
            )
            args = body
            call_info = {
                "phoneNumberId": body.get("phoneNumberId", ""),
                "customer": {"number": body.get("customerPhone") or body.get("callerPhone") or body.get("phone") or ""},
            }
            payload = vapi_service.reschedule_booking_payload(args, call_info)
            if payload.get("error"):
                print(f"[VAPI] reschedule error: {payload['error']}")
                return {"status": "error", "error": payload["error"]}
            print(f"[VAPI] reschedule success: {payload.get('booking', {}).get('id')}")
            return {"status": "ok", "payload": payload}

        # ── cancel ──────────────────────────────────────────────────────────
        if msg_type == "cancel":
            print("[VAPI] cancel → cancelling booking by bookingId")
            print(f"[VAPI] cancel input: bookingId={body.get('bookingId')}")
            args = body
            call_info = {
                "phoneNumberId": body.get("phoneNumberId", ""),
                "customer": {"number": body.get("customerPhone") or body.get("callerPhone") or body.get("phone") or ""},
            }
            payload = vapi_service.cancel_booking_payload(args, call_info)
            if payload.get("error"):
                print(f"[VAPI] cancel error: {payload['error']}")
                return {"status": "error", "error": payload["error"]}
            print(f"[VAPI] cancel success: {payload.get('booking', {}).get('id')}")
            return {"status": "ok", "payload": payload}
        
        # ── search_business ──────────────────────────────────────────────────
        if msg_type == "search_business":
            print(f"[VAPI] search_business → resolving business prompt")

            _COUNTRY_TZ: dict[str, str] = {
                "PT": "Europe/Lisbon",
                "US": "America/New_York",
                "GB": "Europe/London",
                "IN": "Asia/Kolkata",
                "BR": "America/Sao_Paulo",
                "ES": "Europe/Madrid",
                "FR": "Europe/Paris",
                "DE": "Europe/Berlin",
                "IT": "Europe/Rome",
                "MX": "America/Mexico_City",
                "CA": "America/Toronto",
                "AU": "Australia/Sydney",
                "JP": "Asia/Tokyo",
                "ZA": "Africa/Johannesburg",
                "AE": "Asia/Dubai",
                "NG": "Africa/Lagos",
                "KE": "Africa/Nairobi",
                "AR": "America/Argentina/Buenos_Aires",
                "CL": "America/Santiago",
                "CO": "America/Bogota",
                "PK": "Asia/Karachi",
                "BD": "Asia/Dhaka",
            }

            def _tz_from_phone(raw_phone: str) -> str:
                """Detect IANA timezone from a phone number. Falls back to BUSINESS_TIMEZONE."""
                try:
                    import phonenumbers
                    normalized = raw_phone.strip()
                    if not normalized.startswith("+"):
                        normalized = "+" + normalized
                    num = phonenumbers.parse(normalized)
                    country = phonenumbers.region_code_for_number(num)
                    tz = _COUNTRY_TZ.get(country or "", "")
                    if tz:
                        print(f"[VAPI] search_business: detected country={country} → tz={tz}")
                    return tz or settings.BUSINESS_TIMEZONE
                except Exception:
                    return settings.BUSINESS_TIMEZONE

            def _inject_date(prompt_text: str, tz: str) -> str:
                """Replace {{date}} and {{time}} with the current datetime
                in the detected business timezone. Server-side injection is required
                because VAPI liquid tags only work in the dashboard system prompt,
                not in strings returned by tool calls.
                """
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo(tz))
                date_str = now.strftime("%A, %B %d, %Y")
                time_str = now.strftime("%I:%M %p")
                print(f"[VAPI] _inject_date: tz={tz} | date={date_str} | time={time_str}")
                result = (
                    prompt_text
                    .replace("{{date}}", date_str)
                    .replace("{{time}}", time_str)
                )
                print(f"[VAPI] _inject_date: {{{{date}}}} replaced={date_str!r}, {{{{time}}}} replaced={time_str!r}")
                return result

            phone = (
                body.get("phoneNumber")
                or body.get("businessPhone")
                or body.get("phone")
                or ""
            )
            if not phone:
                return {"status": "error", "error": "phoneNumber is required"}

            biz_tz = _tz_from_phone(phone)
            print(f"[VAPI] search_business: phone={phone} → biz_tz={biz_tz}")

            # customerNumber = {{customer.number}} = the actual caller's phone number.
            # Always echo it back so the AI carries it into every subsequent tool call.
            caller_phone = str(body.get("customerNumber") or body.get("callerPhone") or "").strip()

            business = fs.get_business_by_phone_number(phone)
            if not business:
                print(f"[VAPI] search_business: no business found for phone {phone}")
                return {"status": "error", "error": f"No business found for phone {phone}"}

            print(f"[VAPI] search_business: found business {business.get('id')} — {business.get('name')}")

            saved_prompt = business.get("vapiPrompt") or ""
            if saved_prompt:
                # Always ensure the out-of-scope rule is present,
                # even for prompts saved before the rule was introduced.
                if "[Out-of-Scope Service Rule]" not in saved_prompt:
                    saved_prompt = saved_prompt.rstrip() + "\n\n" + _OUT_OF_SCOPE_RULE.strip()
                saved_prompt = _inject_date(saved_prompt, biz_tz)
                print(f"[VAPI] search_business: using saved prompt ({len(saved_prompt)} chars)")
                return {
                    "status": "ok",
                    "prompt": saved_prompt,
                    "businessId": business.get("id"),
                    "businessName": business.get("name"),
                    "callerPhone": caller_phone,
                    "source": "saved",
                }

            # No saved prompt — return the default code-based prompt
            print(f"[VAPI] search_business: no saved prompt, using default for business {business.get('id')}")
            default_prompt = _inject_date(build_default_prompt(business.get("id", ""), business.get("name", "")), biz_tz)
            return {
                "status": "ok",
                "prompt": default_prompt,
                "businessId": business.get("id"),
                "businessName": business.get("name"),
                "callerPhone": caller_phone,
                "source": "default",
            }

        # ── checkPhone ──────────────────────────────────────────────────────
        if msg_type == "checkPhone":
            print(f"[VAPI] checkPhone → validating phone number")
            # body.get("phoneNumber") is VAPI's account phone object (a dict) —
            # accept it only when it arrives as a plain string (AI-supplied).
            _pn = body.get("phoneNumber")
            phone_input = (
                body.get("phone")
                or body.get("callerPhone")
                or body.get("customerPhone")
                or (_pn if isinstance(_pn, str) else "")
                or ""
            )
            # Reject if a dict somehow slipped in (VAPI account object)
            if isinstance(phone_input, dict):
                phone_input = ""
            print(f"[VAPI] checkPhone input: {phone_input!r}")
            if not phone_input:
                print("[VAPI] checkPhone: no phone number provided by LLM")
                return {"status": "ok", "payload": {
                    "valid": False,
                    "error": "No phone number provided. Please ask the caller for their phone number and pass it as the 'phone' parameter."
                }}
            _check_call_info = {
                "phoneNumberId": body.get("phoneNumberId", ""),
                "customer": {"number": phone_input},
            }
            result = vapi_service.check_phone_payload(body, _check_call_info)
            print(f"[VAPI] checkPhone result: valid={result.get('valid')} phone={result.get('phone')} returning={result.get('isReturningCaller')}")
            return {"status": "ok", "payload": result}

        # ── Testing (call initialisation — VAPI resolves {{customer.number}} here) ──
        if msg_type == "Testing":
            print(f"[VAPI] Testing → call init, validating caller number")
            customer_number = body.get("customerNumber") or ""
            business_id = body.get("businessId") or ""
            # Reject if VAPI sent an object instead of a string
            if isinstance(customer_number, dict):
                customer_number = customer_number.get("number", "") or ""
            print(f"[VAPI] Testing customerNumber: {customer_number!r}")
            if not customer_number:
                return {"status": "ok", "payload": {
                    "valid": False,
                    "callerPhone": "",
                    "isReturningCaller": False,
                    "customerName": "",
                    "error": "customerNumber was empty — VAPI did not resolve {{customer.number}}. Ask caller for their number manually."
                }}
            _test_call_info = {
                "phoneNumberId": body.get("phoneNumberId", ""),
                "customer": {"number": customer_number},
            }
            result = vapi_service.check_phone_payload(
                {"phone": customer_number, "businessId": business_id},
                _test_call_info,
            )
            caller_phone = result.get("phone") or customer_number
            print(f"[VAPI] Testing result: valid={result.get('valid')} callerPhone={caller_phone!r} returning={result.get('isReturningCaller')}")
            return {"status": "ok", "payload": {
                **result,
                "callerPhone": caller_phone,
            }}

        # Unknown msgType
        logger.warning(f"[VAPI] unhandled msgType: {msg_type}")
        return {"status": "ok"}

    # Support direct booking payloads (without VAPI message wrapper).
    if isinstance(body, dict) and "message" not in body and (
        "datetime" in body or "dateTime" in body
    ):
        direct_args = dict(body)
        direct_call_info = {
            "phoneNumberId": body.get("phoneNumberId", ""),
            "customer": {"number": body.get("callerPhone") or body.get("customerPhone") or ""},
        }
        if "dateTime" not in direct_args and "datetime" in direct_args:
            direct_args["dateTime"] = direct_args["datetime"]
        if "customerPhone" not in direct_args and "callerPhone" in direct_args:
            direct_args["customerPhone"] = direct_args["callerPhone"]

        print("[VAPI] Direct createBooking payload detected")
        result = vapi_service.tool_create_booking(direct_args, direct_call_info)
        print(f"[VAPI] Result: {result}")
        return {"status": "ok", "result": result}

    message = body.get("message", {})
    msg_type = message.get("type")
    call_info = message.get("call", {})

    print(f"\n{'='*60}")
    print(f"[VAPI] EVENT: {msg_type}")
    print(f"{'='*60}")

    # ── createBooking ───────────────────────────────────────────────────────
    if msg_type == "createBooking":
        print(f"[VAPI] createBooking → storing booking data to database")
        args = message.get("data", {})
        result = vapi_service.tool_create_booking(args, call_info)
        print(f"[VAPI] Result: {result}")
        return {"status": "ok", "result": result}

    # ── comnplaint / complaint aliases ─────────────────────────────────────
    if msg_type in {"comnplaint", "complaint", "complain"}:
        print("[VAPI] comnplaint (message wrapper) → storing complaint")
        args = message.get("data", {})
        payload = vapi_service.create_complaint_payload(args, call_info)
        if payload.get("error"):
            print(f"[VAPI] comnplaint error: {payload['error']}")
            return {"status": "error", "error": payload["error"]}
        return {"status": "ok", "payload": payload}

    # ── checkbooking ────────────────────────────────────────────────────────
    if msg_type == "checkbooking":
        print("[VAPI] checkbooking (message wrapper) → finding booking")
        args = message.get("data", {})
        payload = vapi_service.check_booking_payload(args, call_info)
        if payload.get("error"):
            print(f"[VAPI] checkbooking error: {payload['error']}")
            return {"status": "error", "error": payload["error"], "booking": None}
        return {"status": "ok", "payload": payload}

    # ── reschedule ──────────────────────────────────────────────────────────
    if msg_type == "reschedule":
        print("[VAPI] reschedule (message wrapper) → updating booking")
        args = message.get("data", {})
        payload = vapi_service.reschedule_booking_payload(args, call_info)
        if payload.get("error"):
            print(f"[VAPI] reschedule error: {payload['error']}")
            return {"status": "error", "error": payload["error"]}
        return {"status": "ok", "payload": payload}

    # ── cancel ──────────────────────────────────────────────────────────────
    if msg_type == "cancel":
        print("[VAPI] cancel (message wrapper) → cancelling booking")
        args = message.get("data", {})
        payload = vapi_service.cancel_booking_payload(args, call_info)
        if payload.get("error"):
            print(f"[VAPI] cancel error: {payload['error']}")
            return {"status": "error", "error": payload["error"]}
        return {"status": "ok", "payload": payload}

    # ── checkPhone ──────────────────────────────────────────────────────────
    if msg_type == "checkPhone":
        print("[VAPI] checkPhone (message wrapper) → validating phone number")
        args = message.get("data", {})
        result = vapi_service.check_phone_payload(args, call_info)
        print(f"[VAPI] checkPhone result: valid={result.get('valid')} phone={result.get('phone')} returning={result.get('isReturningCaller')}")
        return {"status": "ok", "payload": result}

    # ── Testing (message wrapper — call initialisation) ──────────────────────
    if msg_type == "Testing":
        print("[VAPI] Testing (message wrapper) → call init")
        args = message.get("data", {})
        customer_number = args.get("customerNumber") or ""
        business_id = args.get("businessId") or ""
        if isinstance(customer_number, dict):
            customer_number = customer_number.get("number", "") or ""
        if not customer_number:
            return {"status": "ok", "payload": {
                "valid": False, "callerPhone": "", "isReturningCaller": False,
                "customerName": "",
                "error": "customerNumber was empty — ask caller for their number manually."
            }}
        _test_call_info = call_info or {"customer": {"number": customer_number}}
        result = vapi_service.check_phone_payload(
            {"phone": customer_number, "businessId": business_id},
            _test_call_info,
        )
        caller_phone = result.get("phone") or customer_number
        print(f"[VAPI] Testing result: valid={result.get('valid')} callerPhone={caller_phone!r} returning={result.get('isReturningCaller')}")
        return {"status": "ok", "payload": {**result, "callerPhone": caller_phone}}

    # Unknown message type — acknowledge silently so VAPI doesn't retry
    logger.warning(f"[VAPI] unhandled message type: {msg_type}")
    return {"status": "ok"}


