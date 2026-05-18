"""Shared timezone utilities for the booking system.

All bookings are stored as UTC ISO-8601 strings.
These helpers centralise timezone resolution and conversion so every part of
the application — customer-facing and owner-facing — always shows the same
correct business-local time.

Usage pattern:
    from app.services.tz_utils import biz_tz, local_day_range, fmt_time, parse_dt

    # Get local-day boundaries for filtering stored UTC datetimes
    start_utc, end_utc = local_day_range(business, offset_days=0)

    # Format a stored UTC datetime string for display
    dt = parse_dt(booking["datetime"])
    time_str = fmt_time(dt, business)          # "14:30"
    dt_str   = fmt_datetime(dt, business)      # "11 May 2026 14:30"
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import pytz

_UTC = timezone.utc


# ── Timezone resolution ───────────────────────────────────────────────────────

def biz_tz(business: dict) -> pytz.BaseTzInfo:
    """Return the pytz timezone for *business*.  Falls back to UTC."""
    tz_name = (business or {}).get("timezone") or "UTC"
    try:
        return pytz.timezone(tz_name)
    except Exception:
        return pytz.UTC


# ── Datetime parsing ──────────────────────────────────────────────────────────

def parse_dt(raw) -> Optional[datetime]:
    """Parse any ISO-8601 string or datetime into a UTC-aware datetime.

    Naive datetimes are assumed to be UTC (defensive — all stored datetimes
    should already carry a UTC offset).
    """
    if not raw:
        return None
    try:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=_UTC)
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=_UTC)
    except (ValueError, TypeError):
        return None


# ── Timezone conversion ───────────────────────────────────────────────────────

def to_local(dt: datetime, business: dict) -> datetime:
    """Convert a UTC-aware datetime to the business-local timezone."""
    return dt.astimezone(biz_tz(business))


# ── Display formatting ────────────────────────────────────────────────────────

def fmt_time(dt: datetime, business: dict) -> str:
    """Format a datetime as 'HH:MM' in the business-local timezone."""
    return to_local(dt, business).strftime("%H:%M")


def fmt_datetime(dt: datetime, business: dict) -> str:
    """Format a datetime as 'D Mon YYYY HH:MM' in the business-local timezone."""
    local = to_local(dt, business)
    return f"{local.day} {local.strftime('%b %Y')} {local.strftime('%H:%M')}"


def fmt_datetime_long(dt: datetime, business: dict) -> str:
    """Format a datetime as 'Month D, YYYY at HH:MM AM/PM' in the business-local timezone."""
    return to_local(dt, business).strftime("%B %d, %Y at %I:%M %p")


# ── Day boundary calculation ──────────────────────────────────────────────────

def local_day_range(business: dict, offset_days: int = 0) -> tuple[str, str]:
    """Return (start_utc_iso, end_utc_iso) for the business-local calendar day.

    offset_days=0  → today in business-local time
    offset_days=1  → tomorrow in business-local time
    offset_days=-6 → 6 days ago in business-local time

    The returned strings are UTC ISO-8601 and safe for direct comparison with
    the UTC datetimes stored in Firestore booking documents.
    """
    tz = biz_tz(business)
    now_local = datetime.now(tz)
    # Midnight of the target local day
    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    local_start = local_midnight + timedelta(days=offset_days)
    local_end = local_start + timedelta(days=1)
    # Convert to UTC ISO strings for storage comparison
    start_utc = local_start.astimezone(pytz.UTC).isoformat()
    end_utc = local_end.astimezone(pytz.UTC).isoformat()
    return start_utc, end_utc
