"""Owner command services — async functions that fetch / mutate data.

Each function returns a WhatsApp-ready reply string.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from app import firestore as db
from app.services.automation.booking_automation import send_cancellation_notice
from app.services.tz_utils import local_day_range, parse_dt, fmt_time

logger = logging.getLogger(__name__)


# ── calendar helper ───────────────────────────────────────────────────────────

def _delete_calendar_event(booking: dict, business: dict) -> None:
    """Delete the Google Calendar event for a cancelled booking (with retry + sync status)."""
    event_id = booking.get("calendarEventId")
    if not event_id:
        return
    booking_id = booking.get("id", "?")
    from app.integrations.calendar_sync import sync_delete
    sync_delete(booking_id=booking_id, event_id=event_id, business=business)

# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_booking(b: dict, business: dict) -> str:
    dt_raw = b.get("datetime") or b.get("date") or "?"
    dt = parse_dt(dt_raw)
    if dt is not None:
        time_str = fmt_time(dt, business)
    else:
        time_str = str(dt_raw)[:16]

    customer_name = b.get("customerName") or b.get("customer_name") or "Unknown"
    customer_phone = b.get("customerPhone") or b.get("customer_phone") or ""
    service = b.get("service") or b.get("serviceName") or "?"
    booking_id = b.get("id") or ""
    short_id = booking_id[:8] if booking_id else "?"
    status = b.get("status", "")
    status_icon = "✅" if status == "confirmed" else "⏳" if status == "pending" else "❌" if status == "cancelled" else "🔵"

    lines = [f"  {status_icon} *{time_str}* — {customer_name} ({service})"]
    details = []
    if customer_phone:
        details.append(f"📞 {customer_phone}")
    # party size (number of people visiting)
    try:
        party_size = int(b.get("partySize") or b.get("party_size") or b.get("party") or 1)
    except Exception:
        party_size = 1
    details.append(f"👥 {party_size}")
    details.append(f"🆔 {short_id}")
    lines.append(f"      {' | '.join(details)}")
    return "\n".join(lines)


_INACTIVE_STATUSES = {"cancelled", "no_show"}


def _bookings_for_date(business: dict, offset_days: int) -> list[dict]:
    biz_id = business.get("id") or business.get("businessId", "")
    start, end = local_day_range(business, offset_days)
    all_bookings = db.list_bookings(biz_id, limit=200)
    result = []
    for b in all_bookings:
        if b.get("status") in _INACTIVE_STATUSES:
            continue
        dt = parse_dt(b.get("datetime") or b.get("date"))
        if dt is None:
            continue
        dt_utc_iso = dt.isoformat()
        if start <= dt_utc_iso < end:
            result.append(b)
    result.sort(key=lambda b: b.get("datetime") or b.get("date") or "")
    return result


# ── command implementations ───────────────────────────────────────────────────

async def get_today_bookings(business: dict) -> str:
    bookings = _bookings_for_date(business, offset_days=0)
    if not bookings:
        return "📅 *Sem marcações para hoje.*"
    total_people = sum(int(b.get("partySize") or 1) for b in bookings)
    lines = [f"📅 *Marcações de hoje ({len(bookings)} marcações • {total_people} pessoas):*"]
    lines += [_fmt_booking(b, business) for b in bookings]
    return "\n".join(lines)


async def get_tomorrow_bookings(business: dict) -> str:
    bookings = _bookings_for_date(business, offset_days=1)
    if not bookings:
        return "📅 *Sem marcações para amanhã.*"
    total_people = sum(int(b.get("partySize") or 1) for b in bookings)
    lines = [f"📅 *Marcações de amanhã ({len(bookings)} marcações • {total_people} pessoas):*"]
    lines += [_fmt_booking(b, business) for b in bookings]
    return "\n".join(lines)


async def get_summary(business: dict) -> str:
    biz_id = business.get("id") or business.get("businessId", "")
    all_bookings = db.list_bookings(biz_id, limit=500)
    today_start, today_end = local_day_range(business, 0)
    week_start = local_day_range(business, -6)[0]
    week_end = today_end

    def _in_utc_range(b: dict, start: str, end: str) -> bool:
        dt = parse_dt(b.get("datetime") or b.get("date"))
        return dt is not None and start <= dt.isoformat() < end

    today_count = sum(
        1 for b in all_bookings
        if _in_utc_range(b, today_start, today_end)
        and b.get("status") not in _INACTIVE_STATUSES
    )
    week_count = sum(
        1 for b in all_bookings
        if _in_utc_range(b, week_start, week_end)
        and b.get("status") not in _INACTIVE_STATUSES
    )
    total = sum(1 for b in all_bookings if b.get("status") not in _INACTIVE_STATUSES)

    lines = [
        "📊 *Resumo do negócio:*",
        f"  • Hoje: *{today_count}* marcações",
        f"  • Esta semana: *{week_count}* marcações",
        f"  • Total: *{total}* marcações",
    ]
    return "\n".join(lines)


async def get_vip_clients(business: dict) -> str:
    biz_id = business.get("id") or business.get("businessId", "")
    customers = db.list_customers(biz_id, limit=200)
    vip = [c for c in customers if "vip" in (c.get("flags") or [])]
    if not vip:
        return "⭐ *Ainda não tens clientes VIP definidos.*\nPara marcar um cliente como VIP, pede ao sistema."
    lines = ["⭐ *Clientes VIP:*"]
    for c in vip:
        name = c.get("name") or c.get("customerName") or c.get("id", "?")
        phone = c.get("phone") or ""
        lines.append(f"  • {name} {phone}")
    return "\n".join(lines)


async def view_settings(business: dict) -> str:
    name = business.get("name") or business.get("businessName") or "?"
    phone = business.get("phone") or business.get("phoneNumber") or "?"
    language = business.get("language") or business.get("primary_language") or "pt"
    services = business.get("services") or []
    hours = business.get("hours") or {}

    service_list = "\n".join(f"  • {s.get('name', s) if isinstance(s, dict) else s}" for s in services) or "  (nenhum)"
    hours_text = _fmt_hours(hours)

    return (
        f"⚙️ *Definições — {name}*\n\n"
        f"📞 Telefone: {phone}\n"
        f"🌍 Idioma: {language}\n\n"
        f"💼 *Serviços:*\n{service_list}\n\n"
        f"🕐 *Horário:*\n{hours_text}"
    )


def _fmt_hours(hours: dict | Any) -> str:
    if not isinstance(hours, dict):
        return "  (não definido)"
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    day_names = {"monday": "Seg", "tuesday": "Ter", "wednesday": "Qua",
                 "thursday": "Qui", "friday": "Sex", "saturday": "Sáb", "sunday": "Dom"}
    lines = []
    for d in days:
        info = hours.get(d)
        if not info:
            continue
        if isinstance(info, dict):
            if info.get("closed"):
                lines.append(f"  {day_names[d]}: Fechado")
            else:
                lines.append(f"  {day_names[d]}: {info.get('open', '?')} – {info.get('close', '?')}")
        else:
            lines.append(f"  {day_names[d]}: {info}")
    return "\n".join(lines) or "  (não definido)"


async def cancel_booking_flow(business: dict, ref: str | None) -> str:
    biz_id = business.get("id") or business.get("businessId", "")
    today_start, today_end = local_day_range(business, 0)
    tomorrow_end = local_day_range(business, 1)[1]

    def _in_window(b: dict) -> bool:
        dt = parse_dt(b.get("datetime") or b.get("date"))
        return dt is not None and today_start <= dt.isoformat() < tomorrow_end

    if not ref:
        # Show today's + tomorrow's upcoming active bookings so owner can pick
        all_bookings = db.list_bookings(biz_id, limit=200)
        upcoming = sorted(
            [b for b in all_bookings if _in_window(b) and b.get("status") not in ("cancelled", "no_show")],
            key=lambda b: b.get("datetime") or b.get("date") or "",
        )
        if not upcoming:
            return "📅 No upcoming bookings to cancel."
        lines = ["❌ *Cancel booking* — reply with the booking number:\n"]
        for i, b in enumerate(upcoming, 1):
            dt = parse_dt(b.get("datetime") or b.get("date"))
            time_str = fmt_time(dt, business) if dt else str(b.get("datetime", "?"))[:16]
            customer = b.get("customerName") or b.get("customerPhone") or "?"
            service = b.get("service") or b.get("serviceName") or "?"
            lines.append(f"  {i}. {time_str} — {customer} ({service})")
        lines.append("\n_Reply: cancel [number] or cancel [phone]_")
        return "\n".join(lines)

    # Also support cancelling by list number (e.g. "cancel 2")
    all_bookings_for_ref = db.list_bookings(biz_id, limit=200)
    upcoming_list = sorted(
        [b for b in all_bookings_for_ref if _in_window(b) and b.get("status") not in ("cancelled", "no_show")],
        key=lambda b: b.get("datetime") or b.get("date") or "",
    )
    try:
        idx = int(ref) - 1
        if 0 <= idx < len(upcoming_list):
            b = upcoming_list[idx]
            cancelled_at = datetime.utcnow().isoformat()
            updated = db.update_booking(b["id"], {"status": "cancelled", "cancelledAt": cancelled_at}, biz_id)
            logger.info("[BOOKING] Owner cancelled booking %s for business %s", b.get("id"), biz_id)
            customer = b.get("customerName") or b.get("customerPhone") or ref
            import asyncio
            asyncio.ensure_future(send_cancellation_notice(updated or b, business))
            _delete_calendar_event(b, business)
            return f"\u2705 Booking cancelled: *{customer}*"
        return f"❌ No booking #{ref}. Type _cancel_ to see the list."
    except ValueError:
        pass

    # Try by booking ID first, then by customer phone
    booking = db.get_booking(ref, biz_id)
    if booking:
        cancelled_at = datetime.utcnow().isoformat()
        updated = db.update_booking(ref, {"status": "cancelled", "cancelledAt": cancelled_at}, biz_id)
        logger.info("[BOOKING] Owner cancelled booking %s for business %s", ref, biz_id)
        customer = booking.get("customerName") or booking.get("customerPhone") or ref
        import asyncio
        asyncio.ensure_future(send_cancellation_notice(updated or booking, business))
        _delete_calendar_event(booking, business)
        return f"\u2705 Booking cancelled: *{customer}*"

    # Search by phone
    matches = [
        b for b in all_bookings_for_ref
        if b.get("customerPhone") == ref
        and _in_window(b)
        and b.get("status") != "cancelled"
    ]
    if len(matches) == 1:
        b = matches[0]
        cancelled_at = datetime.utcnow().isoformat()
        updated = db.update_booking(b["id"], {"status": "cancelled", "cancelledAt": cancelled_at}, biz_id)
        logger.info("[BOOKING] Owner cancelled booking %s for business %s (matched by phone)", b.get("id"), biz_id)
        customer = b.get("customerName") or b.get("customerPhone") or ref
        import asyncio
        asyncio.ensure_future(send_cancellation_notice(updated or b, business))
        _delete_calendar_event(b, business)
        return f"\u2705 Booking cancelled: *{customer}*"
    if len(matches) > 1:
        lines = ["⚠️ Multiple bookings found for that phone. Use the number from the list:"]
        for b in matches:
            lines.append(f"  `{b['id']}` — {b.get('datetime', '')[:16]}")
        return "\n".join(lines)
    return f"⚠️ No active booking found for *{ref}*. Type _cancel_ to see upcoming bookings."


async def block_slot_flow(business: dict, slot: str | None) -> str:
    if not slot:
        return (
            "🚫 *Bloquear horário*\n\n"
            "Indica o horário a bloquear:\n"
            "_Ex: bloquear 14:00_"
        )
    # For now, inform the owner it needs calendar integration; 
    # if calendar_config exists we could create a busy event
    return (
        f"🚫 Horário *{slot}* marcado como bloqueado.\n"
        "_(Para bloquear dias inteiros, usa o Google Calendar ligado ao negócio.)_"
    )


async def show_services(business: dict) -> str:
    """List all services fetched from the DB (business doc)."""
    biz_id = business.get("id") or business.get("businessId")
    # Re-fetch from DB for freshest data
    if biz_id:
        fresh = db.get_business_by_id(biz_id)
        services = (fresh or {}).get("services") or []
    else:
        services = business.get("services") or []

    if not services:
        return (
            "💼 *No services registered yet.*\n\n"
            "Add one with: _add service Name | duration(min) | price(€)_\n"
            "Ex: _add service Haircut | 30 | 15_"
        )

    lines = ["💼 *Your services:*\n"]
    for i, s in enumerate(services, 1):
        if isinstance(s, dict):
            name = s.get("name", "—")
            dur = s.get("duration") or s.get("durationMinutes")
            price = s.get("price") or s.get("priceEur")
            detail = f"  {name}"
            if dur:
                detail += f" · {dur} min"
            if price is not None:
                detail += f" · €{price}"
        else:
            detail = f"  {s}"
        lines.append(f"{i}. {detail}")
    return "\n".join(lines)


async def add_service_flow(business: dict, args: dict | None = None) -> str:
    """Add a service to the business.  args may contain name/duration/price."""
    if not args or not args.get("name"):
        return (
            "➕ *Add service*\n\n"
            "Send the service details in one message:\n"
            "_Name | duration (min) | price (€)_\n\n"
            "Ex: _Haircut + Beard | 45 | 20_"
        )

    name = args["name"]
    duration = args.get("duration")
    price = args.get("price")

    new_service: dict = {"name": name}
    if duration:
        try:
            new_service["duration"] = int(duration)
        except ValueError:
            new_service["duration"] = duration
    if price:
        try:
            new_service["price"] = float(price.replace(",", ".").replace("€", "").strip())
        except ValueError:
            new_service["price"] = price

    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business. Please try again."

    # Read current services from DB and append
    fresh = db.get_business_by_id(biz_id) or {}
    services: list = list(fresh.get("services") or [])
    services.append(new_service)
    print("Updated services list:", services)
    db.update_business_doc(biz_id, {"services": services, "verticalSettings.services": services})
    logger.info("[SERVICES] Added service %r to business %s", name, biz_id)

    summary = name
    if duration:
        summary += f" · {duration} min"
    if price:
        summary += f" · €{price}"
    return (
        f"✅ *Service added:* {summary}\n\n"
        f"Total services: {len(services)}\n"
        "Type _show services_ to see the full list."
    )


async def remove_service_flow(business: dict, args: dict | None = None) -> str:
    """Remove a service by number or name."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    fresh = db.get_business_by_id(biz_id) or {}
    services: list = list(fresh.get("services") or [])

    if not services:
        return "💼 No services registered to remove."

    ref = (args or {}).get("ref")
    if not ref:
        # Show numbered list so owner can reply e.g. "remove service 2"
        lines = ["➖ *Remove service* — reply with the number or name:\n"]
        for i, s in enumerate(services, 1):
            name = s.get("name", s) if isinstance(s, dict) else s
            lines.append(f"  {i}. {name}")
        return "\n".join(lines)

    # Try by index first, then by name
    removed_name = None
    try:
        idx = int(ref) - 1
        if 0 <= idx < len(services):
            removed = services.pop(idx)
            removed_name = removed.get("name", removed) if isinstance(removed, dict) else removed
    except ValueError:
        # Match by name (case-insensitive)
        ref_lower = ref.lower()
        new_list = [s for s in services if (s.get("name", s) if isinstance(s, dict) else s).lower() != ref_lower]
        if len(new_list) == len(services):
            return f"❌ No service found matching *{ref}*. Type _show services_ to see the list."
        removed_name = ref
        services = new_list

    if removed_name is None:
        return f"❌ Invalid number. You have {len(services)} service(s)."

    db.update_business_doc(biz_id, {"services": services, "verticalSettings.services": services})
    logger.info("[SERVICES] Removed service %r from business %s", removed_name, biz_id)
    return (
        f"✅ *Service removed:* {removed_name}\n"
        f"Remaining services: {len(services)}\n"
        "Type _show services_ to see the updated list."
    )


async def change_hours_flow(business: dict, args: dict | None = None) -> str:
    """Parse 'Mon-Sat 9-19' or 'Monday closed' and store in DB."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    spec = (args or {}).get("spec")
    if not spec:
        return (
            "🕐 *Change working hours*\n\n"
            "Format options:\n"
            "  _change hours Mon-Sat 9-19_\n"
            "  _change hours Monday 9:00-18:00_\n"
            "  _change hours Sunday closed_\n\n"
            "Days: Mon/Tue/Wed/Thu/Fri/Sat/Sun (or full names)"
        )

    # Day name → firestore key
    _DAYMAP = {
        "mon": "monday", "monday": "monday", "seg": "monday", "segunda": "monday",
        "tue": "tuesday", "tuesday": "tuesday", "ter": "tuesday", "terca": "tuesday",
        "wed": "wednesday", "wednesday": "wednesday", "qua": "wednesday", "quarta": "wednesday",
        "thu": "thursday", "thursday": "thursday", "qui": "thursday", "quinta": "thursday",
        "fri": "friday", "friday": "friday", "sex": "friday", "sexta": "friday",
        "sat": "saturday", "saturday": "saturday", "sab": "saturday", "sabado": "saturday",
        "sun": "sunday", "sunday": "sunday", "dom": "sunday", "domingo": "sunday",
    }
    _DAYORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    def _parse_time(t: str) -> str:
        """Normalise '9', '9:00', '9am', '09h' → 'HH:MM'."""
        t = t.strip().lower().replace("h", ":")
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(?:am|pm)?", t)
        if not m:
            return t
        h = int(m.group(1))
        mins = m.group(2) or "00"
        if "pm" in t and h < 12:
            h += 12
        return f"{h:02d}:{mins}"

    spec_l = spec.lower().strip()
    updated_days: list[str] = []

    # Detect day range like "Mon-Sat" or single day + time / closed
    range_m = re.match(r"([a-z]+)-([a-z]+)\s+(.+)", spec_l)
    single_m = re.match(r"([a-z]+)\s+(.+)", spec_l)

    if range_m:
        start_key = _DAYMAP.get(range_m.group(1))
        end_key = _DAYMAP.get(range_m.group(2))
        time_part = range_m.group(3)
        if not start_key or not end_key:
            return f"❌ Unknown day names in _{spec}_."
        si, ei = _DAYORDER.index(start_key), _DAYORDER.index(end_key)
        days_to_set = _DAYORDER[si: ei + 1]
    elif single_m:
        day_key = _DAYMAP.get(single_m.group(1))
        if not day_key:
            return f"❌ Unknown day _{single_m.group(1)}_."
        days_to_set = [day_key]
        time_part = single_m.group(2)
    else:
        return f"❌ Could not understand format: _{spec}_"

    # Build the hours value for each day
    time_l = time_part.strip().lower()
    if time_l in ("closed", "fechado", "off", "folga"):
        day_value = {"closed": True}
    else:
        time_range_m = re.match(r"([\d:apmh]+)[-–]([\d:apmh]+)", time_l)
        if not time_range_m:
            return f"❌ Could not parse time _{time_part}_. Example: _9-19_ or _09:00-18:00_"
        day_value = {"open": _parse_time(time_range_m.group(1)), "close": _parse_time(time_range_m.group(2)), "closed": False}

    fresh = db.get_business_by_id(biz_id) or {}
    hours: dict = dict(fresh.get("hours") or {})
    for d in days_to_set:
        hours[d] = day_value
        updated_days.append(d.capitalize())

    db.update_business_doc(biz_id, {"hours": hours})
    logger.info("[HOURS] Updated hours for %s: %s", biz_id, updated_days)

    if day_value.get("closed"):
        summary = "closed"
    else:
        summary = f"{day_value.get('open')} – {day_value.get('close')}"
    return (
        f"✅ *Hours updated:* {', '.join(updated_days)} → {summary}\n"
        "Type _settings_ to review all hours."
    )


async def close_day_flow(business: dict, args: dict | None = None) -> str:
    """Mark a day-of-week or specific date as closed (blocked) or re-open it."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    spec = (args or {}).get("spec")
    is_open = (args or {}).get("open", False)

    if not spec:
        return (
            "🚫 *Close a day*\n\n"
            "Examples:\n"
            "  _closed friday_ — block every Friday\n"
            "  _closed 2026-04-25_ — block a specific date\n"
            "  _open friday_ — re-open Fridays"
        )

    _DAYMAP = {
        "mon": "monday", "monday": "monday", "seg": "monday", "segunda": "monday",
        "tue": "tuesday", "tuesday": "tuesday", "ter": "tuesday", "terca": "tuesday",
        "wed": "wednesday", "wednesday": "wednesday", "qua": "wednesday", "quarta": "wednesday",
        "thu": "thursday", "thursday": "thursday", "qui": "thursday", "quinta": "thursday",
        "fri": "friday", "friday": "friday", "sex": "friday", "sexta": "friday",
        "sat": "saturday", "saturday": "saturday", "sab": "saturday", "sabado": "saturday",
        "sun": "sunday", "sunday": "sunday", "dom": "sunday", "domingo": "sunday",
    }

    spec_l = spec.strip().lower()
    fresh = db.get_business_by_id(biz_id) or {}

    # Check if it's a date (YYYY-MM-DD or DD/MM/YYYY)
    date_m = re.match(r"(\d{4}-\d{2}-\d{2})", spec_l) or re.match(r"(\d{2}/\d{2}/\d{4})", spec_l)
    if date_m:
        date_str = date_m.group(1).replace("/", "-")
        # Normalise DD-MM-YYYY to YYYY-MM-DD if needed
        if len(date_str.split("-")[0]) == 2:
            parts = date_str.split("-")
            date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"
        blocked_dates: list = list(fresh.get("blockedDates") or [])
        if is_open:
            blocked_dates = [d for d in blocked_dates if d != date_str]
            db.update_business_doc(biz_id, {"blockedDates": blocked_dates})
            return f"✅ *{date_str}* is now open for bookings."
        else:
            if date_str not in blocked_dates:
                blocked_dates.append(date_str)
            db.update_business_doc(biz_id, {"blockedDates": blocked_dates})
            return f"🚫 *{date_str}* blocked — no bookings will be accepted on this date."

    # Day-of-week
    day_key = _DAYMAP.get(spec_l)
    if not day_key:
        return f"❌ Unknown day _{spec}_. Use day name (e.g. _friday_) or date (e.g. _2026-04-25_)."

    hours: dict = dict(fresh.get("hours") or {})
    if is_open:
        if day_key in hours:
            hours[day_key] = {"closed": False, "open": "09:00", "close": "18:00"}
        db.update_business_doc(biz_id, {"hours": hours})
        return f"✅ *{day_key.capitalize()}* is now open. Adjust time with _change hours {day_key[:3]} 9-18_."
    else:
        hours[day_key] = {"closed": True}
        db.update_business_doc(biz_id, {"hours": hours})
        return f"🚫 *{day_key.capitalize()}* is now closed — bookings will not be accepted."


async def inactive_clients_flow(business: dict, args: dict | None = None) -> str:
    """List customers who haven't visited in X days (default 30)."""
    biz_id = business.get("id") or business.get("businessId", "")
    days = int((args or {}).get("days") or 30)

    from datetime import timezone as tz
    cutoff = (datetime.now(tz.utc) - timedelta(days=days)).isoformat()

    customers = db.list_customers(biz_id, limit=500)
    inactive = []
    for c in customers:
        last = c.get("lastVisit") or c.get("last_visit") or ""
        if not last or str(last) < cutoff:
            inactive.append(c)

    if not inactive:
        return f"👍 All customers have visited within the last *{days} days*."

    lines = [f"📊 *Inactive clients (no visit in {days}+ days): {len(inactive)}*\n"]
    for c in inactive[:20]:  # cap at 20 to avoid WA message limit
        name = c.get("name") or c.get("customerName") or "?"
        phone = c.get("phone") or c.get("customerPhone") or ""
        last = c.get("lastVisit") or c.get("last_visit") or "never"
        if len(str(last)) > 10:
            last = str(last)[:10]
        lines.append(f"  • {name} {phone} (last: {last})")
    if len(inactive) > 20:
        lines.append(f"  _... and {len(inactive) - 20} more_")
    lines.append("\nReply _send outreach [message]_ to contact them.")
    return "\n".join(lines)


async def send_outreach_flow(business: dict, args: dict | None = None) -> str:
    """Send a WhatsApp message to all inactive customers (no visit in 30 days)."""
    biz_id = business.get("id") or business.get("businessId", "")
    biz_name = business.get("name") or business.get("businessName") or "Us"
    device_id = business.get("waSessionId") or business.get("deviceId") or ""

    custom_msg = (args or {}).get("message")
    if not custom_msg:
        return (
            "📤 *Send outreach*\n\n"
            "Include your message:\n"
            "_send outreach Hey! We miss you, come back for 10% off 😊_"
        )

    from datetime import timezone as tz
    cutoff = (datetime.now(tz.utc) - timedelta(days=30)).isoformat()
    customers = db.list_customers(biz_id, limit=500)
    targets = [
        c for c in customers
        if (c.get("lastVisit") or c.get("last_visit") or "") < cutoff
        and (c.get("phone") or c.get("customerPhone"))
    ]

    if not targets:
        return "👍 No inactive customers to contact right now."

    from app.services.whatsmeow_client import WhatsmeowClient
    _wa = WhatsmeowClient()
    sent, failed = 0, 0
    for c in targets:
        phone = c.get("phone") or c.get("customerPhone", "")
        name = c.get("name") or c.get("customerName") or "there"
        personalised = custom_msg.replace("{name}", name).replace("{business}", biz_name)
        try:
            await _wa.send_message(device_id=device_id, to=phone, message=personalised)
            sent += 1
        except Exception as exc:
            logger.warning("[OUTREACH] Failed to send to %s: %s", phone, exc)
            failed += 1

    logger.info("[OUTREACH] Sent %d / %d for business %s", sent, len(targets), biz_id)
    result = f"✅ *Outreach sent to {sent} customer(s).*"
    if failed:
        result += f"\n⚠️ {failed} failed (bad phone or delivery error)."
    return result


async def add_faq_flow(business: dict, args: dict | None = None) -> str:
    """Add a FAQ entry to the business knowledge base."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    question = (args or {}).get("question")
    answer = (args or {}).get("answer")

    if not question or not answer:
        return (
            "❓ *Add FAQ*\n\n"
            "Format: _add faq: Question | Answer_\n\n"
            "Ex: _add faq: Do you accept walk-ins? | Yes, subject to availability._"
        )

    fresh = db.get_business_by_id(biz_id) or {}
    faqs: list = list(fresh.get("faqs") or [])
    faqs.append({"question": question, "answer": answer})
    db.update_business_doc(biz_id, {"faqs": faqs})
    logger.info("[FAQ] Added FAQ #%d to business %s", len(faqs), biz_id)
    return (
        f"✅ *FAQ added* (#{len(faqs)}):\n"
        f"Q: {question}\n"
        f"A: {answer}"
    )


async def add_stylist_flow(business: dict, args: dict | None = None) -> str:
    """Add a stylist/staff member to the business."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    name = (args or {}).get("name")
    specialties = (args or {}).get("specialties")

    if not name:
        return (
            "👤 *Add stylist / staff*\n\n"
            "Format: _add stylist Name, specialties: skill_\n\n"
            "Ex: _add stylist Maria, specialties: color_"
        )

    new_stylist: dict = {"name": name}
    if specialties:
        new_stylist["specialties"] = specialties

    fresh = db.get_business_by_id(biz_id) or {}
    stylists: list = list(fresh.get("stylists") or [])
    stylists.append(new_stylist)
    db.update_business_doc(biz_id, {"stylists": stylists})
    logger.info("[STYLIST] Added stylist %r to business %s", name, biz_id)
    detail = name
    if specialties:
        detail += f" (specialties: {specialties})"
    return (
        f"✅ *Stylist added:* {detail}\n"
        f"Total staff: {len(stylists)}"
    )


async def change_vibe_flow(business: dict, args: dict | None = None) -> str:
    """Change the AI assistant's tone/vibe and store in DB."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    vibe = (args or {}).get("vibe")
    if not vibe:
        current = business.get("vibe") or "not set"
        return (
            f"🎨 *Change assistant vibe*\n\n"
            f"Current: _{current}_\n\n"
            "Options: _casual_, _professional_, _luxury_, _friendly_, _formal_, _fun_\n"
            "Or describe freely: _warm and welcoming_\n\n"
            "Ex: _change vibe to casual_"
        )

    db.update_business_doc(biz_id, {"vibe": vibe})
    logger.info("[VIBE] Changed vibe to %r for business %s", vibe, biz_id)
    return (
        f"✅ *Assistant vibe changed to:* _{vibe}_\n"
        "The AI will now reply in this tone to your customers."
    )


async def scan_website_flow(business: dict, args: dict | None = None) -> str:
    """Fetch a website URL and store its text content as business knowledge."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    url = (args or {}).get("url")
    if not url:
        return (
            "🌐 *Scan website*\n\n"
            "Send the URL:\n"
            "_scan https://mysite.com_"
        )

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning("[SCAN] Failed to fetch %s: %s", url, exc)
        return f"❌ Could not fetch _{url}_. Check the URL and try again."

    # Strip HTML tags, collapse whitespace
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;", " ", text)  # basic HTML entities
    text = re.sub(r"\s+", " ", text).strip()
    # Trim to 3000 chars to keep Firestore doc size reasonable
    snippet = text[:3000]

    db.update_business_doc(biz_id, {"websiteContent": snippet, "websiteUrl": url})
    logger.info("[SCAN] Stored %d chars from %s for business %s", len(snippet), url, biz_id)
    return (
        f"✅ *Website scanned:* {url}\n"
        f"Stored {len(snippet)} characters as knowledge base.\n"
        "The AI will now use this when answering customer questions."
    )


async def auto_reply_flow(business: dict, args: dict | None = None) -> str:
    """Enable or disable the customer AI auto-reply."""
    biz_id = business.get("id") or business.get("businessId")
    if not biz_id:
        return "❌ Could not identify your business."

    enabled: bool = (args or {}).get("enabled", True)
    db.update_business_doc(biz_id, {"autoReply": enabled})
    logger.info("[AUTO_REPLY] Set autoReply=%s for business %s", enabled, biz_id)
    if enabled:
        return (
            "✅ *Auto-reply enabled.*\n"
            "The AI will now respond to customer messages automatically."
        )
    return (
        "🔕 *Auto-reply disabled.*\n"
        "Customers will NOT receive automated responses until you turn it back on.\n"
        "Type _turn on auto reply_ to re-enable."
    )

async def help_command(business: dict) -> str:
    name = business.get("name") or business.get("businessName") or "your business"
    return (
        f"🤖 *Panel Commands — {name}*\n\n"
        "📅 *Bookings:*\n"
        "  • `today` — today's bookings\n"
        "  • `tomorrow` — tomorrow's bookings\n"
        "  • `summary` — statistics\n"
        "  • `vip` — VIP clients\n\n"
        "❌ *Cancel:*\n"
        "  • `cancel` — list upcoming bookings to cancel\n"
        "  • `cancel 2` — cancel booking #2 from list\n"
        "  • `cancel 351912345678` — cancel by phone\n\n"
        "💼 *Services:*\n"
        "  • `show services` — list all services\n"
        "  • `add service Name | 30 | 15` — add (name|min|€)\n"
        "  • `remove service 1` — remove by number or name\n\n"
        "🕐 *Hours:*\n"
        "  • `change hours Mon-Sat 9-19` — set hours\n"
        "  • `closed friday` — block a day\n"
        "  • `closed 2026-12-25` — block specific date\n"
        "  • `open friday` — re-open a day\n\n"
        "📊 *Customers:*\n"
        "  • `inactive clients` — last 30-day inactive\n"
        "  • `inactive clients 60` — last 60-day inactive\n"
        "  • `send outreach Hi, we miss you!` — message inactive\n\n"
        "❓ *Knowledge:*\n"
        "  • `add faq: Question | Answer` — add FAQ\n"
        "  • `add stylist Maria, specialties: color` — add staff\n"
        "  • `scan https://mysite.com` — import website\n"
        "  • `change vibe to casual` — set AI tone\n\n"
        "⚙️ *System:*\n"
        "  • `settings` — view config\n"
        "  • `turn off auto reply` — disable AI responses\n"
        "  • `turn on auto reply` — enable AI responses\n"
        "  • `help` — show this menu"
    )
