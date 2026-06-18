"""KB Expiry Sweeper.

Runs once a day. For every active business, transitions pending KB entries
past their `expiresAt` timestamp from `pending_review` → `expired`. This
keeps the dedup window honest: a customer asking the same question after the
TTL window can re-create a fresh capture (and trigger a new owner prompt)
rather than being silently dropped against an expired-but-not-yet-cleaned
entry.

Wired into `automation.scheduler` next to the other daily sweeps. Intentional
design choices:

  • Idempotent — running twice is harmless (`expire_stale_pending` only
    moves entries where `expiresAt <= now`).
  • Per-business loop is wrapped so a single business failure doesn't abort
    the sweep.
  • No retries inside the loop: the next day's sweep is the retry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import firestore as db
from app.services import knowledge_base_service as kb

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def run_kb_expiry_sweep(now: datetime | None = None) -> None:
    """Expire stale pending KB entries across every active business.

    Async to match the other sweeper signatures even though the underlying
    work is synchronous Firestore I/O (this lets us slot it into the
    AsyncIOScheduler without a wrapper).
    """
    if now is None:
        now = _now_utc()
    logger.info("[AUTOMATION:KB_EXPIRY] starting at %s", now.isoformat())

    businesses = db.list_active_businesses()
    total_expired = 0
    failed_businesses: list[str] = []

    for business in businesses:
        biz_id = business.get("id", "")
        if not biz_id:
            continue
        try:
            expired = kb.expire_stale_pending(biz_id, now=now)
            total_expired += expired
        except Exception as exc:
            failed_businesses.append(biz_id)
            logger.exception(
                "[AUTOMATION:KB_EXPIRY] error for biz %s: %s", biz_id, exc,
            )

    logger.info(
        "[AUTOMATION:KB_EXPIRY] done — %d entries expired across %d businesses "
        "(%d failures)",
        total_expired, len(businesses), len(failed_businesses),
    )
