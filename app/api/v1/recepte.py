"""Recepte.co Lead Endpoint — receives and stores leads from the recepte.co website.

When a business owner searches for their restaurant on recepte.co and enters
their name + phone number, the website posts the lead here.  We store it in
Firestore so the onboarding AI can look it up when the owner later sends the
WhatsApp activation message ("I want to activate recepte for <BusinessName>").
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app import firestore as db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/lead")
async def save_recepte_lead(request: Request) -> dict:
    """Receive and persist a lead from the recepte.co website.

    Expected JSON payload (fields from the recepte.co registration form +
    Google Places data):
    {
        "name":         "Owner Name",
        "phone":        "+918427791370",
        "businessName": "Restaurant Name",
        "city":         "City, Region",
        "address":      "Full street address (optional, enriches onboarding card)",
        "country":      "IN",
        "type":         "restaurant",
        "url":          "https://...",
        "placeId":      "ChIJ... (Google Places ID — optional)",
        "hours":        "Mon-Sat 9:00-21:00 (optional, skips hours question in onboarding)",
        "services":     [{"name": "...", "price": "...", "duration": "..."} ...],
        "source":       "recepte.co",
        "integration":  "whatsapp"
    }

    The ``hours``, ``services``, ``address``, and ``placeId`` fields are optional
    enrichment data from Google Places. When present they are stored on the lead doc
    and surfaced in the WhatsApp onboarding confirmation card so the owner can confirm
    (or correct) them without going through a long Q&A conversation.
    """
    try:
        lead = await request.json()
    except Exception:
        return Response(
            content='{"error":"Invalid JSON"}',
            status_code=400,
            media_type="application/json",
        )

    phone = lead.get("phone", "")
    if not phone:
        return Response(
            content='{"error":"phone is required"}',
            status_code=400,
            media_type="application/json",
        )

    try:
        saved = db.save_recepte_lead(lead)
        logger.info(
            "Lead saved from recepte.co: %s (%s)",
            lead.get("businessName"),
            saved.get("phone"),
        )
        logger.info(
            "[RECEPTE] Lead saved: businessName=%r phone=%r",
            lead.get('businessName'),
            saved.get('phone'),
        )
        return {"status": "ok", "phone": saved.get("phone")}
    except Exception as exc:
        logger.exception("Failed to save recepte lead: %s", exc)
        return Response(
            content='{"error":"Internal error"}',
            status_code=500,
            media_type="application/json",
        )


@router.get("/lead/{phone}")
async def get_recepte_lead(phone: str) -> dict:
    """Look up a stored recepte.co lead by phone number (debug/admin use)."""
    lead = db.get_recepte_lead_by_phone(phone)
    if not lead:
        return Response(
            content='{"error":"Not found"}',
            status_code=404,
            media_type="application/json",
        )
    return lead
