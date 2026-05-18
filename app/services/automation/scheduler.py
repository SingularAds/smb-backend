"""Automation Scheduler

Uses APScheduler (AsyncIOScheduler) to run background jobs
without blocking the FastAPI event loop.

Jobs:
  • Every 30 min  → reminder sweep (24h + 2h booking reminders)
  • Every 30 min  → visit confirmation sweep (2 h post-booking YES/NO ask)
  • Every 15 min  → referral invite sweep (90 min post-visit invite)
  • Daily 08:00 UTC → daily owner summary
  • Daily 02:00 UTC → customer intelligence (VIP / inactive)

Start via start_scheduler() in the FastAPI lifespan handler.
"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.services.automation.booking_automation import run_reminder_sweep, run_visit_confirmation_sweep
from app.services.automation.daily_summary import run_daily_summary_for_all_businesses
from app.services.automation.customer_intelligence import run_customer_intelligence_sweep
from app.services.automation.referral_automation import run_referral_invite_sweep, run_referral_discount_expiry_sweep
from app.services.automation.trial_expiry_automation import run_trial_expiry_sweep

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ── Job wrappers (catch exceptions so scheduler keeps running) ────────────────

async def _job_reminders() -> None:
    try:
        await run_reminder_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] reminder sweep crashed: %s", exc)


async def _job_visit_confirmation() -> None:
    try:
        await run_visit_confirmation_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] visit confirmation sweep crashed: %s", exc)


async def _job_daily_summary() -> None:
    try:
        await run_daily_summary_for_all_businesses()
    except Exception as exc:
        logger.exception("[Scheduler] daily summary crashed: %s", exc)


async def _job_customer_intel() -> None:
    try:
        await run_customer_intelligence_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] customer intel crashed: %s", exc)


async def _job_referral_invites() -> None:
    try:
        await run_referral_invite_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] referral invite sweep crashed: %s", exc)


async def _job_referral_expiry() -> None:
    try:
        await run_referral_discount_expiry_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] referral expiry sweep crashed: %s", exc)

async def _job_trial_expiry() -> None:
    try:
        await run_trial_expiry_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] trial expiry sweep crashed: %s", exc)

# ── Public API ────────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    """Start the background scheduler. Call once from the FastAPI lifespan."""
    global _scheduler
    if _scheduler and _scheduler.running:
        logger.warning("[Scheduler] already running — skipping start")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # Reminder sweep — every 30 minutes
    _scheduler.add_job(
        _job_reminders,
        trigger=IntervalTrigger(minutes=30),
        id="reminder_sweep",
        name="Booking reminder sweep (24h + 2h)",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )

    # No-show sweep — every 30 minutes (offset by 15 min to stagger load)
    _scheduler.add_job(
        _job_visit_confirmation,
        trigger=IntervalTrigger(minutes=30, start_date="2000-01-01 00:15:00"),
        id="visit_confirmation_sweep",
        name="Visit confirmation sweep (2 h post-booking YES/NO)",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )

    # Daily owner summary — every day at 08:00 UTC
    _scheduler.add_job(
        _job_daily_summary,
        trigger=CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="daily_summary",
        name="Daily owner summary",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Customer intelligence — every day at 02:00 UTC
    _scheduler.add_job(
        _job_customer_intel,
        trigger=CronTrigger(hour=2, minute=0, timezone="UTC"),
        id="customer_intel",
        name="Customer intelligence sweep (VIP/inactive)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Referral invite sweep — every 15 minutes (90-min delay handled inside)
    _scheduler.add_job(
        _job_referral_invites,
        trigger=IntervalTrigger(minutes=15),
        id="referral_invite_sweep",
        name="Referral invite sweep (90-min post-visit)",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )

    # Referral discount expiry sweep — every day at 03:00 UTC
    _scheduler.add_job(
        _job_referral_expiry,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="referral_discount_expiry",
        name="Referral discount expiry sweep",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Trial expiry reminder sweep — every day at 09:00 UTC
    # Sends WhatsApp + email reminders to owners whose trial has expired on
    # day 0, 1, 3, and 7 after expiry.  Stops automatically once they subscribe.
    _scheduler.add_job(
        _job_trial_expiry,
        trigger=CronTrigger(hour=9, minute=0, timezone="UTC"),
        id="trial_expiry_sweep",
        name="Trial expiry reminder sweep (day 0/1/3/7)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    _scheduler.start()
    logger.info("[Scheduler] started — %d jobs registered", len(_scheduler.get_jobs()))
    print("[SCHEDULER] ✅ started with jobs:")
    for job in _scheduler.get_jobs():
        print(f"  • {job.id} ({job.name}) next run: {job.next_run_time}")


def stop_scheduler() -> None:
    """Gracefully stop the scheduler. Call from the FastAPI lifespan shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[Scheduler] stopped")
        print("[SCHEDULER] 🛑 stopped")
