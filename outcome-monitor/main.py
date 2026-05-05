"""
main.py — Outcome Monitor entry point.

Runs every 30 minutes during NYSE market hours (9:30–16:00 ET, Mon–Fri).
Zero trades in a cycle triggers auto-diagnosis and CHRONICLE logging.
Escalates to SOVEREIGN for CRITICAL/WARN conditions.

This service is read-only — it never modifies Nexus service state,
never triggers trades, and never restarts other services.
"""

import json
import logging
import sys
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Dict
import zoneinfo

from apscheduler.schedulers.blocking import BlockingScheduler

from chronicler import write_to_chronicle
from collector import collect_all_services, collect_alpaca
from config import STATE_FILE
from diagnoser import diagnose
from escalator import escalate_to_sovereign
from models import CycleResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("outcome-monitor")

# ── Market hours configuration ────────────────────────────────────────────────
ET = zoneinfo.ZoneInfo("America/New_York")
MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(16, 0)
MARKET_DAYS = {0, 1, 2, 3, 4}  # Monday–Friday

# NYSE holidays 2026
NYSE_HOLIDAYS_2026 = [
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
]


def is_market_open() -> bool:
    """
    Return True if NYSE is currently open for regular trading.

    Returns:
        True between 9:30–16:00 ET on non-holiday weekdays, False otherwise.
    """
    now = datetime.now(tz=ET)
    if now.weekday() not in MARKET_DAYS:
        return False
    if now.date() in NYSE_HOLIDAYS_2026:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def load_state() -> Dict[str, Any]:
    """
    Load persistent cycle state from the state file.

    Returns:
        State dict with consecutive_dry_cycles, last_cycle_trades_total,
        and last_cycle_ts. Returns safe defaults if file is missing or corrupt.
    """
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except Exception:
        return {
            "consecutive_dry_cycles": 0,
            "last_cycle_trades_total": 0,
            "last_cycle_ts": None,
        }


def save_state(state: Dict[str, Any]) -> None:
    """
    Persist cycle state to the state file for delta tracking across cycles.

    Args:
        state: Dict with consecutive_dry_cycles, last_cycle_trades_total,
               and last_cycle_ts fields.
    """
    try:
        Path(STATE_FILE).write_text(json.dumps(state))
    except Exception as e:
        logger.error("State save failed: %s", e)


def run_cycle() -> None:
    """
    Execute one outcome monitor cycle.

    Exits immediately (no logging) if outside market hours.
    Collects service snapshots, calculates trade delta, runs diagnosis,
    writes to CHRONICLE, and escalates to SOVEREIGN if warranted.
    """
    if not is_market_open():
        return

    logger.info("=== Outcome Monitor cycle starting ===")
    state = load_state()

    # Collect data from all services and Alpaca
    services = collect_all_services()
    alpaca = collect_alpaca()

    # Calculate trades since last cycle
    alpha_exec = services.get("alpha_execution")
    prime_exec = services.get("prime_execution")
    alpha_trades = (
        alpha_exec.data.get("trades_today", 0)
        if alpha_exec and alpha_exec.status == "UP"
        else 0
    )
    prime_trades = (
        prime_exec.data.get("trades_today", 0)
        if prime_exec and prime_exec.status == "UP"
        else 0
    )
    current_total = alpha_trades + prime_trades
    last_total = state.get("last_cycle_trades_total", 0)
    trades_since = max(0, current_total - last_total)

    # Track consecutive dry cycles
    if trades_since == 0:
        state["consecutive_dry_cycles"] = state.get("consecutive_dry_cycles", 0) + 1
    else:
        state["consecutive_dry_cycles"] = 0

    diagnosis = diagnose(services, alpaca, trades_since, state["consecutive_dry_cycles"])

    cycle_ts = datetime.now(timezone.utc).isoformat()
    result = CycleResult(
        cycle_ts=cycle_ts,
        market_open=True,
        trades_since_last_cycle=trades_since,
        diagnosis=diagnosis,
        services=services,
        alpaca=alpaca,
        escalated=diagnosis.escalate,
    )

    write_to_chronicle(result)

    if diagnosis.escalate:
        escalate_to_sovereign(result)
        logger.warning(
            "ESCALATED: %s [%s]", diagnosis.diagnosis, diagnosis.severity
        )
    else:
        logger.info(
            "Cycle complete: %s [%s]", diagnosis.diagnosis, diagnosis.severity
        )

    state["last_cycle_trades_total"] = current_total
    state["last_cycle_ts"] = cycle_ts
    save_state(state)


if __name__ == "__main__":
    logger.info("Outcome Monitor starting up")
    scheduler = BlockingScheduler(timezone="America/New_York")
    scheduler.add_job(run_cycle, "interval", minutes=30, misfire_grace_time=300)
    logger.info("Running initial cycle immediately")
    run_cycle()
    logger.info("Scheduler running — every 30 min during market hours")
    scheduler.start()
