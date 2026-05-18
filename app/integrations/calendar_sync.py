"""calendar_sync.py — Centralised Google Calendar ↔ DB sync utilities.

All calendar writes go through here so:
  * A single guard checks calendarConnected + calendarRefreshToken before
    every call — no silent no-ops when calendar isn't configured.
  * Retry logic (1 retry) for UPDATE and DELETE operations.
  * Every outcome is written back to the booking:
      calendarSyncStatus = "OK" | "FAILED"
      calendarSyncError  = <reason>  (only on failure)
  * Structured log lines include: action, booking_id, event_id, cal_id,
    attempt number, and Google API response.

DB is always the source of truth.  All functions here are called AFTER
the booking has been committed to Firestore.

Public surface:
  guard_calendar(business) -> (ok: bool, reason: str)
  sync_create(booking_data, business)              -> event_id: str | None
  sync_update(booking_id, event_id, business, ...) -> bool
  sync_delete(booking_id, event_id, business)      -> bool
"""

from __future__ import annotations

import logging
from datetime import datetime

from app import firestore as fs
from app.config import settings
from app.integrations.google_calendar import google_calendar

logger = logging.getLogger(__name__)

_SYNC_OK = "OK"
_SYNC_FAILED = "FAILED"


# ── Guard ─────────────────────────────────────────────────────────────────────

def guard_calendar(business: dict) -> tuple[bool, str]:
    """Return (True, "") if calendar operations are possible for this business.

    Rules:
      * calendarRefreshToken must be a non-empty string.
      * calendarConnected must not be explicitly False (allows None/missing for
        backward compat with older records that pre-date the calendarConnected flag).
    """
    refresh_token = (business.get("calendarRefreshToken") or "").strip()
    if not refresh_token:
        return False, "calendarRefreshToken is missing or empty"
    if business.get("calendarConnected") is False:
        return False, "calendarConnected is False (calendar was disconnected)"
    return True, ""


def _cal_kwargs(business: dict) -> dict:
    """Build the calendar_id + refresh_token + timezone keyword args for google_calendar calls."""
    return {
        "calendar_id": (
            business.get("ownerCalendarId")
            or settings.GOOGLE_CALENDAR_ID
            or "primary"
        ),
        "refresh_token": (business.get("calendarRefreshToken") or "").strip() or None,
        "timezone": business.get("timezone") or "UTC",
    }


# ── sync_create ───────────────────────────────────────────────────────────────

def sync_create(booking_data: dict, business: dict) -> str | None:
    """Create a Google Calendar event for a newly persisted booking.

    MUST be called AFTER the booking has been saved to Firestore.

    On success → stores calendarEventId + calendarSyncStatus="OK" in DB.
    On failure → stores calendarSyncStatus="FAILED" + calendarSyncError in DB.

    Returns the Google Calendar event_id, or None on failure.
    """
    booking_id = booking_data.get("id", "?")
    biz_id = (business or {}).get("id", "?")

    ok, reason = guard_calendar(business or {})
    if not ok:
        logger.warning(
            "[CalendarSync] SKIP CREATE booking=%s biz=%s — %s",
            booking_id, biz_id, reason,
        )
        return None

    kwargs = _cal_kwargs(business)
    logger.info(
        "[CalendarSync] ACTION=CREATE booking=%s biz=%s cal=%s",
        booking_id, biz_id, kwargs["calendar_id"],
    )

    try:
        start_dt = booking_data.get("datetime")
        if isinstance(start_dt, str):
            start_dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))

        event_id = google_calendar.create_event(
            customer_name=booking_data.get("customerName", ""),
            customer_phone=(
                booking_data.get("customerPhone")
                or booking_data.get("callerPhone", "")
            ),
            service_name=booking_data.get("serviceName", ""),
            start_dt=start_dt,
            duration_minutes=int(booking_data.get("serviceDuration") or 60),
            notes=booking_data.get("notes", ""),
            **kwargs,
        )

        if event_id:
            logger.info(
                "[CalendarSync] RESPONSE=OK CREATE booking=%s event=%s",
                booking_id, event_id,
            )
            _mark_db(booking_id, biz_id, {"calendarEventId": event_id, "calendarSyncStatus": _SYNC_OK})
        else:
            logger.error(
                "[CalendarSync] RESPONSE=FAILED CREATE booking=%s — create_event returned None",
                booking_id,
            )
            logger.error("[CalendarSync] RESPONSE=FAILED event=None")
            _mark_db(booking_id, biz_id, {
                "calendarSyncStatus": _SYNC_FAILED,
                "calendarSyncError": "create_event returned None",
            })

        return event_id

    except Exception as exc:
        logger.error(
            "[CalendarSync] EXCEPTION CREATE booking=%s: %s",
            booking_id, exc, exc_info=True,
        )
        _mark_db(booking_id, biz_id, {
            "calendarSyncStatus": _SYNC_FAILED,
            "calendarSyncError": str(exc),
        })
        return None


# ── sync_update ───────────────────────────────────────────────────────────────

def sync_update(
    booking_id: str,
    event_id: str,
    business: dict,
    start_dt: datetime,
    duration_minutes: int = 60,
    customer_name: str = "",
    service_name: str = "",
    notes: str = "",
) -> bool:
    """Update an existing Google Calendar event (reschedule or field changes).

    start_dt is required — always pass the current/new booking datetime.
    Retries once on failure.
    Returns True on success, False on failure.
    """
    biz_id = (business or {}).get("id", "?")

    ok, reason = guard_calendar(business or {})
    if not ok:
        logger.warning(
            "[CalendarSync] SKIP UPDATE booking=%s event=%s — %s",
            booking_id, event_id, reason,
        )
        return False

    kwargs = _cal_kwargs(business)
    logger.info(
        "[CalendarSync] ACTION=UPDATE booking=%s event=%s cal=%s start=%s",
        booking_id, event_id, kwargs["calendar_id"],
        start_dt.isoformat() if start_dt else "?",
    )

    for attempt in (1, 2):
        try:
            success = google_calendar.update_event(
                event_id=event_id,
                start_dt=start_dt,
                duration_minutes=duration_minutes,
                customer_name=customer_name,
                service_name=service_name,
                notes=notes,
                **kwargs,
            )
            if success:
                logger.info(
                    "[CalendarSync] RESPONSE=OK UPDATE booking=%s event=%s attempt=%s",
                    booking_id, event_id, attempt,
                )
                _mark_db(booking_id, biz_id, {"calendarSyncStatus": _SYNC_OK})
                return True
            logger.warning(
                "[CalendarSync] RESPONSE=False UPDATE booking=%s event=%s attempt=%s",
                booking_id, event_id, attempt,
            )
        except Exception as exc:
            logger.error(
                "[CalendarSync] EXCEPTION UPDATE attempt=%s booking=%s event=%s: %s",
                attempt, booking_id, event_id, exc,
            )

        if attempt == 1:
            logger.info("[CalendarSync] Retrying UPDATE booking=%s event=%s", booking_id, event_id)

    logger.error(
        "[CalendarSync] RESPONSE=FAILED UPDATE booking=%s event=%s (after retry)",
        booking_id, event_id,
    )
    _mark_db(booking_id, biz_id, {
        "calendarSyncStatus": _SYNC_FAILED,
        "calendarSyncError": "update_event failed after retry",
    })
    return False


# ── sync_delete ───────────────────────────────────────────────────────────────

def sync_delete(booking_id: str, event_id: str, business: dict) -> bool:
    """Delete a Google Calendar event for a cancelled booking.

    Retries once on failure.
    Returns True on success, False on failure.
    """
    biz_id = (business or {}).get("id", "?")

    ok, reason = guard_calendar(business or {})
    if not ok:
        logger.warning(
            "[CalendarSync] SKIP DELETE booking=%s event=%s — %s",
            booking_id, event_id, reason,
        )
        return False

    kwargs = _cal_kwargs(business)
    logger.info(
        "[CalendarSync] ACTION=DELETE booking=%s event=%s cal=%s",
        booking_id, event_id, kwargs["calendar_id"],
    )

    for attempt in (1, 2):
        try:
            success = google_calendar.delete_event(event_id, **kwargs)
            if success:
                logger.info(
                    "[CalendarSync] RESPONSE=OK DELETE booking=%s event=%s attempt=%s",
                    booking_id, event_id, attempt,
                )
                return True
            logger.warning(
                "[CalendarSync] RESPONSE=False DELETE booking=%s event=%s attempt=%s",
                booking_id, event_id, attempt,
            )
        except Exception as exc:
            logger.error(
                "[CalendarSync] EXCEPTION DELETE attempt=%s booking=%s event=%s: %s",
                attempt, booking_id, event_id, exc,
            )

        if attempt == 1:
            logger.info("[CalendarSync] Retrying DELETE booking=%s event=%s", booking_id, event_id)

    logger.error(
        "[CalendarSync] RESPONSE=FAILED DELETE booking=%s event=%s (after retry)",
        booking_id, event_id,
    )
    _mark_db(booking_id, biz_id, {
        "calendarSyncStatus": _SYNC_FAILED,
        "calendarSyncError": "delete_event failed after retry",
    })
    return False


# ── DB helpers ────────────────────────────────────────────────────────────────

def _mark_db(booking_id: str, business_id: str, fields: dict) -> None:
    """Best-effort update of sync-status fields on a booking doc."""
    if not booking_id or not business_id or booking_id == "?" or business_id == "?":
        return
    try:
        fs.update_booking(booking_id, fields, business_id)
    except Exception as exc:
        logger.error("[CalendarSync] Could not update sync status for %s: %s", booking_id, exc)
