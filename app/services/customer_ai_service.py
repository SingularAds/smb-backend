"""Customer AI Service — AI-driven conversation for customers messaging businesses.

When a customer sends a WhatsApp message to a business that has linked its
WhatsApp via onboarding, this service:
  1. Looks up the business from the whatsmeow device/session ID
  2. Loads business context (services, hours, etc.)
  3. Maintains per-customer conversation history
  4. Uses Claude with function calling for intent detection
  5. Routes booking/cancellation/reschedule to centralized vapi_service functions
  6. Returns a dynamic AI-generated response
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from anthropic import AsyncAnthropic

from app.config import settings
from app import firestore as db
from app.integrations import deepgram_client, cartesia_client
from app.services import vapi_service
from app.services.whatsmeow_client import WhatsmeowClient

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 30  # keep conversation context manageable


def _local_date_str(timezone_name: str) -> str:
    """Return today's date string (YYYY-MM-DD) in the given timezone."""
    try:
        import pytz
        tz = pytz.timezone(timezone_name)
        return datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


# ── Claude tool definitions (mapped to vapi_service functions) ────────────────

CUSTOMER_TOOLS = [
    {
        "name": "create_booking",
        "description": (
            "Create a new booking / appointment / reservation for the customer. "
            "Use this when the customer wants to book a service, make an appointment, "
            "or reserve a table / slot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customerName": {
                    "type": "string",
                    "description": "Customer's name",
                },
                "serviceName": {
                    "type": "string",
                    "description": "Name of the service or type of booking",
                },
                "dateTime": {
                    "type": "string",
                    "description": "Booking date and time in ISO 8601 format (e.g. 2026-04-21T14:00:00)",
                },
                "durationMinutes": {
                    "type": "integer",
                    "description": "Duration of the service in minutes",
                    "default": 60,
                },
                "partySize": {
                    "type": "integer",
                    "description": "Number of people (for restaurants/group bookings)",
                    "default": 1,
                },
                "specialRequests": {
                    "type": "string",
                    "description": "Any special requests or notes from the customer",
                },
            },
            "required": ["serviceName", "dateTime"],
        },
    },
    {
        "name": "get_available_slots",
        "description": (
            "Check which time slots are available on a given date. "
            "Use this when the customer asks about availability. "
            "Pass partySize so large-group slots are correctly filtered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date to check in YYYY-MM-DD format",
                },
                "durationMinutes": {
                    "type": "integer",
                    "description": "Duration needed in minutes",
                    "default": 60,
                },
                "partySize": {
                    "type": "integer",
                    "description": "Number of people in the party (used for capacity filtering)",
                    "default": 1,
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "check_booking",
        "description": (
            "Look up an existing booking for the customer. "
            "Use when they ask about their booking details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Optional date filter (YYYY-MM-DD)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "cancel_booking",
        "description": (
            "Cancel an existing booking. "
            "Provide bookingId if known. If not known, provide serviceName (and optionally "
            "currentDateTime) to look it up automatically from the customer's active bookings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bookingId": {
                    "type": "string",
                    "description": "The booking ID to cancel (optional if serviceName is given)",
                },
                "serviceName": {
                    "type": "string",
                    "description": "Service name to identify the booking when ID is unknown",
                },
                "currentDateTime": {
                    "type": "string",
                    "description": "Current booking date/time hint (ISO 8601) for disambiguation",
                },
            },
            "required": [],
        },
    },
    {
        "name": "reschedule_booking",
        "description": (
            "Reschedule an existing booking to a new date/time. "
            "Provide bookingId if known. If not known, provide serviceName (and optionally "
            "currentDateTime) to look it up automatically from the customer's active bookings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bookingId": {
                    "type": "string",
                    "description": "The booking ID to reschedule (optional if serviceName is given)",
                },
                "newDateTime": {
                    "type": "string",
                    "description": "New date and time in ISO 8601 format",
                },
                "serviceName": {
                    "type": "string",
                    "description": "Service name to identify the booking when ID is unknown",
                },
                "currentDateTime": {
                    "type": "string",
                    "description": "Current booking date/time hint (ISO 8601) for disambiguation",
                },
            },
            "required": ["newDateTime"],
        },
    },
    {
        "name": "update_booking",
        "description": (
            "Update details of an existing booking such as party size, special requests, "
            "or notes. Use this — NOT create_booking — when the customer wants to modify "
            "an existing reservation without changing the date or time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bookingId": {
                    "type": "string",
                    "description": "The booking ID to update",
                },
                "partySize": {
                    "type": "integer",
                    "description": "New number of people (replaces the current value)",
                },
                "specialRequests": {
                    "type": "string",
                    "description": "Updated special requests or dietary notes",
                },
                "notes": {
                    "type": "string",
                    "description": "Any additional notes for the booking",
                },
            },
            "required": ["bookingId"],
        },
    },
]


def _build_system_prompt(business: dict) -> str:
    """Build a system prompt tailored to the business."""
    name = business.get("name", "the business")
    biz_type = business.get("businessType", "business")
    vs = business.get("verticalSettings", {})
    description = vs.get("description", business.get("description", ""))
    # Prefer top-level services — owner commands (add/remove service) always update
    # this field.  verticalSettings.services is only the onboarding snapshot and may
    # be stale after subsequent edits.
    services = business.get("services") or vs.get("services", [])
    hours = vs.get("hours", business.get("hoursRaw", ""))
    opening_days: list = business.get("openingDays") or vs.get("openingDays") or []
    address = business.get("address", "")
    languages = vs.get("languages", business.get("supportedLanguages", ["en"]))
    staff = vs.get("staff", business.get("staff", []))
    phone = business.get("businessPhone", "")
    biz_timezone = business.get("timezone") or "UTC"

    services_text = ""
    if services:
        lines = []
        for s in services:
            if isinstance(s, dict):
                parts = [str(s.get("name", "") or "")]
                if s.get("duration"):
                    parts.append(str(s["duration"]))
                if s.get("price"):
                    parts.append(str(s["price"]))
                lines.append(" — ".join(p for p in parts if p))
            else:
                lines.append(str(s))
        services_text = "\n  ".join(lines)

    staff_text = ", ".join(staff) if staff else "Not specified"
    opening_days_text = ", ".join(opening_days) if opening_days else ""

    return f"""\
You are {name}'s AI receptionist on WhatsApp. You help customers with bookings, \
questions about services, hours, and general inquiries.

BUSINESS INFORMATION:
  Name: {name}
  Type: {biz_type}
  Description: {description}
  Services:
  {services_text or 'Not specified'}
  Hours: {hours or 'Not specified'}
  Open days: {opening_days_text or 'See hours above'}
  Address: {address or 'Not specified'}
  Phone: {phone or 'Not specified'}
  Staff: {staff_text}
  Languages: {', '.join(languages)}
  Timezone: {biz_timezone}

RULES:
- Be warm, professional, and concise — this is WhatsApp, keep messages short
- Detect the customer's language and respond in the same language
- When a customer wants to book, gather the required details (service, date, time) ONE question at a time
- If a missing detail is still unclear after the customer replies, ask for THAT specific detail again — do not silently skip or repeat the full intro
- Once you have service + date + time, call create_booking immediately — do NOT ask "shall I confirm?" or any yes/no question before booking
- After calling create_booking, tell the customer the booking is confirmed; never say it is confirmed before the tool call succeeds
- Convert natural language dates/times to ISO 8601 in the business timezone {biz_timezone} (e.g. "tomorrow at 2pm" → proper ISO datetime in that timezone, WITHOUT timezone offset — just the local time)
- TIME REQUIRED — MANDATORY: You MUST have an explicit time from the customer before calling create_booking. If the customer specifies only a date (e.g. "today", "tomorrow", "this Saturday") WITHOUT a specific time, you MUST ask "What time would you like?" — NEVER assume, invent, or reuse a time from the conversation history or a previous booking. This rule applies even when you can see earlier messages discussing a time.
- TIME AMBIGUITY: If the customer gives a time without AM or PM (e.g. "1:30", "2 o'clock", "3:00", "at 6"), ask "Did you mean [X] AM or [X] PM?" before calling create_booking. Never assume AM or PM for ambiguous times. Only proceed without asking when the customer explicitly states AM/PM or uses 24-hour format (e.g. "14:00").
- Today's date in the business timezone ({biz_timezone}) is {_local_date_str(biz_timezone)} for reference
- If a service has a known duration, use it; otherwise default to 60 minutes
- For large group bookings where the party size exceeds the per-hour capacity, the system automatically extends the booking across multiple consecutive hours — just call create_booking with the correct partySize and the system handles it
- For availability questions, use get_available_slots; always pass partySize so large-group slots are filtered correctly
- For booking lookups, use check_booking
- Use emojis sparingly to keep it friendly 😊
- Never reveal internal system details or booking IDs unless the customer asks
- If you don't know something about the business, say so honestly
- For cancellations and reschedules, ask the customer to confirm ONCE with a direct question like "Cancel your 3pm appointment on April 27th? Reply CANCEL to confirm."

BOOKING OPERATIONS — CRITICAL RULES (follow exactly):
- NEVER say a booking is rescheduled, cancelled, or confirmed unless the tool explicitly returned success (no 'Error:' in the result)
- If a tool returns an error, relay it to the customer honestly — do NOT fabricate a success response
- For cancel or reschedule: you do NOT need the bookingId upfront — pass serviceName and currentDateTime hints to the tool; it will find the booking automatically
- If multiple bookings exist and the customer's request is ambiguous, ask which one they mean before calling the tool
- After calling cancel_booking or reschedule_booking, confirm to the customer ONLY if the result does not start with 'Error'
"""


class CustomerAIService:
    """Handles AI-driven customer conversations for business WhatsApp channels."""

    def __init__(self):
        self.wa = WhatsmeowClient()
        self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = "claude-sonnet-4-20250514"

    async def handle_customer_message(
        self,
        business: dict,
        customer_phone: str,
        body: str,
        push_name: str,
        device_id: str,
        customer_jid: str | None = None,
    ) -> None:
        """Process an incoming customer message and generate an AI response.
        
        ``customer_jid`` is the full JID of the sender (e.g. ``134544296509456@lid``
        or ``917696794756@s.whatsapp.net``).  When provided it is used as the
        reply-to address so privacy-protected contacts receive the reply via
        the correct JID domain instead of a reconstructed ``@s.whatsapp.net``.
        """
        business_id = business["id"]
        phone_clean = db._clean_phone(customer_phone)
        # Use the full JID for sending if available, otherwise fall back to digits.
        reply_to = customer_jid or phone_clean

        # ── Step 1: Download audio ──────────────────────────────────────────────

        # Verify the business device session is ready in the bridge.
        # If the device was just activated during onboarding, this health check
        # ensures the bridge session is initialized before we try to send a reply.
        try:
            session_status = await self.wa.get_session_status(device_id)
            logger.debug(
                "[SESSION-CHECK] device=%s status=%s paired=%s",
                device_id,
                session_status.get("status"),
                session_status.get("paired"),
            )
        except Exception as check_exc:
            logger.warning(
                "[SESSION-CHECK] could not verify session %s: %s (will retry at send time)",
                device_id, check_exc,
            )

        # Load or create conversation history
        convo = db.get_customer_conversation(business_id, phone_clean)
        if convo:
            history = convo.get("messages", [])
        else:
            history = []

        # Add the new user message
        history.append({"role": "user", "content": body})

        # Trim history to keep context manageable
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        # Ensure customer record exists
        db.upsert_customer(business_id, phone_clean, {
            "name": push_name or "",
            "lastMessageAt": datetime.utcnow().isoformat(),
        })

        # Generate AI response (with potential tool calls)
        system_prompt = _build_system_prompt(business)
        context_note = f"Customer name: {push_name}. " if push_name else ""
        context_note += f"Customer phone: {phone_clean}."
        full_system = f"{system_prompt}\n\n{context_note}"

        reply = await self._get_ai_response(
            system=full_system,
            history=history,
            business=business,
            customer_phone=phone_clean,
            push_name=push_name,
        )
        # Log AI reply for visibility
        try:
            logger.debug("AI -> Customer (%s) [business=%s]: %s", phone_clean, business_id, reply)
        except Exception:
            logger.exception("AI -> Customer (logging failed)")

        # Store updated history
        history.append({"role": "assistant", "content": reply})
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        db.upsert_customer_conversation(business_id, phone_clean, {
            "messages": history,
            "customerPhone": phone_clean,
            "customerName": push_name or "",
            "businessId": business_id,
            "lastMessageAt": datetime.utcnow().isoformat(),
        })

        # Send reply via WhatsApp — use the full JID so @lid contacts are reached
        await self._send(reply_to, reply, device_id)

        logger.info(
            "Customer AI reply sent to %s for business %s (msg=%s)",
            phone_clean, business_id, body[:60],
        )

    async def _get_ai_response(
        self,
        system: str,
        history: list[dict],
        business: dict,
        customer_phone: str,
        push_name: str,
    ) -> str:
        """Send conversation to Claude with tools and handle tool calls."""
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=1000,
                system=system,
                messages=history,
                tools=CUSTOMER_TOOLS,
            )

            # Process the response
            final_text_parts = []
            tool_results = []
            _booking_created_this_turn = False  # guard: only one create_booking per turn

            for block in response.content:
                if block.type == "text":
                    final_text_parts.append(block.text)
                elif block.type == "tool_use":
                    # Guard: prevent Claude emitting two create_booking blocks in one turn
                    if block.name == "create_booking" and _booking_created_this_turn:
                        logger.warning(
                            "[DUPLICATE-BOOKING-GUARD] Skipping extra create_booking call "
                            "in the same turn for customer=%s", customer_phone,
                        )
                        tool_results.append({
                            "tool_use_id": block.id,
                            "name": block.name,
                            "result": "Booking already created this turn — do not call create_booking again.",
                        })
                        continue
                    # Execute the tool call
                    result = self._execute_tool(
                        block.name, block.input, business, customer_phone, push_name,
                    )
                    if block.name == "create_booking":
                        _booking_created_this_turn = True
                    tool_results.append({
                        "tool_use_id": block.id,
                        "name": block.name,
                        "result": result,
                    })

            # If there were tool calls, send results back to Claude for final response
            if tool_results:
                # Build tool result messages
                history_with_tools = list(history)
                history_with_tools.append({
                    "role": "assistant",
                    "content": response.content,
                })

                tool_result_content = []
                for tr in tool_results:
                    tool_result_content.append({
                        "type": "tool_result",
                        "tool_use_id": tr["tool_use_id"],
                        "content": tr["result"],
                    })

                history_with_tools.append({
                    "role": "user",
                    "content": tool_result_content,
                })

                # Get final response after tool execution
                follow_up = await self.client.messages.create(
                    model=self.model,
                    max_tokens=1000,
                    system=system,
                    messages=history_with_tools,
                    tools=CUSTOMER_TOOLS,
                )

                for block in follow_up.content:
                    if block.type == "text":
                        final_text_parts.append(block.text)

            final_reply = "\n".join(final_text_parts).strip() or "I'm here to help! How can I assist you?"
            try:
                print("AI (customer) generated reply:", final_reply)
                logger.debug("AI (customer) generated reply: %s", final_reply)
            except Exception:
                logger.exception("AI (customer) generated reply (logging failed)")
            return final_reply

        except Exception as exc:
            logger.exception("Customer AI error: %s", exc)
            return (
                "Sorry, I'm having a small technical issue. "
                "Please try again in a moment!"
            )

    def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict,
        business: dict,
        customer_phone: str,
        push_name: str,
    ) -> str:
        """Execute a tool call using vapi_service functions (centralized logic)."""
        business_id = business["id"]
        call_info = {"phoneNumberId": "", "customer": {"number": customer_phone}}
        logger.info(
            "[TOOL_CALL] tool=%s biz=%s phone=%s input=%s",
            tool_name, business_id, customer_phone, json.dumps(tool_input)[:300],
        )

        try:
            if tool_name == "create_booking":
                args = {
                    "businessId": business_id,
                    "customerPhone": customer_phone,
                    "customerName": tool_input.get("customerName") or push_name or "Customer",
                    "serviceName": tool_input.get("serviceName", "Appointment"),
                    "dateTime": tool_input.get("dateTime", ""),
                    "durationMinutes": tool_input.get("durationMinutes", 60),
                    "partySize": tool_input.get("partySize", 1),
                    "specialRequests": tool_input.get("specialRequests", ""),
                    "source": "whatsapp",
                }
                return vapi_service.tool_create_booking(args, call_info)

            elif tool_name == "get_available_slots":
                args = {
                    "businessId": business_id,
                    "date": tool_input.get("date", ""),
                    "durationMinutes": tool_input.get("durationMinutes", 60),
                    "partySize": tool_input.get("partySize", 1),
                }
                payload = vapi_service.get_available_slots_payload(args, call_info)
                if payload.get("error"):
                    return f"Error: {payload['error']}"
                slots = payload.get("slots", [])
                if not slots:
                    return f"No available slots on {tool_input.get('date', 'the requested date')}."
                readable = []
                for s in slots[:8]:
                    try:
                        readable.append(
                            datetime.fromisoformat(s).strftime("%I:%M %p").lstrip("0")
                        )
                    except ValueError:
                        readable.append(s)
                return f"Available slots on {payload.get('date', '')}: {', '.join(readable)}"

            elif tool_name == "check_booking":
                args = {
                    "businessId": business_id,
                    "customerPhone": customer_phone,
                    "date": tool_input.get("date", ""),
                }
                payload = vapi_service.check_booking_payload(args, call_info)
                if payload.get("error"):
                    return f"Error: {payload['error']}"
                all_bookings = payload.get("bookings", [])
                active = [b for b in all_bookings if (b.get("status") or "").lower() != "cancelled"]
                if not active:
                    return "No active bookings found for this customer."
                result = []
                for b in active:
                    dt_raw = b.get("datetime") or b.get("dateTime") or ""
                    try:
                        dt_fmt = datetime.fromisoformat(str(dt_raw)).strftime("%Y-%m-%d %I:%M %p") if dt_raw else dt_raw
                    except ValueError:
                        dt_fmt = dt_raw
                    result.append({
                        "bookingId": b.get("id"),
                        "service": b.get("serviceName"),
                        "dateTime": dt_fmt,
                        "status": b.get("status"),
                    })
                logger.info("[TOOL_RESULT] check_booking returned %d active bookings", len(result))
                return json.dumps(result)

            elif tool_name == "cancel_booking":
                booking_id = (tool_input.get("bookingId") or "").strip()
                # Auto-lookup: find the booking by service name when ID is not provided.
                if not booking_id:
                    booking_id = self._resolve_booking_id(
                        business_id=business_id,
                        customer_phone=customer_phone,
                        service_hint=(tool_input.get("serviceName") or "").strip(),
                        time_hint=(tool_input.get("currentDateTime") or "").strip(),
                        call_info=call_info,
                    )
                    if booking_id.startswith("Error:") or booking_id.startswith("Ambiguous:"):
                        logger.warning("[TOOL_RESULT] cancel_booking lookup failed: %s", booking_id)
                        return booking_id
                args = {
                    "businessId": business_id,
                    "bookingId": booking_id,
                    "customerPhone": customer_phone,
                }
                payload = vapi_service.cancel_booking_payload(args, call_info)
                if payload.get("error"):
                    logger.warning("[TOOL_RESULT] cancel_booking error: %s", payload['error'])
                    return f"Error: {payload['error']}"
                logger.info("[TOOL_RESULT] cancel_booking OK bookingId=%s", booking_id)
                try:
                    import asyncio as _asyncio
                    from app.services.automation.booking_automation import send_cancellation_notice
                    from app.services.automation.whatsapp_notifier import send_to_owner
                    cancelled_booking = payload.get("booking") or {"customerPhone": customer_phone, "id": booking_id}
                    _asyncio.get_event_loop().create_task(send_cancellation_notice(cancelled_booking, business))
                    _asyncio.get_event_loop().create_task(send_to_owner(
                        business,
                        f"❌ *Booking cancelled*\nCustomer: {cancelled_booking.get('customerName', push_name)}\n"
                        f"Phone: {customer_phone}\nService: {cancelled_booking.get('serviceName', '')}\n"
                        f"Booking ID: {booking_id}",
                    ))
                except Exception as _auto_err:
                    logger.warning("Cancellation notification skipped: %s", _auto_err)
                return "Booking cancelled successfully."

            elif tool_name == "reschedule_booking":
                booking_id = (tool_input.get("bookingId") or "").strip()
                # Auto-lookup: find the booking by service name when ID is not provided.
                if not booking_id:
                    booking_id = self._resolve_booking_id(
                        business_id=business_id,
                        customer_phone=customer_phone,
                        service_hint=(tool_input.get("serviceName") or "").strip(),
                        time_hint=(tool_input.get("currentDateTime") or "").strip(),
                        call_info=call_info,
                    )
                    if booking_id.startswith("Error:") or booking_id.startswith("Ambiguous:"):
                        logger.warning("[TOOL_RESULT] reschedule_booking lookup failed: %s", booking_id)
                        return booking_id
                args = {
                    "businessId": business_id,
                    "bookingId": booking_id,
                    "rescheduleDateTime": tool_input.get("newDateTime", ""),
                    "customerPhone": customer_phone,
                }
                payload = vapi_service.reschedule_booking_payload(args, call_info)
                if payload.get("error"):
                    logger.warning("[TOOL_RESULT] reschedule_booking error: %s", payload['error'])
                    return f"Error: {payload['error']}"
                logger.info("[TOOL_RESULT] reschedule_booking OK bookingId=%s newDT=%s", booking_id, tool_input.get('newDateTime'))
                updated_bk = payload.get("booking") or {}
                try:
                    import asyncio as _asyncio
                    from app.services.automation.whatsapp_notifier import send_to_owner
                    new_dt_str = tool_input.get("newDateTime", "")
                    try:
                        new_dt_fmt = datetime.fromisoformat(new_dt_str).strftime("%B %d, %Y at %I:%M %p") if new_dt_str else new_dt_str
                    except ValueError:
                        new_dt_fmt = new_dt_str
                    _asyncio.get_event_loop().create_task(send_to_owner(
                        business,
                        f"🔄 *Booking rescheduled*\nCustomer: {updated_bk.get('customerName', push_name)}\n"
                        f"Phone: {customer_phone}\nService: {updated_bk.get('serviceName', '')}\n"
                        f"New time: {new_dt_fmt}\nBooking ID: {booking_id}",
                    ))
                except Exception as _notify_err:
                    logger.warning("Reschedule owner notification skipped: %s", _notify_err)
                return "Booking rescheduled successfully."

            elif tool_name == "update_booking":
                args: dict[str, Any] = {
                    "businessId": business_id,
                    "bookingId": tool_input.get("bookingId", ""),
                    "customerPhone": customer_phone,
                }
                if tool_input.get("partySize") is not None:
                    args["partySize"] = tool_input["partySize"]
                if tool_input.get("specialRequests") is not None:
                    args["specialRequests"] = tool_input["specialRequests"]
                if tool_input.get("notes") is not None:
                    args["notes"] = tool_input["notes"]
                payload = vapi_service.update_booking_payload(args, call_info)
                if payload.get("error"):
                    return f"Error: {payload['error']}"
                return "Booking updated successfully."

            else:
                return f"Unknown tool: {tool_name}"

        except Exception as exc:
            logger.exception("Tool execution error (%s): %s", tool_name, exc)
            return f"Error executing {tool_name}: {str(exc)}"

    def _resolve_booking_id(
        self,
        business_id: str,
        customer_phone: str,
        service_hint: str,
        time_hint: str,
        call_info: dict,
    ) -> str:
        """Find the correct bookingId for a customer's active booking.

        Searches the customer's active (non-cancelled) bookings and returns the
        single matching booking's ID.  Returns an Error/Ambiguous string if the
        lookup fails so the caller can surface it to Claude.
        """
        lookup_payload = vapi_service.check_booking_payload(
            {"businessId": business_id, "customerPhone": customer_phone}, call_info
        )
        all_bkgs = lookup_payload.get("bookings", [])
        active = [b for b in all_bkgs if (b.get("status") or "").lower() != "cancelled"]

        if not active:
            return "Error: No active bookings found for this customer."

        if len(active) == 1:
            return active[0].get("id", "")

        # Multiple active bookings — try to narrow down.
        if service_hint:
            hint_lower = service_hint.lower()
            matched = [b for b in active if hint_lower in (b.get("serviceName") or "").lower()]
            if len(matched) == 1:
                return matched[0].get("id", "")
            if len(matched) > 1 and time_hint:
                # Further filter by time hint (date or hour substring)
                time_filter = time_hint[:16]  # "YYYY-MM-DDTHH:MM"
                for b in matched:
                    if time_filter in (b.get("datetime") or ""):
                        return b.get("id", "")
            if matched:
                # Best effort: return first match
                return matched[0].get("id", "")

        # Could not narrow down — return ambiguous error so Claude asks the customer
        services_list = ", ".join(
            f"{b.get('serviceName')} at {b.get('datetime', '')[:16]}"
            for b in active
        )
        return f"Ambiguous: multiple active bookings found ({services_list}). Please ask the customer which booking they mean."

    def _is_critical_message(self, reply_text: str) -> bool:
        """Return True when a reply looks transactional/critical (bookings,
        reschedules, cancellations, dates/times, payments, booking IDs).
        This heuristic helps decide when to send a text record alongside audio.
        """
        if not reply_text:
            return False
        text = reply_text.lower()
        keywords = [
            "booking confirmed",
            "your booking",
            "booking id",
            "booking #",
            "appointment",
            "rescheduled",
            "reschedule",
            "cancelled",
            "canceled",
            "confirmation",
            "slot",
            "date",
            "time",
            "payment",
            "paid",
            "invoice",
        ]
        for k in keywords:
            if k in text:
                return True
        # Match ISO dates like 2026-05-05
        if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
            return True
        # Match times like 5pm, 5 pm, 17:30
        if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", text) or re.search(r"\b\d{2}:\d{2}\b", text):
            return True
        return False

    async def handle_audio_message(
        self,
        business: dict,
        customer_phone: str,
        media_url: str,
        mime_type: str,
        push_name: str,
        device_id: str,
        customer_jid: str | None = None,
    ) -> None:
        """Process an incoming WhatsApp voice note and reply with an audio message.

        ``customer_jid`` is the full JID of the sender (preserves ``@lid`` etc.).

        Pipeline:
          1. Download audio bytes from the bridge/CDN URL
          2. Transcribe with Deepgram (STT)
          3. Run the transcription through the existing AI + booking logic
          4. Convert the AI text reply to audio via Cartesia (TTS)
          5. Send the audio reply as a WhatsApp voice note
          Fallbacks:
            - If download fails   → send an apology text message
            - If transcription fails / empty → send an apology text message
            - If TTS / audio send fails → fall back to sending the text reply
        """
        business_id = business["id"]
        phone_clean = db._clean_phone(customer_phone)
        reply_to = customer_jid or phone_clean
        try:
            audio_bytes, detected_mime = await self.wa.download_media(media_url)
            # Use the Content-Type returned by the server — it's more accurate
            # than the mime_type in the webhook payload (which can lag or be generic).
            effective_mime = detected_mime if detected_mime not in ("", "application/octet-stream") else mime_type
            logger.info(
                "[AUDIO] Downloaded %d bytes from %s for %s (mime=%r → effective=%r)",
                len(audio_bytes), media_url[:60], phone_clean, detected_mime, effective_mime,
            )
            if len(audio_bytes) < 100:
                raise ValueError(f"Downloaded audio too small ({len(audio_bytes)} bytes) — possible error response")
        except Exception as exc:
            logger.error(
                "Failed to download audio for %s (url=%s): %s", phone_clean, media_url, exc
            )
            await self._send(
                reply_to,
                "Sorry, I couldn't process your voice message. "
                "Please try again or send a text. 🙏",
                device_id,
            )
            return

        # ── Step 2: Transcribe with Deepgram ─────────────────────────────
        try:
            transcript = await deepgram_client.transcribe_audio(audio_bytes, effective_mime)
            logger.debug("[AUDIO] Transcript for %s: %r", phone_clean, transcript)
        except Exception as exc:
            logger.error("Deepgram transcription failed for %s: %s", phone_clean, exc)
            await self._send(
                reply_to,
                "Sorry, I couldn't understand your voice message. "
                "Please try again or send a text instead. 🙏",
                device_id,
            )
            return

        if not transcript:
            logger.warning("Empty transcript for %s — audio contained no speech", phone_clean)
            await self._send(
                reply_to,
                "I couldn't make out what you said. "
                "Could you please repeat or send a text message? 😊",
                device_id,
            )
            return

        # ── Step 3: AI processing (identical to text path) ────────────────
        convo = db.get_customer_conversation(business_id, phone_clean)
        history = convo.get("messages", []) if convo else []

        # Store transcription in history so Claude has context
        history.append({"role": "user", "content": f"[Voice message]: {transcript}"})
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        db.upsert_customer(business_id, phone_clean, {
            "name": push_name or "",
            "lastMessageAt": datetime.utcnow().isoformat(),
        })

        system_prompt = _build_system_prompt(business)
        context_note = f"Customer name: {push_name}. " if push_name else ""
        context_note += (
            f"Customer phone: {phone_clean}. "
            "Note: the customer sent a voice message — keep the reply concise and clear."
        )
        full_system = f"{system_prompt}\n\n{context_note}"

        reply_text = await self._get_ai_response(
            system=full_system,
            history=history,
            business=business,
            customer_phone=phone_clean,
            push_name=push_name,
        )
        logger.debug("[AUDIO] AI reply for %s: %r", phone_clean, reply_text[:100])

        # Persist updated conversation
        history.append({"role": "assistant", "content": reply_text})
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        db.upsert_customer_conversation(business_id, phone_clean, {
            "messages": history,
            "customerPhone": phone_clean,
            "customerName": push_name or "",
            "businessId": business_id,
            "lastMessageAt": datetime.utcnow().isoformat(),
        })

        # ── Step 4: TTS + send audio (fallback to text on failure) ────────
        vs = business.get("verticalSettings", {})
        languages = vs.get("languages", business.get("supportedLanguages", ["en"]))
        lang = (languages[0] if languages else "en")[:2].lower()
        voice_id: str | None = vs.get("cartesiaVoiceId") or None

        try:
            audio_reply = await cartesia_client.synthesize(
                reply_text, voice_id=voice_id, language=lang
            )
            await self._send_audio(
                reply_to, audio_reply, device_id,
                mime_type=cartesia_client.OUTPUT_MIME_TYPE,
            )
            logger.info(
                "Audio AI reply sent to %s for business %s (transcript=%s)",
                phone_clean, business_id, transcript[:60],
            )
            # Send text only for critical/transactional messages so customers
            # have a reliable record (bookings, reschedules, cancellations).
            if self._is_critical_message(reply_text):
                await self._send(reply_to, reply_text, device_id)
        except Exception as exc:
            logger.error(
                "Cartesia TTS / audio send failed for %s — falling back to text: %s",
                phone_clean, exc,
            )
            await self._send(reply_to, reply_text, device_id)

    async def _send_audio(
        self,
        phone: str,
        audio_bytes: bytes,
        device_id: str,
        mime_type: str = "audio/mpeg",
    ) -> None:
        """Send an audio message via the WhatsApp bridge."""
        try:
            logger.debug(
                "Sending WA audio (customer AI) to %s (device=%s, %d bytes, mime=%s)",
                phone, device_id, len(audio_bytes), mime_type,
            )
            await self.wa.send_audio(
                phone, audio_bytes, device_id=device_id, mime_type=mime_type, ptt=True
            )
        except Exception as exc:
            logger.error("Failed to send audio reply to %s: %s", phone, exc)
            raise

    async def _send(self, phone: str, message: str, device_id: str) -> None:
        """Send a WhatsApp message via the bridge.
        
        If the primary device (business-specific) fails, fall back to the global
        onboarding device so customer messages are never lost.
        """
        try:
            logger.info("[SEND] → %s (device=%s)", phone, device_id)
            await self.wa.send_message(phone, message, device_id=device_id)
            logger.info("[SEND] ✓ delivered to %s (device=%s)", phone, device_id)
        except Exception as exc:
            # Business device not ready — try fallback to global onboarding device
            logger.warning(
                "[SEND] device %s failed for %s (%s) — attempting fallback to global device",
                device_id, phone, exc,
            )
            try:
                fallback_device = self.wa.default_device_id
                if fallback_device != device_id:
                    logger.info("[SEND-FALLBACK] → %s (device=%s)", phone, fallback_device)
                    await self.wa.send_message(phone, message, device_id=fallback_device)
                    logger.info(
                        "[SEND-FALLBACK] ✓ delivered to %s via fallback device %s",
                        phone, fallback_device,
                    )
                    return
            except Exception as fallback_exc:
                logger.error(
                    "[SEND-FALLBACK] ✗ also failed for %s: %s",
                    phone, fallback_exc,
                )
            # Both primary and fallback failed
            logger.error("[SEND] ✗ message delivery failed for %s (primary: %s, fallback tried)", phone, exc)
