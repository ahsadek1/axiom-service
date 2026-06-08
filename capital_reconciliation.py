#!/usr/bin/env python3
"""
CAPITAL RECONCILIATION — Periodic Orphan Detection & Position Reconciliation

Runs every 5 minutes during market hours (09:30–16:00 ET).
Detects orphan positions (in Alpaca but not in position_binding).
Alerts SOVEREIGN if orphans found.
Registers incidents in CHRONICLE.

STATUS: ACTIVE
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pytz

# Import shared modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '.openclaw', 'workspace-cipher'))

from capital_manager import CapitalManager, OrphanPosition
from shared.sovereign_comms import EscalationLevel, report

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("capital_reconciliation")

ET = pytz.timezone("America/New_York")

# ============================================================================
# CONFIGURATION
# ============================================================================

RECONCILIATION_INTERVAL_SEC = 300  # 5 minutes
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0


class CapitalReconciler:
    """Periodic reconciliation of Alpaca positions vs position_binding table."""

    def __init__(self):
        self.capital_manager = CapitalManager()
        self.alpaca_client = None
        self.last_reconciliation: Optional[datetime] = None
        self.orphan_count = 0
        self._init_alpaca_client()

    def _init_alpaca_client(self):
        """Initialize Alpaca client from environment."""
        try:
            from alpaca_trade_api import REST

            api_key = os.getenv("ALPACA_API_KEY")
            secret_key = os.getenv("ALPACA_SECRET_KEY")

            if not api_key or not secret_key:
                logger.warning("Alpaca credentials not found in env")
                return

            # Set environment for REST client
            os.environ["APCA_API_KEY_ID"] = api_key
            os.environ["APCA_API_SECRET_KEY"] = secret_key
            os.environ["APCA_API_BASE_URL"] = "https://paper-api.alpaca.markets"

            self.alpaca_client = REST()
            logger.info("Alpaca client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Alpaca client: {e}")
            self.alpaca_client = None

    def is_market_hours(self) -> bool:
        """Check if current time is within market hours (09:30-16:00 ET on weekdays)."""
        now = datetime.now(ET)

        # Weekend check
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        # Time check: 09:30-16:00
        if now.hour < MARKET_OPEN_HOUR:
            return False
        if now.hour == MARKET_OPEN_HOUR and now.minute < MARKET_OPEN_MIN:
            return False
        if now.hour > MARKET_CLOSE_HOUR:
            return False

        return True

    async def reconcile_once(self) -> bool:
        """
        Run one reconciliation cycle.

        Returns: True if successful, False otherwise
        """
        try:
            now = datetime.now(ET)
            logger.info("=== CAPITAL RECONCILIATION CYCLE START ===")

            # Only run during market hours
            if not self.is_market_hours():
                logger.debug(f"Outside market hours ({now.strftime('%H:%M')}), skipping")
                return False

            # Detect orphans
            orphans = self.capital_manager.detect_orphans(self.alpaca_client)

            self.last_reconciliation = now
            self.orphan_count = len(orphans)

            if orphans:
                logger.error(f"Found {len(orphans)} orphan position(s)")
                await self._alert_sovereign(orphans)
                await self._log_to_chronicle(orphans)
            else:
                logger.info("Reconciliation OK: no orphans detected")

            logger.info("=== CAPITAL RECONCILIATION CYCLE END ===")
            return True

        except Exception as e:
            logger.error(f"Reconciliation cycle failed: {e}", exc_info=True)
            return False

    async def _alert_sovereign(self, orphans: List[OrphanPosition]):
        """Alert SOVEREIGN of orphan positions."""
        try:
            payload = {
                "event": "capital_orphan_detected",
                "count": len(orphans),
                "timestamp": datetime.now(ET).isoformat(),
                "orphans": [
                    {
                        "symbol": o.symbol,
                        "qty": o.qty,
                        "entry_price": o.entry_price,
                        "market_value": o.market_value,
                    }
                    for o in orphans
                ],
                "action_required": "reconcile_positions_manually",
            }

            report(
                "capital_reconciliation",
                "alert",
                payload,
                escalation=EscalationLevel.CRITICAL,
            )
            logger.info(f"Alert sent to SOVEREIGN: {len(orphans)} orphans")
        except Exception as e:
            logger.error(f"Failed to alert SOVEREIGN: {e}")

    async def _log_to_chronicle(self, orphans: List[OrphanPosition]):
        """Register orphan incident in CHRONICLE."""
        try:
            # Log orphans to audit trail
            logger.critical(f"CHRONICLE: Orphan incident — {len(orphans)} untracked position(s)")
            for o in orphans:
                logger.critical(
                    f"  - {o.symbol}: qty={o.qty} entry=${o.entry_price:.2f} market_value=${o.market_value:.2f}"
                )
        except Exception as e:
            logger.error(f"Failed to log to chronicle: {e}")

    async def run(self):
        """Main reconciliation loop."""
        logger.info("Capital Reconciliation Engine starting...")

        while True:
            try:
                await self.reconcile_once()
            except Exception as e:
                logger.error(f"Reconciliation error: {e}", exc_info=True)

            # Wait before next cycle
            await asyncio.sleep(RECONCILIATION_INTERVAL_SEC)


# ============================================================================
# ENTRY POINT
# ============================================================================

async def main():
    """Start reconciliation engine."""
    reconciler = CapitalReconciler()
    await reconciler.run()


if __name__ == "__main__":
    asyncio.run(main())
