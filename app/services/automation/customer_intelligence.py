"""Customer Intelligence — Step 4.

VIP Detection:
  • A customer who has >= VIP_THRESHOLD confirmed bookings is automatically
    flagged as VIP (flag added to their `flags` list in Firestore).

Inactive Detection:
  • A customer whose last confirmed booking was more than INACTIVE_DAYS ago
    is flagged as `inactive`.

Both sweeps are designed to run nightly (or on-demand).
No WhatsApp messages are sent here — flags are stored and can be used
by campaign triggers later.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from app import firestore as db

logger = logging.getLogger(__name__)

VIP_THRESHOLD = 5        # visits to become VIP
INACTIVE_DAYS = 30       # days of silence → inactive


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def run_customer_intelligence_sweep() -> None:
    """Sweep all active businesses and update VIP / inactive customer flags."""
    now = _now()
    logger.info("[AUTOMATION:CUSTOMER_INTEL] starting at %s", now.isoformat())
    businesses = db.list_active_businesses()

    total_vip_flagged = 0
    total_inactive_flagged = 0

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id:
            continue

        try:
            v, i = await _process_business(biz_id, now)
            total_vip_flagged += v
            total_inactive_flagged += i
        except Exception as exc:
            logger.exception("[Automation] Customer intel sweep failed for biz %s: %s", biz_id, exc)
            logger.error("[AUTOMATION:CUSTOMER_INTEL] error for biz %s: %s", biz_id, exc)

    logger.info(
        "[AUTOMATION:CUSTOMER_INTEL] done — %d newly VIP, %d newly inactive across %d businesses",
        total_vip_flagged,
        total_inactive_flagged,
        len(businesses),
    )


async def _process_business(biz_id: str, now: datetime) -> tuple[int, int]:
    """Return (vip_count, inactive_count) for the business."""
    bookings = db.list_bookings(biz_id, limit=500)
    customers = db.list_customers(biz_id, limit=500)

    # Build per-customer stats from bookings
    from collections import defaultdict
    stats: dict[str, dict] = defaultdict(lambda: {"confirmed_count": 0, "last_booking": None})

    for b in bookings:
        phone = b.get("customerPhone", "")
        if not phone:
            continue
        status = b.get("status", "")
        if status == "confirmed":
            stats[phone]["confirmed_count"] += 1
            dt = _parse_dt(b.get("datetime") or b.get("date"))
            if dt:
                prev = stats[phone]["last_booking"]
                if prev is None or dt > prev:
                    stats[phone]["last_booking"] = dt

    inactive_cutoff = now - timedelta(days=INACTIVE_DAYS)
    vip_flagged = 0
    inactive_flagged = 0

    for customer in customers:
        phone = customer.get("phone") or customer.get("id", "")
        if not phone:
            continue

        current_flags: list = list(customer.get("flags") or [])
        s = stats.get(phone, {})
        confirmed_count = s.get("confirmed_count", 0)
        # Also count `totalVisits` stored on the customer record
        total_visits = max(confirmed_count, customer.get("totalVisits") or 0)
        last_booking = s.get("last_booking")
        updates: dict = {}

        # ── VIP detection ────────────────────────────────────────────────
        if total_visits >= VIP_THRESHOLD and "vip" not in current_flags:
            current_flags.append("vip")
            updates["flags"] = current_flags
            updates["vipSince"] = now.isoformat()
            updates["totalVisits"] = total_visits
            vip_flagged += 1
            logger.info(
                "[AUTOMATION:CUSTOMER_INTEL] ⭐ VIP flagged phone=%s biz=%s visits=%s",
                phone,
                biz_id,
                total_visits,
            )

        # ── Inactive detection ───────────────────────────────────────────
        # Use booking-derived last_booking first; fall back to customer.lastVisit
        last_activity = last_booking
        if not last_activity:
            last_activity = _parse_dt(customer.get("lastVisit") or customer.get("last_visit"))
        if last_activity and last_activity < inactive_cutoff and "inactive" not in current_flags:
            current_flags.append("inactive")
            updates["flags"] = current_flags
            updates["inactiveSince"] = now.isoformat()
            inactive_flagged += 1
            logger.info(
                "[AUTOMATION:CUSTOMER_INTEL] 💤 inactive flagged phone=%s biz=%s last_booking=%s",
                phone,
                biz_id,
                last_activity.date(),
            )

        # Remove inactive flag if they've had a recent booking
        if last_activity and last_activity >= inactive_cutoff and "inactive" in current_flags:
            current_flags = [f for f in current_flags if f != "inactive"]
            updates["flags"] = current_flags

        if updates:
            db.upsert_customer(biz_id, phone, updates)

    return vip_flagged, inactive_flagged
