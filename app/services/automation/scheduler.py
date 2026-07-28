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
from app.services.automation.weekly_summary import run_weekly_summary_for_all_businesses
from app.services.automation.customer_intelligence import run_customer_intelligence_sweep
from app.services.automation.referral_automation import run_referral_invite_sweep, run_referral_discount_expiry_sweep
from app.services.automation.trial_expiry_automation import run_trial_expiry_sweep
from app.services.automation.day2_checkin import run_day2_checkin_sweep
from app.services.automation.onboarding_followup import run_onboarding_followup_sweep
from app.services.automation.demo_followup import run_demo_aboutus_sweep
from app.services.automation.kb_expiry import run_kb_expiry_sweep
from app.services.csat_service import sweep_all as run_csat_sweep
from app.config import settings as _settings

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


async def _job_weekly_summary() -> None:
    try:
        await run_weekly_summary_for_all_businesses()
    except Exception as exc:
        logger.exception("[Scheduler] weekly summary crashed: %s", exc)


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


async def _job_day2_checkin() -> None:
    try:
        await run_day2_checkin_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] day-2 check-in sweep crashed: %s", exc)


async def _job_onboarding_followup() -> None:
    try:
        await run_onboarding_followup_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] onboarding follow-up sweep crashed: %s", exc)


async def _job_demo_aboutus() -> None:
    try:
        await run_demo_aboutus_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] demo about-us sweep crashed: %s", exc)


async def _job_kb_expiry() -> None:
    try:
        await run_kb_expiry_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] KB expiry sweep crashed: %s", exc)


async def _job_csat_sweep() -> None:
    try:
        await run_csat_sweep()
    except Exception as exc:
        logger.exception("[Scheduler] CSAT sweep crashed: %s", exc)

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

    # Daily owner summary — run every hour to check business local times (8:00 AM local)
    _scheduler.add_job(
        _job_daily_summary,
        trigger=CronTrigger(minute=0, timezone="UTC"),
        id="daily_summary",
        name="Daily owner summary (hourly timezone check)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Weekly owner summary — run every hour to check business local times (Sunday 7:00 PM local)
    _scheduler.add_job(
        _job_weekly_summary,
        trigger=CronTrigger(minute=0, timezone="UTC"),
        id="weekly_summary",
        name="Weekly owner summary (hourly timezone check)",
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

    # Day-2 trust check-in — every day at 10:00 UTC. One-shot Sofia message
    # 2 days after trial start repeating the "disconnect anytime" reminder
    # (client trust spec item 12 — people who feel free stay).
    _scheduler.add_job(
        _job_day2_checkin,
        trigger=CronTrigger(hour=10, minute=0, timezone="UTC"),
        id="day2_checkin_sweep",
        name="Day-2 trust check-in (one-shot, 2 days after trial start)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Onboarding drop-off follow-up sweep — every 15 minutes. The 1 h / 18 h
    # silence thresholds are enforced inside the sweep; running every 15 min just
    # bounds how soon after a threshold the nudge actually goes out. Capped at two
    # nudges per session (see app/services/automation/onboarding_followup.py).
    _scheduler.add_job(
        _job_onboarding_followup,
        trigger=IntervalTrigger(minutes=15),
        id="onboarding_followup_sweep",
        name="Onboarding drop-off follow-up sweep (1 h + 18 h nudges)",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )

    # End-of-demo "About us" idle sweep — every 5 minutes. The DEMO_ABOUTUS_IDLE_MIN
    # (default 10 min) threshold is enforced inside the sweep; running every 5 min
    # just bounds how soon after that threshold the message goes out. At-most-once
    # per demo session (see app/services/automation/demo_followup.py).
    _scheduler.add_job(
        _job_demo_aboutus,
        trigger=IntervalTrigger(minutes=5),
        id="demo_aboutus_sweep",
        name="End-of-demo About-us idle sweep (10-min after pairing offer)",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )

    # KB expiry sweep — every day at 04:00 UTC. Flips pending KB entries past
    # their TTL (default 7 days) from pending_review → expired so the dedup
    # window resets and the next customer asking the same question creates a
    # fresh prompt.  Off-peak slot to avoid contention with the summary jobs.
    _scheduler.add_job(
        _job_kb_expiry,
        trigger=CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="kb_expiry_sweep",
        name="Per-SMB knowledge-base expiry sweep",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # CSAT prompt sweep — every CSAT_SWEEP_INTERVAL_MINUTES (default 5 min).
    # Finds conversations that have been idle for CSAT_IDLE_MINUTES after the
    # last AI reply and sends a "rate 1-5" WhatsApp prompt.
    if _settings.CSAT_ENABLED:
        _scheduler.add_job(
            _job_csat_sweep,
            trigger=IntervalTrigger(minutes=_settings.CSAT_SWEEP_INTERVAL_MINUTES),
            id="csat_sweep",
            name=f"CSAT prompt sweep (idle ≥ {_settings.CSAT_IDLE_MINUTES} min)",
            replace_existing=True,
            misfire_grace_time=120,
            max_instances=1,
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
