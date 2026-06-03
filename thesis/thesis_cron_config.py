"""
THESIS Cron Job Configuration.

Integrates health monitoring, status reporting, and trading validation
into APScheduler with the following schedule:

  TRADING HOURS (9:30 AM - 4:00 PM ET):
    - 9:45 AM ET: Daily trading ability validation
    - Every 30 minutes: System health check & self-healing
   - 12:15 PM ET: Mid-session status report
    - 4:15 PM ET: Post-close summary

  NON-TRADING HOURS:
    - Health checks disabled (not needed)
    - Status reports disabled

This module is imported into thesis/main.py and called during lifespan startup.
"""

from __future__ import annotations

import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
from zoneinfo import ZoneInfo

from thesis_health_monitor import ThesisHealthMonitor
from thesis_status_reporter import ThesisStatusReporter
from thesis_trading_validator import ThesisTradingValidator

logger = logging.getLogger("thesis_cron_config")

_ET = ZoneInfo("America/New_York")


def configure_monitoring_jobs(
    scheduler: AsyncIOScheduler,
    alpaca_key: str,
    alpaca_secret: str,
    telegram_token: str = "",
    thesis_service_url: str = "http://localhost:8060",
) -> bool:
    """
    Configure all monitoring, status reporting, and validation jobs.

    Args:
        scheduler: APScheduler AsyncIOScheduler instance.
        alpaca_key: Alpaca API key.
        alpaca_secret: Alpaca API secret.
        telegram_token: Axiom bot token for messaging Ahmed.
        thesis_service_url: Thesis service URL.

    Returns:
        True if all jobs configured successfully, False otherwise.
    """
    try:
        logger.info("Configuring monitoring jobs...")

        # Initialize clients
        health_monitor = ThesisHealthMonitor(
            alpaca_key=alpaca_key,
            alpaca_secret=alpaca_secret,
            telegram_token=telegram_token,
            thesis_service_url=thesis_service_url,
        )

        status_reporter = ThesisStatusReporter(
            alpaca_key=alpaca_key,
            alpaca_secret=alpaca_secret,
            telegram_token=telegram_token,
            thesis_service_url=thesis_service_url,
        )

        trading_validator = ThesisTradingValidator(
            alpaca_key=alpaca_key,
            alpaca_secret=alpaca_secret,
            telegram_token=telegram_token,
        )

        # Job 1: 9:45 AM — Daily trading ability validation
        scheduler.add_job(
            _job_trading_validation,
            CronTrigger(hour=9, minute=45, timezone=_ET),
            id="thesis_trading_validation",
            name="Daily Trading Ability Validation (9:45 AM)",
            args=[trading_validator],
            misfire_grace_time=60,
        )
        logger.info("✓ Job scheduled: Daily trading ability validation (9:45 AM ET)")

        # Job 2: Every 30 minutes during trading hours (9:30 AM - 4:00 PM)
        scheduler.add_job(
            _job_health_check,
            IntervalTrigger(minutes=30),
            id="thesis_health_check",
            name="Health Check & Self-Healing (every 30 min)",
            args=[health_monitor],
            misfire_grace_time=60,
        )
        logger.info("✓ Job scheduled: Health check (every 30 min)")

        # Job 3: 12:15 PM — Mid-session status update
        scheduler.add_job(
            _job_mid_session_update,
            CronTrigger(hour=12, minute=15, timezone=_ET),
            id="thesis_mid_session_update",
            name="Mid-Session Status Update (12:15 PM)",
            args=[status_reporter],
            misfire_grace_time=60,
        )
        logger.info("✓ Job scheduled: Mid-session update (12:15 PM ET)")

        # Job 4: 4:15 PM — Post-close summary
        scheduler.add_job(
            _job_post_close_summary,
            CronTrigger(hour=16, minute=15, timezone=_ET),
            id="thesis_post_close_summary",
            name="Post-Close Summary (4:15 PM)",
            args=[status_reporter],
            misfire_grace_time=60,
        )
        logger.info("✓ Job scheduled: Post-close summary (4:15 PM ET)")

        logger.info("All monitoring jobs configured successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to configure monitoring jobs: {str(e)}", exc_info=True)
        return False


async def _job_trading_validation(validator: ThesisTradingValidator):
    """Job handler for daily trading validation."""
    logger.info("[JOB] Running daily trading ability validation...")
    try:
        result = await validator.run_daily_validation()
        logger.info(f"[JOB] Trading validation complete: {result['status'].upper()}")
    except Exception as e:
        logger.error(f"[JOB] Trading validation failed: {str(e)}", exc_info=True)


async def _job_health_check(monitor: ThesisHealthMonitor):
    """Job handler for health checks."""
    logger.info("[JOB] Running health check...")
    try:
        result = await monitor.run_health_check()
        logger.info(f"[JOB] Health check complete: {result['overall_status']}")
        if result.get("escalation_triggered"):
            logger.warning("[JOB] Escalation triggered — Axiom will be notified")
    except Exception as e:
        logger.error(f"[JOB] Health check failed: {str(e)}", exc_info=True)


async def _job_mid_session_update(reporter: ThesisStatusReporter):
    """Job handler for mid-session status update."""
    logger.info("[JOB] Sending mid-session status update...")
    try:
        await reporter.send_mid_session_update()
        logger.info("[JOB] Mid-session update sent")
    except Exception as e:
        logger.error(f"[JOB] Mid-session update failed: {str(e)}", exc_info=True)


async def _job_post_close_summary(reporter: ThesisStatusReporter):
    """Job handler for post-close summary."""
    logger.info("[JOB] Sending post-close summary...")
    try:
        await reporter.send_post_close_summary()
        logger.info("[JOB] Post-close summary sent")
    except Exception as e:
        logger.error(f"[JOB] Post-close summary failed: {str(e)}", exc_info=True)


# Integration helper for main.py
def apply_monitoring_configuration(
    scheduler: AsyncIOScheduler,
    alpaca_key: str = "",
    alpaca_secret: str = "",
    telegram_token: str = "",
) -> bool:
    """
    Call this from main.py lifespan to configure all monitoring jobs.

    Example:
        from thesis_cron_config import apply_monitoring_configuration

        @app.on_event("startup")
        async def startup_event():
            success = apply_monitoring_configuration(
                scheduler=_scheduler,
                alpaca_key=os.getenv("ALPACA_KEY"),
                alpaca_secret=os.getenv("ALPACA_SECRET"),
                telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            )
            if not success:
                logger.error("Failed to configure monitoring jobs")
    """
    return configure_monitoring_jobs(
        scheduler,
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret,
        telegram_token=telegram_token,
    )
