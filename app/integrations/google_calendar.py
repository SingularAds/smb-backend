"""Google Calendar Integration

Supports two auth modes:
  1. OAuth user credentials (preferred) — uses the refresh_token stored after
     the business owner completes the OAuth flow. Writes events as the owner.
  2. Service account fallback — used when no refresh_token is available.
     Requires the owner to have shared their calendar with the service account:
       riley-calendar@smbaicallz.iam.gserviceaccount.com  (Make changes to events)

The calendar_id to write to is set via:
  1. business.ownerCalendarId     (per-business override, set during OAuth)
  2. settings.GOOGLE_CALENDAR_ID  (default fallback — set in .env)
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
import pytz
import requests as _requests

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def get_access_token(refresh_token: str) -> str | None:
    """Exchange a refresh_token for a fresh access_token.

    Returns the access_token string, or None on failure.
    Always generates a new token — never uses a cached/potentially-expired one.
    """
    if not refresh_token:
        logger.error("[get_access_token] refresh_token is empty — cannot fetch access token")
        return None
    try:
        resp = _requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        data = resp.json()
        if resp.status_code != 200 or "access_token" not in data:
            logger.error(
                "[get_access_token] Failed status=%s body=%s",
                resp.status_code,
                data,
            )
            return None
        token = data["access_token"]
        logger.info("[get_access_token] OK — token preview: %s...", token[:12])
        return token
    except Exception as exc:
        logger.error("[get_access_token] Exception: %s", exc)
        return None


class GoogleCalendarClient:
    """Google Calendar API wrapper.

    Prefers user OAuth credentials (refresh_token) over the service account,
    because the service account requires manual calendar sharing which most
    users won't do.
    """

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def _get_service(self, refresh_token: str | None = None):
        """Build Calendar API service.

        If ``refresh_token`` is set, use user OAuth credentials (no manual
        calendar sharing needed).  Otherwise fall back to service account.
        """
        # ── OAuth user credentials ────────────────────────────────────────
        if refresh_token:
            logger.info(
                "[GoogleCalendar] _get_service: refresh_token PRESENT (hidden)",
            )
            try:
                creds = Credentials(
                    token=None,
                    refresh_token=refresh_token,
                    token_uri=GOOGLE_TOKEN_URL,
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET,
                    scopes=self.SCOPES,
                )
                creds.refresh(GoogleAuthRequest())
                logger.info(
                    "[GoogleCalendar] OAuth token refreshed — access_token preview: %s...",
                    (creds.token or "")[:12],
                )
                return build("calendar", "v3", credentials=creds, cache_discovery=False)
            except Exception as e:
                logger.error("[GoogleCalendar] OAuth refresh FAILED: %s", e)
                logger.error("[GoogleCalendar] OAuth credentials failed, trying service account: %s", e)
        else:
            logger.warning("[GoogleCalendar] No refresh_token provided — using service account fallback")

        # ── Service account fallback ──────────────────────────────────────
        credentials_path = settings.GOOGLE_CREDENTIALS_FILE
        if not os.path.exists(credentials_path):
            logger.error("[GoogleCalendar] credentials file not found: %s", credentials_path)
            return None
        try:
            creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=self.SCOPES,
            )
            return build("calendar", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            logger.error("[GoogleCalendar] Failed to build service: %s", e)
            return None

    def create_event(
        self,
        customer_name: str,
        customer_phone: str,
        service_name: str,
        start_dt: datetime,
        duration_minutes: int = 60,
        notes: str = "",
        timezone: str = "Europe/Lisbon",
        calendar_id: str = None,
        refresh_token: str | None = None,
        # kept for backward compat — ignored
        calendar_config: dict = None,
    ) -> Optional[str]:
        """
        Create a calendar event on the owner's calendar.
        Returns the Google Calendar event ID, or None on failure.
        """
        try:
            service = self._get_service(refresh_token=refresh_token)
            if not service:
                return None

            cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID or "primary"

            end_dt = start_dt + timedelta(minutes=duration_minutes)
            tz = pytz.timezone(timezone)

            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)
                end_dt = tz.localize(end_dt)

            event_body = {
                "summary": f"{service_name} — {customer_name}",
                "description": (
                    f"Customer: {customer_name}\n"
                    f"Phone: {customer_phone}\n"
                    f"Service: {service_name}\n"
                    + (f"Notes: {notes}" if notes else "")
                ),
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": timezone,
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": timezone,
                },
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 60},
                        {"method": "popup", "minutes": 15},
                    ],
                },
            }

            logger.info(
                "[GoogleCalendar] create_event → calendar=%s start=%s",
                cal_id,
                start_dt.isoformat(),
            )
            try:
                created = service.events().insert(
                    calendarId=cal_id, body=event_body
                ).execute()
            except HttpError as http_err:
                status = http_err.resp.status if http_err.resp else 0
                logger.error("[GoogleCalendar] create_event HTTP %s: %s", status, http_err)
                if status == 401:
                    logger.error(
                        "[GoogleCalendar] 401 Unauthorized — token invalid or expired. "
                        "Business must re-connect calendar via /auth/google/connect"
                    )
                elif status == 403:
                    logger.error(
                        "[GoogleCalendar] 403 Forbidden — Google Calendar API may not be "
                        "enabled in GCP console, or the account lacks calendar permissions"
                    )
                elif status == 400:
                    logger.error(
                        "[GoogleCalendar] 400 Bad Request — invalid event payload: %s",
                        json.dumps(event_body, default=str),
                    )
                return None

            event_id = created.get("id")
            logger.info("[GoogleCalendar] Event created: %s on %s", event_id, cal_id)
            return event_id

        except Exception as e:
            logger.exception("[GoogleCalendar] create_event unexpected error: %s", e)
            return None

    def delete_event(
        self,
        event_id: str,
        calendar_id: str = None,
        refresh_token: str | None = None,
        calendar_config: dict = None,
    ) -> bool:
        """Delete a calendar event by ID. Returns True on success."""
        try:
            service = self._get_service(refresh_token=refresh_token)
            if not service:
                return False
            cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID or "primary"
            service.events().delete(calendarId=cal_id, eventId=event_id).execute()
            logger.info("[GoogleCalendar] Event deleted: %s", event_id)
            return True
        except HttpError as e:
            logger.error("[GoogleCalendar] delete_event error: %s", e)
            return False

    def update_event(
        self,
        event_id: str,
        start_dt: datetime,
        duration_minutes: int = 60,
        customer_name: str = "",
        customer_phone: str = "",
        service_name: str = "",
        notes: str = "",
        timezone: str = "Europe/Lisbon",
        calendar_id: str = None,
        refresh_token: str | None = None,
        calendar_config: dict = None,
    ) -> bool:
        """Update the start/end time and details of a calendar event.

        Uses OAuth refresh_token when available (preferred), falls back to
        service account.  Returns True on success, False on failure.
        Used when a booking is rescheduled so the owner's calendar stays in sync.
        """
        try:
            service = self._get_service(refresh_token=refresh_token)
            if not service:
                return False

            cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID or "primary"
            end_dt = start_dt + timedelta(minutes=duration_minutes)
            tz = pytz.timezone(timezone)

            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)
                end_dt = tz.localize(end_dt)

            patch_body: dict = {
                "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
                "end":   {"dateTime": end_dt.isoformat(),   "timeZone": timezone},
            }
            if customer_name or service_name:
                patch_body["summary"] = f"{service_name} — {customer_name}".strip(" — ")
            if customer_name or customer_phone or service_name or notes:
                patch_body["description"] = (
                    (f"Customer: {customer_name}\n" if customer_name else "")
                    + (f"Phone: {customer_phone}\n" if customer_phone else "")
                    + (f"Service: {service_name}\n" if service_name else "")
                    + (f"Notes: {notes}" if notes else "")
                ).strip()

            service.events().patch(
                calendarId=cal_id, eventId=event_id, body=patch_body
            ).execute()
            logger.info("[GoogleCalendar] Event updated: %s on calendar %s", event_id, cal_id)
            return True

        except HttpError as e:
            logger.error("[GoogleCalendar] update_event error: %s", e)
            return False

    def get_free_slots(
        self,
        date: str,
        duration_minutes: int = 60,
        business_hours: dict = None,
        timezone: str = "Europe/Lisbon",
        calendar_id: str = None,
        refresh_token: str | None = None,
        calendar_config: dict = None,
    ) -> list[str]:
        """
        Return list of available ISO datetime strings for a given date.
        business_hours example: {"start": "09:00", "end": "18:00"}
        """
        try:
            service = self._get_service(refresh_token=refresh_token)
            if not service:
                return []

            cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID or "primary"
            tz = pytz.timezone(timezone)

            hours = business_hours or {"start": "09:00", "end": "18:00"}
            start_h, start_m = map(int, hours["start"].split(":"))
            end_h, end_m = map(int, hours["end"].split(":"))

            day = datetime.strptime(date, "%Y-%m-%d")
            time_min = tz.localize(day.replace(hour=start_h, minute=start_m, second=0))
            time_max = tz.localize(day.replace(hour=end_h, minute=end_m, second=0))

            events_result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            busy_blocks = []
            for ev in events_result.get("items", []):
                ev_start = ev.get("start", {}).get("dateTime")
                ev_end = ev.get("end", {}).get("dateTime")
                if ev_start and ev_end:
                    busy_blocks.append((
                        datetime.fromisoformat(ev_start),
                        datetime.fromisoformat(ev_end),
                    ))

            slots = []
            cursor = time_min
            slot_delta = timedelta(minutes=30)
            slot_len = timedelta(minutes=duration_minutes)

            while cursor + slot_len <= time_max:
                slot_end = cursor + slot_len
                conflict = any(
                    not (slot_end <= b_start or cursor >= b_end)
                    for b_start, b_end in busy_blocks
                )
                if not conflict:
                    slots.append(cursor.isoformat())
                cursor += slot_delta

            return slots

        except HttpError as e:
            logger.error("[GoogleCalendar] get_free_slots error: %s", e)
            return []


google_calendar = GoogleCalendarClient()
