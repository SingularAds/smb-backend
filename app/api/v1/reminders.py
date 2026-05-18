"""Booking Reminders — Cron-triggered endpoint

Call this endpoint from a cron job (e.g. daily at 9 AM) to send SMS reminders
to customers with upcoming bookings in the next 1–3 days.

POST /api/v1/reminders/send
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Request
from typing import Any

from app import firestore as fs
from app.config import settings
from app.services.notification_service import notifications


def _verify_auth(request: Request) -> None:
    expected = settings.VAPI_AUTHENTICATION_SECRET_KEY.strip()
    if not expected:
        return  # not configured — skip in dev
    header_name = settings.VAPI_AUTHENTICATION_HEADER_NAME.strip()
    received = request.headers.get(header_name, "")
    if not received or not hmac.compare_digest(received.strip(), expected):
        raise HTTPException(status_code=403, detail="Forbidden: invalid credentials")

router = APIRouter()


def _parse_dt(raw: Any) -> datetime | None:
    """Parse ISO string or Firestore Timestamp to a business-timezone-aware datetime."""
    biz_tz = ZoneInfo(settings.BUSINESS_TIMEZONE)
    if raw is None:
        return None
    if hasattr(raw, "ToDatetime"):       # Firestore Timestamp
        return raw.ToDatetime().replace(tzinfo=timezone.utc).astimezone(biz_tz)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Naive datetimes stored in Firestore are already local time
            dt = dt.replace(tzinfo=biz_tz)
        else:
            dt = dt.astimezone(biz_tz)
        return dt
    except (ValueError, TypeError):
        return None


@router.post("/send")
async def send_reminders(
    request: Request,
    dry_run: bool = False,
):
    """
    Scan all businesses' confirmed bookings and send SMS reminders for any
    booking happening in 0, 1, 2, or 3 days from now (UTC).

    Add `?dry_run=true` to preview what would be sent without actually sending.
    """
    _verify_auth(request)

    biz_tz = ZoneInfo(settings.BUSINESS_TIMEZONE)
    now_local = datetime.now(biz_tz)
    today = now_local.date()

    # Reminder windows: today (0), tomorrow (1), in 2 days (2), in 3 days (3)
    target_dates = {today + timedelta(days=d): d for d in range(4)}

    print(f"\n[Reminders] ── Starting reminder run at {now_local.isoformat()} ({settings.BUSINESS_TIMEZONE}) ──")
    print(f"[Reminders] Target dates: {[str(d) for d in target_dates]}")

    # Fetch all businesses
    from firebase_admin import firestore as fb_firestore
    db = fb_firestore.client()
    biz_docs = list(db.collection("businesses").stream())
    print(f"[Reminders] Found {len(biz_docs)} businesses")

    sent = 0
    skipped = 0
    failed = 0
    results: list[dict] = []

    for biz_doc in biz_docs:
        biz = biz_doc.to_dict()
        biz["id"] = biz_doc.id
        biz_id = biz["id"]
        biz_name = biz.get("name") or biz_id

        # Stream confirmed bookings for this business
        bookings_ref = (
            db.collection("businesses")
            .document(biz_id)
            .collection("bookings")
            .where("status", "==", "confirmed")
            .stream()
        )

        for bdoc in bookings_ref:
            booking = bdoc.to_dict()
            booking["id"] = bdoc.id

            booking_dt = _parse_dt(booking.get("datetime"))
            if not booking_dt:
                skipped += 1
                continue

            booking_date = booking_dt.date()
            if booking_date not in target_dates:
                continue

            days_until = target_dates[booking_date]

            customer_phone = str(booking.get("customerPhone") or "").strip()
            customer_name = str(booking.get("customerName") or "there").strip()
            service_name = str(booking.get("serviceName") or "Appointment").strip()
            language = str(booking.get("language") or "en").strip()

            if not customer_phone:
                print(f"[Reminders]   SKIP booking {booking['id']} — no customer phone")
                skipped += 1
                continue

            formatted_dt = booking_dt.strftime("%B %d, %Y at %I:%M %p")

            print(
                f"[Reminders]   {'DRY-RUN ' if dry_run else ''}→ {customer_name} "
                f"({customer_phone}) — {biz_name} — {service_name} — in {days_until}d"
            )

            entry = {
                "bookingId": booking["id"],
                "businessId": biz_id,
                "businessName": biz_name,
                "customerName": customer_name,
                "customerPhone": customer_phone,
                "serviceName": service_name,
                "bookingDatetime": formatted_dt,
                "daysUntil": days_until,
                "sent": False,
            }

            if not dry_run:
                ok = notifications.send_booking_reminder(
                    customer_phone=customer_phone,
                    customer_name=customer_name,
                    business_name=biz_name,
                    service_name=service_name,
                    booking_datetime=formatted_dt,
                    days_until=days_until,
                    language=language,
                    business_type=biz.get("businessType", ""),
                    business_phone=biz.get("phoneNumber") or biz.get("ownerPhone") or "",
                )
                entry["sent"] = ok
                if ok:
                    sent += 1
                else:
                    failed += 1
            else:
                entry["sent"] = "dry_run"
                sent += 1

            results.append(entry)

    print(
        f"[Reminders] ── Done: {sent} sent, {skipped} skipped, {failed} failed ──\n"
    )

    return {
        "status": "ok",
        "dry_run": dry_run,
        "ran_at": now_local.isoformat(),
        "summary": {
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
            "total_reminders": len(results),
        },
        "reminders": results,
    }
