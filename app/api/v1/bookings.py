"""Bookings API Router — Firestore backed"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import firestore as fs
from app.integrations.calendar_sync import (
    sync_create as _cal_create,
    sync_delete as _cal_delete,
    sync_update as _cal_update,
)
from app.services import vapi_service
from app.services.mail_service import mail_service
from app.services.whatsmeow_client import WhatsmeowClient

logger = logging.getLogger(__name__)

router = APIRouter()
_wa = WhatsmeowClient()


class CreateBookingRequest(BaseModel):
    businessId: str = Field(..., example="boomreception-demo")
    customerName: str = Field(..., example="Sarah Johnson")
    serviceName: Optional[str] = Field(None, example="Haircut")
    datetime: str = Field(..., example="2026-04-18T20:00:00", description="ISO 8601 datetime")
    partySize: int = Field(..., ge=1, example=2)
    callerPhone: Optional[str] = Field(None, example="+18454079320")
    specialRequests: Optional[str] = Field("", example="window seat")
    source: Optional[str] = Field("dashboard", example="dashboard")


class UpdateBookingRequest(BaseModel):
    customerName: Optional[str] = None
    datetime: Optional[str] = None
    partySize: Optional[int] = Field(None, ge=1)
    specialRequests: Optional[str] = None
    callerPhone: Optional[str] = None


class AvailableSlotsRequest(BaseModel):
    date: str = Field(..., example="2026-04-18", description="Target date in YYYY-MM-DD format")
    durationMinutes: Optional[int] = Field(60, ge=1, le=360)
    businessId: Optional[str] = Field(None, example="boomreception-demo")
    phoneNumberId: Optional[str] = Field(None, example="vapi-phone-number-id")


@router.get("")
async def get_bookings(
    business_id: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """Get all bookings for a business."""
    if not business_id:
        raise HTTPException(status_code=400, detail="business_id query param required. Example: ?business_id=boomreception-demo")
    return fs.list_bookings(business_id, limit=limit, offset=offset)


@router.post("", status_code=201)
async def create_booking(body: CreateBookingRequest):
    """Create a new booking and sync to Google Calendar."""
    data = body.model_dump()
    data["status"] = "confirmed"
    data["visited"] = True  # default: assume visited unless cancelled or customer says No
    data["createdAt"] = datetime.utcnow().isoformat()
    
    booking = fs.create_booking(data)
    
    # Send confirmation email
    try:
        business = fs.get_business_by_id(body.businessId)
        if business and body.callerPhone:
            customer = fs.get_customer_by_phone(body.businessId, body.callerPhone)
            customer_email = customer.get("email") if customer else None
            
            if customer_email:
                mail_service.send_booking_confirmation(
                    customer_email=customer_email,
                    customer_name=body.customerName,
                    business_name=business.get("name", business.get("id", "Our Business")),
                    datetime_str=body.datetime,
                    party_size=body.partySize,
                    booking_id=booking.get("id"),
                    special_requests=body.specialRequests,
                )
            else:
                logger.warning(f"No email found for customer {body.callerPhone}")
    except Exception as e:
        logger.error(f"Error sending booking confirmation email: {str(e)}")

    # WhatsApp notification to business owner
    try:
        if not business:
            business = fs.get_business_by_id(body.businessId)
        owner_phone = business.get("ownerPhone") if business else None
        if owner_phone:
            if not owner_phone.startswith("+"):
                owner_phone = "+" + owner_phone
            biz_name = business.get("name", "your business")
            msg = (
                f"ðŸ“… New booking!\n"
                f"{'ðŸ†• New' if not body.callerPhone else 'ðŸ”„'} customer: {body.customerName}\n"
                f"ðŸ“… {body.datetime}\n"
                f"ðŸ‘¥ Party of {body.partySize}"
                + (f"\nðŸ“ {body.specialRequests}" if body.specialRequests else "")
                + (f"\nðŸ“ž {body.callerPhone}" if body.callerPhone else "")
            )
            await _wa.send_message(owner_phone, msg)
    except Exception as e:
        logger.warning(f"WhatsApp owner notification failed (create): {e}")

    return booking


@router.patch("/{booking_id}")
async def update_booking(
    booking_id: str,
    body: UpdateBookingRequest,
    business_id: str = "",
):
    """Update booking fields — customerName, datetime, partySize, specialRequests, callerPhone.

    When ``datetime`` changes the corresponding Google Calendar event is updated.
    """
    if not business_id:
        raise HTTPException(status_code=400, detail="business_id query param required")
    
    booking = fs.get_booking(booking_id, business_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    old_datetime = booking.get("datetime")
    new_datetime = updates.get("datetime")
    datetime_changed = new_datetime and old_datetime and old_datetime != new_datetime

    updated = fs.update_booking(booking_id, updates, business_id=business_id)
    
    # Send reschedule email if datetime changed
    if datetime_changed:
        try:
            business = fs.get_business_by_id(business_id)
            customer_phone = booking.get("callerPhone")
            
            if business and customer_phone:
                customer = fs.get_customer_by_phone(business_id, customer_phone)
                customer_email = customer.get("email") if customer else None
                
                if customer_email:
                    mail_service.send_booking_rescheduled(
                        customer_email=customer_email,
                        customer_name=updated.get("customerName") or booking.get("customerName", "Guest"),
                        business_name=business.get("name", business.get("id", "Our Business")),
                        old_datetime=old_datetime,
                        new_datetime=new_datetime,
                        party_size=updated.get("partySize") or booking.get("partySize", 1),
                        booking_id=booking_id,
                        special_requests=updated.get("specialRequests") or booking.get("specialRequests"),
                    )
                else:
                    logger.warning(f"No email found for customer {customer_phone}")
        except Exception as e:
            logger.error(f"Error sending reschedule email: {str(e)}")

    # WhatsApp notification to business owner
    try:
        business = business if datetime_changed else fs.get_business_by_id(business_id)
        owner_phone = business.get("ownerPhone") if business else None
        if owner_phone:
            if not owner_phone.startswith("+"):
                owner_phone = "+" + owner_phone
            customer_name = updated.get("customerName") or booking.get("customerName", "Customer")
            customer_phone = booking.get("callerPhone", "")
            if datetime_changed:
                msg = (
                    f"🔁 Booking rescheduled\n"
                    f"{customer_name} moved their appointment\n"
                    f"From: {old_datetime}\n"
                    f"To: {new_datetime}"
                    + (f"\n📞 {customer_phone}" if customer_phone else "")
                )
            else:
                msg = (
                    f"✏️ Booking updated\n"
                    f"Customer: {customer_name}"
                    + (f"\n📞 {customer_phone}" if customer_phone else "")
                )
            await _wa.send_message(owner_phone, msg)
    except Exception as e:
        logger.warning(f"WhatsApp owner notification failed (update): {e}")

    return {"success": True, "booking": updated}


@router.patch("/{booking_id}/confirm")
async def confirm_booking(booking_id: str, business_id: str = ""):
    """Confirm a booking."""
    if not business_id:
        raise HTTPException(status_code=400, detail="business_id query param required")
    booking = fs.get_booking(booking_id, business_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    updated = fs.update_booking(booking_id, {
        "status": "confirmed",
        "confirmedAt": datetime.utcnow().isoformat(),
    }, business_id=business_id)
    return {"success": True, "booking": updated}


@router.patch("/{booking_id}/complete")
async def complete_booking(booking_id: str, business_id: str = ""):
    """Mark a booking as completed.

    Triggers:
    - Set status = "completed" and completedAt timestamp
    - Increment customer's totalVisits
    - Referral reward flow (clear friend's 10 % discount, award referrer 25 %)
    - Clear referrer's 25 % discount after they complete a visit
    - Schedule referral invite message (sent 90 min later by sweep)
    """
    if not business_id:
        raise HTTPException(status_code=400, detail="business_id query param required")
    booking = fs.get_booking(booking_id, business_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Booking already completed")

    now_iso = datetime.utcnow().isoformat()
    updated = fs.update_booking(
        booking_id,
        {"status": "completed", "visited": True, "completedAt": now_iso},
        business_id=business_id,
    )
    if updated is None:
        updated = booking
        updated.update({"status": "completed", "completedAt": now_iso})

    # Post-completion referral logic — fire-and-forget so HTTP response is fast
    customer_phone = booking.get("customerPhone", "")
    if customer_phone:
        business = fs.get_business_by_id(business_id)
        if business:
            from app.services.referral_service import on_booking_completed
            asyncio.ensure_future(
                on_booking_completed(
                    business_id=business_id,
                    customer_phone=customer_phone,
                    booking=updated,
                    business=business,
                )
            )

    logger.info("Completed booking %s for business %s", booking_id, business_id)
    return {"success": True, "booking": updated}


@router.delete("/{booking_id}")
async def cancel_booking(booking_id: str, business_id: str = ""):
    """Cancel a booking and delete the corresponding Google Calendar event."""
    if not business_id:
        raise HTTPException(status_code=400, detail="business_id query param required")
    
    booking = fs.get_booking(booking_id, business_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    fs.update_booking(booking_id, {
        "status": "cancelled",
        "visited": False,  # explicitly cancelled → not visited
        "cancelledAt": datetime.utcnow().isoformat(),
    }, business_id=business_id)
    
    # Send cancellation email
    try:
        business = fs.get_business_by_id(business_id)
        customer_phone = booking.get("callerPhone")
        
        if business and customer_phone:
            customer = fs.get_customer_by_phone(business_id, customer_phone)
            customer_email = customer.get("email") if customer else None
            
            if customer_email:
                mail_service.send_booking_cancelled(
                    customer_email=customer_email,
                    customer_name=booking.get("customerName", "Guest"),
                    business_name=business.get("name", business.get("id", "Our Business")),
                    datetime_str=booking.get("datetime", ""),
                    party_size=booking.get("partySize", 1),
                    booking_id=booking_id,
                    reason="Booking was cancelled",
                )
            else:
                logger.warning(f"No email found for customer {customer_phone}")
    except Exception as e:
        logger.error(f"Error sending cancellation email: {str(e)}")

    # WhatsApp notification to business owner
    try:
        if not business:
            business = fs.get_business_by_id(business_id)
        owner_phone = business.get("ownerPhone") if business else None
        if owner_phone:
            if not owner_phone.startswith("+"):
                owner_phone = "+" + owner_phone
            customer_name = booking.get("customerName", "Customer")
            customer_phone = booking.get("callerPhone", "")
            booking_dt = booking.get("datetime", "")
            msg = (
                f"❌ Booking cancelled\n"
                f"{customer_name} cancelled their appointment\n"
                f"Was scheduled: {booking_dt}"
                + (f"\n📞 {customer_phone}" if customer_phone else "")
            )
            await _wa.send_message(owner_phone, msg)
    except Exception as e:
        logger.warning(f"WhatsApp owner notification failed (cancel): {e}")

    return {"success": True}


@router.post("/available-slots")
async def get_available_slots(body: AvailableSlotsRequest):
    """Get available slots using calendar + collision checks against existing bookings."""
    args = body.model_dump(exclude_none=True)
    call_info = {"phoneNumberId": body.phoneNumberId or ""}

    payload = vapi_service.get_available_slots_payload(args, call_info)
    if payload.get("error"):
        raise HTTPException(status_code=400, detail=payload["error"])

    return payload
