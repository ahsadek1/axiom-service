"""
FIX #3: Daily Reconciliation Daemon

Runs every 4 hours to reconcile closed positions and release capital retroactively.

Problem being solved:
- When exit_monitor closes a position, it calls release_capital() via HTTP
- If Capital Router is down, release fails (HTTP timeout)
- Capital remains allocated forever
- New position entries are blocked

Solution:
- Every 4 hours, query ALL positions closed in the last 24 hours
- For each closed position, check Capital Router for active allocations
- If allocation still exists, release it
- Log all reconciliation events for audit trail

Deployment: Add to systemd timer or cron
  cron: 0 */4 * * * /usr/bin/python3 /Users/ahmedsadek/nexus/alpha-execution/daily_reconciler.py
"""

import logging
import sys
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Add shared modules
sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
sys.path.insert(0, "/Users/ahmedsadek/nexus/alpha-execution")

from database import get_conn
from alert_client import send_alert

# Configuration
ALPHA_EXEC_DB = "/Users/ahmedsadek/nexus/data/alpha-execution.db"
CAPITAL_ROUTER_URL = "http://localhost:9100"
RECONCILIATION_LOOKBACK_HOURS = 24
LOG_FILE = Path("/Users/ahmedsadek/nexus/logs/daily_reconciler.log")

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("alpha_exec.daily_reconciler")


async def get_closed_positions_last_n_hours(
    db_path: str, hours: int = RECONCILIATION_LOOKBACK_HOURS
) -> list[dict]:
    """
    Query all positions closed in the last N hours.

    Returns:
        List of position dicts with id, ticker, arena, closed_at
    """
    try:
        cutoff_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with get_conn(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, arena, closed_at, pnl_pct, pnl_usd
                FROM positions
                WHERE status='closed' AND closed_at >= ?
                ORDER BY closed_at DESC
                """,
                (cutoff_time,),
            ).fetchall()
        positions = [dict(r) for r in rows]
        logger.info(f"Found {len(positions)} positions closed in last {hours} hours")
        return positions
    except Exception as e:
        logger.error(f"Failed to query closed positions: {e}")
        return []


async def check_capital_allocation(
    capital_router_url: str, position_id: int
) -> bool:
    """
    Check if a capital allocation still exists for this position.

    Returns:
        True if allocation is still active, False if not found/released.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            # Query capital router for allocation status
            # Note: This is a custom endpoint — may need to be added to capital-router
            resp = await client.get(
                f"{capital_router_url}/allocations/{position_id}",
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("status") == "locked"
            elif resp.status_code == 404:
                # Allocation already released
                return False
            else:
                logger.warning(
                    f"Unexpected status {resp.status_code} checking allocation for position {position_id}"
                )
                return False
    except Exception as e:
        logger.warning(
            f"Failed to check allocation for position {position_id}: {e} — assuming unreleased"
        )
        # Conservative: assume it's still allocated if we can't reach capital router
        return True


async def release_capital_for_position(
    capital_router_url: str, position_id: int, arena: str
) -> bool:
    """
    Release capital allocation for a position.

    Returns:
        True if release succeeded, False otherwise.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{capital_router_url}/release-allocation/{position_id}",
                json={"arena": arena, "position_id": position_id},
            )
            if resp.status_code == 200:
                logger.info(f"Released capital for position {position_id} (arena={arena})")
                return True
            else:
                logger.warning(
                    f"Capital release returned {resp.status_code} for position {position_id}"
                )
                return False
    except Exception as e:
        logger.error(f"Failed to release capital for position {position_id}: {e}")
        return False


async def reconcile_closed_positions(
    db_path: str, capital_router_url: str, lookback_hours: int = RECONCILIATION_LOOKBACK_HOURS
) -> dict:
    """
    FIX #3: Main reconciliation loop.

    Process all positions closed in the last N hours and release any stranded capital.

    Returns:
        Summary dict with counts of positions checked, releases attempted, etc.
    """
    logger.info(f"Starting daily reconciliation (lookback={lookback_hours}h)")

    # Get all closed positions from last N hours
    closed_positions = await get_closed_positions_last_n_hours(db_path, lookback_hours)

    if not closed_positions:
        logger.info("No closed positions found in lookback window")
        return {
            "positions_checked": 0,
            "allocations_found": 0,
            "releases_attempted": 0,
            "releases_succeeded": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # Check and release each allocation
    allocations_found = 0
    releases_attempted = 0
    releases_succeeded = 0

    for pos in closed_positions:
        pos_id = pos["id"]
        arena = pos.get("arena", "alpha")

        # Check if allocation still exists
        if await check_capital_allocation(capital_router_url, pos_id):
            allocations_found += 1
            logger.warning(
                f"Position {pos_id} ({pos['ticker']}) closed but capital still allocated — releasing"
            )

            # Attempt release
            releases_attempted += 1
            if await release_capital_for_position(capital_router_url, pos_id, arena):
                releases_succeeded += 1

    summary = {
        "positions_checked": len(closed_positions),
        "allocations_found": allocations_found,
        "releases_attempted": releases_attempted,
        "releases_succeeded": releases_succeeded,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(f"Reconciliation complete: {json.dumps(summary)}")

    # Alert if any releases failed
    if releases_attempted > 0 and releases_succeeded < releases_attempted:
        send_alert(
            source="alpha-daily-reconciler",
            level="WARNING",
            title="⚠️  CAPITAL RECONCILIATION — Some Releases Failed",
            body=f"Attempted {releases_attempted} releases, {releases_succeeded} succeeded. "
            f"Check logs for details.",
            dedup_key="capital_reconciliation_partial_failure",
            targets=["nexus_health_group"],
        )

    return summary


async def main():
    """Main entry point for daily reconciliation."""
    try:
        logger.info("=" * 80)
        logger.info("Daily Capital Reconciliation Starting")
        logger.info("=" * 80)

        summary = await reconcile_closed_positions(ALPHA_EXEC_DB, CAPITAL_ROUTER_URL)

        logger.info("=" * 80)
        logger.info(f"Summary: {json.dumps(summary, indent=2)}")
        logger.info("=" * 80)

        return 0
    except Exception as e:
        logger.error(f"Reconciliation failed: {e}", exc_info=True)
        send_alert(
            source="alpha-daily-reconciler",
            level="CRITICAL",
            title="🚨 CAPITAL RECONCILIATION FAILED",
            body=f"Daily reconciliation crashed: {str(e)[:200]}",
            dedup_key="capital_reconciliation_crash",
            targets=["nexus_health_group"],
        )
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
