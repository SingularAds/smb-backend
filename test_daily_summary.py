import asyncio
import sys
import logging

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO)

# Initialize Firebase first
import app.firebase as fb
fb.init_firebase()

from app import firestore as db
from app.services.automation.daily_summary import _send_daily_summary
from app.services.tz_utils import local_day_range

async def test_daily_summary(business_id: str):
    print(f"Fetching business {business_id}...")
    business = db.get_business_by_id(business_id)
    if not business:
        print("Business not found.")
        return

    # 0 = today
    today_start, today_end = local_day_range(business, 0)
    
    print(f"Sending daily summary for {business.get('name')} to owner phone...")
    await _send_daily_summary(business, today_start, today_end)
    print("✅ Test daily summary sent successfully!")

if __name__ == "__main__":
    business_id = "s7g5l4AQGb8MrghZiimS"
    asyncio.run(test_daily_summary(business_id))
