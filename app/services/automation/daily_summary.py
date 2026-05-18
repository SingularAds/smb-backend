"""Daily Summary Automation — Step 3.

Every morning (configurable, default 08:00 local UTC) the scheduler calls
run_daily_summary_for_all_businesses() which loops every active business
that has a linked WhatsApp session and sends the owner a daily digest.

Digest includes:
  • Today's bookings (count + list)
  • New customers (joined today)
  • Cancellations (cancelled today)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import pytz

from app import firestore as db
from app.services.automation.whatsapp_notifier import send_to_owner
from app.services.tz_utils import biz_tz as _biz_tz, local_day_range as _local_day_range, parse_dt as _parse_dt_tz

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw) -> datetime | None:
    return _parse_dt_tz(raw)


def _in_range(raw, start: str, end: str) -> bool:
    dt = _parse_dt(raw)
    if not dt:
        return False
    return start <= dt.isoformat() < end


async def run_daily_summary_for_all_businesses() -> None:
    """Send daily digest to every active business owner with a linked WhatsApp."""
    now = _now()
    logger.info("[AUTOMATION:DAILY_SUMMARY] starting run at %s", now.isoformat())

    businesses = db.list_active_businesses()
    sent_count = 0

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id:
            continue
        # Only send to businesses that have WhatsApp linked
        if not business.get("waSessionId"):
            continue
        owner_phone = business.get("ownerPhone") or business.get("owner_phone") or ""
        if not owner_phone:
            continue

        try:
            # Compute the local-day range per business so timezone differences are respected
            today_start, today_end = _local_day_range(business, 0)
            await _send_daily_summary(business, today_start, today_end)
            sent_count += 1
        except Exception as exc:
            logger.exception("[Automation] Daily summary failed for business %s: %s", biz_id, exc)
            logger.error("[AUTOMATION:DAILY_SUMMARY] error for biz %s: %s", biz_id, exc)

    logger.info("[AUTOMATION:DAILY_SUMMARY] done — %d/%d summaries sent", sent_count, len(businesses))


async def _send_daily_summary(business: dict, today_start: str, today_end: str) -> None:
    biz_id = business["id"]
    biz_name = business.get("name") or "Your business"
    tz = _biz_tz(business)
    _dl = datetime.fromisoformat(today_start).astimezone(tz)
    today_label = f"{_dl.day} {_dl.strftime('%b %Y')}"

    all_bookings = db.list_bookings(biz_id, limit=300)
    all_customers = db.list_customers(biz_id, limit=500)

    # Today's bookings
    today_bookings = [
        b for b in all_bookings
        if _in_range(b.get("datetime") or b.get("date"), today_start, today_end)
        and b.get("status") != "cancelled"
    ]
    today_bookings.sort(key=lambda b: b.get("datetime") or b.get("date") or "")

    # Cancellations today
    cancelled_today = [
        b for b in all_bookings
        if b.get("status") == "cancelled"
        and _in_range(b.get("updatedAt") or b.get("cancelleddAt") or b.get("datetime"), today_start, today_end)
    ]

    # New customers today (createdAt in today range)
    new_customers = [
        c for c in all_customers
        if _in_range(c.get("createdAt"), today_start, today_end)
    ]

    # Build message
    total_people = sum(int(b.get("partySize") or 1) for b in today_bookings)

    lines = [
        f"☀️ *Good morning! Daily Summary — {today_label}*",
        f"📌 *{biz_name}*",
        "",
        f"📅 *Bookings today: {len(today_bookings)} ({total_people} people)*",
    ]

    if today_bookings:
        for b in today_bookings[:10]:  # cap at 10 in the message
            dt = _parse_dt(b.get("datetime") or b.get("date"))
            time_str = dt.astimezone(tz).strftime("%H:%M") if dt else "?"
            customer = b.get("customerName") or b.get("customerPhone") or "?"
            service = b.get("serviceName") or b.get("service") or "?"
            try:
                party = int(b.get("partySize") or 1)
            except Exception:
                party = 1
            lines.append(f"  • {time_str} — {customer} ({service}) 👥 {party}")
        if len(today_bookings) > 10:
            lines.append(f"  _... and {len(today_bookings) - 10} more_")
    else:
        lines.append("  _No bookings scheduled for today_")

    lines += [
        "",
        f"🆕 *New customers today: {len(new_customers)}*",
        f"❌ *Cancellations today: {len(cancelled_today)}*",
        "",
        "_Have a great day! 💪_",
    ]

    msg = "\n".join(lines)
    logger.info("[AUTOMATION:DAILY_SUMMARY] sending to owner of biz %s (%s)", biz_id, biz_name)
    await send_to_owner(business, msg)
