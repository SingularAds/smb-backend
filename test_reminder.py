import asyncio
import sys
import logging

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO)

# Initialize Firebase first
import app.firebase as fb
fb.init_firebase()

from app import firestore as db
from app.services.automation.booking_automation import _send_reminder
from app.services.tz_utils import parse_dt

async def test_send_reminder(booking_id: str, business_id: str, label: str):
    """
    Sends a test reminder (24h or 2h) for a specific booking.
    """
    print(f"Fetching booking {booking_id} for business {business_id}...")
    booking = db.get_booking(booking_id, business_id)
    if not booking:
        print("Booking not found.")
        return

    business = db.get_business_by_id(business_id)
    if not business:
        print("Business not found.")
        return

    booking_dt = parse_dt(booking.get("datetime") or booking.get("date"))
    if not booking_dt:
        print("Booking missing valid datetime.")
        return

    print(f"Sending {label} reminder to {booking.get('customerPhone')}...")
    await _send_reminder(business, booking, label, booking_dt)
    print("✅ Test reminder sent successfully!")

if __name__ == "__main__":
    booking_id = "BK06E9F1"
    business_id = "s7g5l4AQGb8MrghZiimS"
    
    # Send the 24h reminder for testing
    asyncio.run(test_send_reminder(booking_id, business_id, "24h"))
