"""Weekly Summary Automation — Step 4.

Runs every Sunday at 19:00 (7 PM) local time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

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
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        return start_dt <= dt < end_dt
    except (ValueError, TypeError):
        return False


async def run_weekly_summary_for_all_businesses(now: datetime | None = None) -> None:
    if now is None:
        now = _now()
    logger.info("[AUTOMATION:WEEKLY_SUMMARY] starting run at %s", now.isoformat())

    businesses = db.list_active_businesses()
    sent_count = 0

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id:
            continue
        if not business.get("waSessionId"):
            continue
        owner_phone = business.get("ownerPhone") or business.get("owner_phone") or ""
        if not owner_phone:
            continue

        tz = _biz_tz(business)
        now_local = now.astimezone(tz)
        
        # Only run on Sunday at 7 PM (19:00)
        if now_local.weekday() != 6 or now_local.hour != 19:
            continue

        try:
            # 7 days ago to today
            week_start, _ = _local_day_range(business, -6, now=now)
            _, week_end = _local_day_range(business, 0, now=now)
            await _send_weekly_summary(business, week_start, week_end, now=now)
            sent_count += 1
        except Exception as exc:
            logger.exception("[Automation] Weekly summary failed for business %s: %s", biz_id, exc)
            logger.error("[AUTOMATION:WEEKLY_SUMMARY] error for biz %s: %s", biz_id, exc)

    logger.info("[AUTOMATION:WEEKLY_SUMMARY] done — %d/%d summaries sent", sent_count, len(businesses))


def _calls_week(business: dict, today_local_iso: str) -> int:
    """Read the rolling 7-day weekly call counter, gated on its date range.

    `weekly_counter` is a rolling 7-day window starting on the first call of
    the window. We only trust it if "today" (business-local) falls inside the
    stored range — otherwise the value is stale and we report 0.
    """
    weekly_range = business.get("weekly_counter_date_range") or {}
    start_iso = weekly_range.get("start")
    end_iso = weekly_range.get("end")
    if not (start_iso and end_iso and start_iso <= today_local_iso <= end_iso):
        return 0
    try:
        return int(business.get("weekly_counter", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def _send_weekly_summary(business: dict, week_start: str, week_end: str, now: datetime | None = None) -> None:
    if now is None:
        now = _now()
    biz_id = business["id"]
    biz_name = business.get("name") or "Your business"
    tz = _biz_tz(business)

    all_bookings = db.list_bookings(biz_id, limit=1000)
    all_customers = db.list_customers(biz_id, limit=1000)

    # Week's bookings (received and still active)
    week_bookings = [
        b for b in all_bookings
        if _in_range(b.get("datetime") or b.get("date"), week_start, week_end)
        and b.get("status") != "cancelled"
    ]

    cancelled_week = [
        b for b in all_bookings
        if b.get("status") == "cancelled"
        and _in_range(b.get("updatedAt") or b.get("cancelledAt") or b.get("datetime"), week_start, week_end)
    ]

    new_customers = [
        c for c in all_customers
        if _in_range(c.get("createdAt"), week_start, week_end)
    ]

    handled_set = set()
    for c in new_customers: handled_set.add(c.get("phone") or c.get("id"))
    for b in week_bookings: handled_set.add(b.get("customerPhone"))
    for b in cancelled_week: handled_set.add(b.get("customerPhone"))
    handled_set.discard(None)
    customers_handled = len(handled_set)

    bookings_made = len(week_bookings)
    # Total bookings received this week = active + cancelled (anything booked).
    total_bookings_received = bookings_made + len(cancelled_week)
    owner_name = str(business.get("ownerName") or business.get("owner_name") or "there").split()[0]

    now_local_iso = now.astimezone(tz).strftime("%Y-%m-%d")
    ai_calls_week = _calls_week(business, now_local_iso)

    has_activity = bool(customers_handled or total_bookings_received or ai_calls_week)

    # Always surface the headline metrics together when there's any activity, so
    # the owner can compare week-over-week. Calls and bookings render as 0 if
    # absent (rather than being hidden) — this is what the owner is tracking.
    stats_lines: list[str] = []
    if has_activity:
        stats_lines.append(f"💬 {customers_handled} customers handled")
        stats_lines.append(
            f"📅 *Bookings this week: {bookings_made}* "
            f"({total_bookings_received} received incl. cancellations)"
        )
        stats_lines.append(f"📞 *Calls handled this week: {ai_calls_week}*")
        stats_lines.append("")

    if not has_activity:
        lines = [
            f"🌟 A calm week, {owner_name} — nothing slipped, nothing missed 😌",
            "I was watching your WhatsApp every hour of every day. When your customers come, I'll be ready 🙌"
        ]
    elif customers_handled <= 5 and bookings_made <= 5: # arbitrary low totals for week
        lines = [f"🌟 Your week, {owner_name}:"] + stats_lines + [
            "Small steps build big things. Every customer I caught is one you didn't lose 💪",
            "This is just the beginning 🚀"
        ]
    else:
        lines = [f"🌟 Your week, {owner_name}:"] + stats_lines + [
            "That's a whole week you didn't have to chase anyone. I held it all together while you ran your business 💪",
            "Imagine where you'll be in three months 🚀"
        ]

    lines += [
        "",
        "You were living your life. I had your back 💪",
    ]

    msg = "\n".join(lines)
    logger.info(
        "[AUTOMATION:WEEKLY_SUMMARY] sending to owner of biz %s (%s) | bookings=%d calls=%d",
        biz_id, biz_name, bookings_made, ai_calls_week,
    )
    await send_to_owner(business, msg)
