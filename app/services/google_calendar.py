from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging

SCOPES = ['https://www.googleapis.com/auth/calendar']

logger = logging.getLogger(__name__)


def get_service(calendar_config: dict):
    """
    calendar_config comes from business["calendarConfig"] in your DB.
    It should contain the service account JSON contents and calendarId.
    """
    service_account_info = calendar_config.get("serviceAccountKey")  # dict from your DB
    
    creds = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES
    )
    return build('calendar', 'v3', credentials=creds)


def create_event(
    calendar_config: dict,
    customer_name: str,
    customer_phone: str,
    service_name: str,
    start_dt: datetime,
    duration_minutes: int,
    notes: str = "NA",
) -> str | None:
    """
    Creates a Google Calendar event and returns the event ID.
    Returns None if it fails.
    """
    try:
        service = get_service(calendar_config)
        calendar_id = calendar_config.get("calendarId", "primary")
        timezone = calendar_config.get("timezone", "UTC")

        end_dt = start_dt + timedelta(minutes=duration_minutes)

        event = {
            "summary": f"{service_name} - {customer_name}",
            "description": (
                f"Customer: {customer_name}\n"
                f"Phone: {customer_phone}\n"
                f"Service: {service_name}\n"
                f"Duration: {duration_minutes} mins\n"
                f"Notes: {notes}"
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
                    {"method": "email", "minutes": 24 * 60},
                    {"method": "popup", "minutes": 30},
                ],
            },
        }

        created = service.events().insert(
            calendarId=calendar_id,
            body=event
        ).execute()

        logger.info("[Google Calendar] Event created: %s", created.get('htmlLink'))
        return created["id"]

    except HttpError as e:
        logger.error("[Google Calendar] Failed to create event: %s", e)
        return None


def update_event(
    calendar_config: dict,
    event_id: str,
    start_dt: datetime,
    duration_minutes: int,
    customer_name: str = None,
    customer_phone: str = None,
    service_name: str = None,
    notes: str = None,
) -> bool:
    """
    Updates an existing Google Calendar event for rescheduling.
    Returns True if successful.
    """
    try:
        service = get_service(calendar_config)
        calendar_id = calendar_config.get("calendarId", "primary")
        timezone = calendar_config.get("timezone", "UTC")

        # Fetch existing event first
        event = service.events().get(
            calendarId=calendar_id,
            eventId=event_id
        ).execute()

        end_dt = start_dt + timedelta(minutes=duration_minutes)

        event["start"] = {"dateTime": start_dt.isoformat(), "timeZone": timezone}
        event["end"] = {"dateTime": end_dt.isoformat(), "timeZone": timezone}

        # Update fields if provided
        if customer_name and service_name:
            event["summary"] = f"{service_name} - {customer_name}"
        if notes:
            event["description"] = (
                f"Customer: {customer_name or 'N/A'}\n"
                f"Phone: {customer_phone or 'N/A'}\n"
                f"Service: {service_name or 'N/A'}\n"
                f"Duration: {duration_minutes} mins\n"
                f"Notes: {notes}"
            )

        service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event
        ).execute()

        logger.info("[Google Calendar] Event %s updated.", event_id)
        return True

    except HttpError as e:
        logger.error("[Google Calendar] Failed to update event: %s", e)
        return False


def delete_event(
    calendar_config: dict,
    event_id: str,
) -> bool:
    """
    Deletes a Google Calendar event for cancellations.
    Returns True if successful.
    """
    try:
        service = get_service(calendar_config)
        calendar_id = calendar_config.get("calendarId", "primary")

        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id
        ).execute()

        logger.info("[Google Calendar] Event %s deleted.", event_id)
        return True

    except HttpError as e:
        logger.error("[Google Calendar] Failed to delete event: %s", e)
        return False