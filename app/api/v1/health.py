"""Health Check Router"""

from fastapi import APIRouter, HTTPException
from datetime import datetime
import urllib.parse

from app.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/ping")
async def ping():
    """Simple ping endpoint"""
    return {"message": "pong"}


@router.get("/verify-phone/{phone_number:path}")
async def verify_phone(phone_number: str):
    """
    Verify whether a phone number is valid using Twilio Lookup API.
    Pass the number URL-encoded, e.g. /verify-phone/%2B919434800080
    or just /verify-phone/919434800080 ('+' will be added automatically).
    """
    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException

        phone = urllib.parse.unquote(phone_number).strip()
        if not phone.startswith("+"):
            phone = "+" + phone

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        lookup = client.lookups.v2.phone_numbers(phone).fetch()

        return {
            "phone": phone,
            "valid": lookup.valid,
            "national_format": lookup.national_format,
            "country_code": lookup.country_code,
            "calling_country_code": lookup.calling_country_code,
            "phone_number": lookup.phone_number,
        }

    except Exception as e:
        # Twilio returns 404 / specific codes for invalid numbers
        err = str(e)
        if "20404" in err or "not found" in err.lower() or "Unable to fetch" in err:
            return {"phone": phone_number, "valid": False, "error": "Invalid or unrecognized phone number"}
        raise HTTPException(status_code=400, detail=err)
