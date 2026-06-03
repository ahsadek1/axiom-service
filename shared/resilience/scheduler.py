"""
scheduler.py — Stateless APScheduler scan wrapper for all Nexus agents.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
Status: Zero conflicts — replaces hand-rolled watchdog threads.

Rule: Every recurring scan cycle goes through build_scanner().
- Service never crashes on scan exception — the wrapper catches and alerts.
- max_instances=1 prevents intra-process job overlap.
- Cross-process safety handled by idempotent_insert in db.py.
- Health report updated on every cycle (last_cycle, last_skip_rate).
- Skip threshold breach triggers a CRITICAL alert to Ahmed automatically.

Usage:
    scheduler = build_scanner(
        scan_fn=my_scan,            # async callable returning ScanResult
        interval_minutes=5,
        job_id="cipher_scanner",
        health=health_report,       # shared HealthReport instance
        fn_kwargs={"db_path": DB_PATH},
    )
    scheduler.start()
    # In shutdown handler: scheduler.shutdown(wait=False)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .health import HealthReport

logger = logging.getLogger("nexus.scheduler")


def build_scanner(
    scan_fn: Callable,
    interval_minutes: int,
    job_id: str,
    health: HealthReport,
    fn_args: Optional[list] = None,
    fn_kwargs: Optional[dict] = None,
    misfire_grace_seconds: int = 60,
    skip_threshold: float = 0.80,
) -> AsyncIOScheduler:
    """
    Build and configure an AsyncIOScheduler that runs *scan_fn* on an interval.

    The scheduler is returned but NOT started — caller calls scheduler.start()
    after any async setup is complete.

    Args:
        scan_fn               — async callable returning ScanResult
        interval_minutes      — how often to run (minutes)
        job_id                — unique job ID string (used in logs and alert keys)
        health                — shared HealthReport instance to update each cycle
        fn_args               — positional args forwarded to scan_fn
        fn_kwargs             — keyword args forwarded to scan_fn
        misfire_grace_seconds — APScheduler misfire window (default 60s)
        skip_threshold        — alert threshold for skip rate (default 0.80 = 80%)

    Returns:
        Configured AsyncIOScheduler (not yet started).

    Raises:
        Nothing — all scan exceptions are caught and alerted.

    Example:
        scheduler = build_scanner(
            scan_fn=run_scan,
            interval_minutes=5,
            job_id="axiom_scan",
            health=health,
            fn_kwargs={"pool": tickers, "db_path": DB_PATH},
        )
        scheduler.start()
    """
    # Late import avoids circular dependency (alerts imports nothing from here)
    from .alerts import alert_ahmed

    scheduler = AsyncIOScheduler()

    async def _wrapped() -> None:
        try:
            # C4 fix: handle both async and sync scan functions.
            # A sync scan_fn passed to 'await' raises TypeError inside the
            # exception handler and gets silently logged as a crash.
            if inspect.iscoroutinefunction(scan_fn):
                result = await scan_fn(*(fn_args or []), **(fn_kwargs or {}))
            else:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: scan_fn(*(fn_args or []), **(fn_kwargs or {}))
                )

            # Update health report.
            # C6 fix: use result.pool_size if set by scan_fn (authoritative).
            # Fall back to verdicts+skips only if scan_fn didn't set pool_size
            # (backward compat — avoids undercounting pre-loop filtered tickers).
            health.last_cycle = datetime.utcnow()
            pool_size = result.pool_size if result.pool_size > 0 else (len(result.verdicts) + result.total_skips)
            health.last_skip_rate = result.skip_rate(pool_size)

            logger.info(
                "[%s] cycle complete — verdicts=%d skips=%d skip_rate=%.1f%%",
                job_id,
                len(result.verdicts),
                result.total_skips,
                result.skip_rate(pool_size) * 100,
            )

            # Alert if data outage threshold breached
            if result.check_skip_threshold(pool_size, threshold=skip_threshold):
                skip_pct = int(result.skip_rate(pool_size) * 100)
                alert_ahmed(
                    f"*{job_id}*: {skip_pct}% of tickers skipped "
                    f"({result.total_skips}/{pool_size}) — likely data outage.\n"
                    f"Reasons: {dict(result.skip_reasons)}\n"
                    f"Sample tickers: {list(result.skipped_tickers.keys())[:10]}",
                    key=f"{job_id}_skip_threshold",
                    severity="CRITICAL",
                )

        except Exception as exc:
            logger.exception("[%s] scan cycle crashed: %s", job_id, exc)
            alert_ahmed(
                f"*{job_id}* scan cycle crashed:\n`{exc}`",
                key=f"{job_id}_cycle_crash",
                severity="CRITICAL",
            )

    scheduler.add_job(
        _wrapped,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id=job_id,
        max_instances=1,                       # no intra-process overlap
        misfire_grace_time=misfire_grace_seconds,
        replace_existing=True,
    )

    return scheduler
