#!/usr/bin/env python3
"""
Reconcile Orphan Positions — Retroactively add untracked positions to position_binding

This script detects orphan positions in Alpaca and creates retroactive position_binding
entries for them, allowing them to be tracked and managed properly going forward.

Usage:
    python3 reconcile_orphan_positions.py [--dry-run] [--close]
    
Options:
    --dry-run   Show what would be reconciled without making changes
    --close     Close all orphan positions after reconciling
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'alpha-execution'))

from capital_manager import CapitalManager, PositionStatus
from alpaca_client import AlpacaClient

# ============================================================================
# LOGGING
# ============================================================================

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("reconcile_orphans")

# Suppress noisy imports
logging.getLogger('alpaca_trade_api').setLevel(logging.WARNING)

ET = pytz.timezone("America/New_York")


def reconcile_orphans(dry_run: bool = True, close: bool = False):
    """
    Reconcile orphan positions.
    
    Args:
        dry_run: If True, show what would be done without making changes
        close: If True, close all orphan positions after reconciling
    """
    # Initialize
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    
    if not api_key or not secret_key:
        logger.error("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        return False
    
    alpaca_client = AlpacaClient(api_key, secret_key)
    capital_manager = CapitalManager()
    
    logger.info(f"\n{'='*60}")
    logger.info(f"ORPHAN RECONCILIATION {'(DRY RUN)' if dry_run else '(LIVE)'}")
    logger.info(f"{'='*60}\n")
    
    # Fetch live positions
    try:
        positions = alpaca_client.get_positions()
        logger.info(f"Found {len(positions)} positions in Alpaca")
    except Exception as e:
        logger.error(f"Failed to fetch Alpaca positions: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Check which are tracked
    import sqlite3
    conn = sqlite3.connect(capital_manager.db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT DISTINCT symbol FROM position_binding
        WHERE status IN ('filled', 'allocated', 'submitted')
    """)
    tracked = {row['symbol'] for row in cursor.fetchall()}
    conn.close()
    
    logger.info(f"Currently tracking {len(tracked)} symbols: {sorted(tracked)}")
    
    # Find orphans
    orphans = []
    for pos in positions:
        if pos['symbol'] not in tracked:
            orphans.append(pos)
    
    if not orphans:
        logger.info("\n✅ No orphan positions found — all positions are tracked")
        return True
    

    
    logger.info(f"\n⚠️  Found {len(orphans)} orphan position(s):\n")
    
    for pos in orphans:
        symbol = pos['symbol']
        qty = float(pos['qty'])
        entry = float(pos['avg_entry_price'])
        market = float(pos['market_value'])
        logger.info(
            f"  {symbol:6} | qty={qty:6.0f} | "
            f"entry=${entry:8.2f} | market=${market:10.2f}"
        )
    
    if dry_run:
        logger.info(f"\n[DRY RUN] Would reconcile {len(orphans)} position(s)")
        logger.info("Run with --no-dry-run to actually reconcile")
        return True
    
    # ===============================================================
    # LIVE RECONCILIATION
    # ===============================================================
    
    logger.info(f"\n{'='*60}")
    logger.info("RECONCILING...")
    logger.info(f"{'='*60}\n")
    
    reconciled = 0
    for pos in orphans:
        try:
            symbol = pos['symbol']
            qty = int(pos['qty'])
            entry_price = float(pos['avg_entry_price'])
            market_value = float(pos['market_value'])
            
            # Create retroactive binding entry
            now = datetime.now(ET)
            allocation_id = f"retroactive_{symbol}_{now.timestamp()}"
            
            conn = sqlite3.connect(capital_manager.db_path)
            cursor = conn.cursor()
            
            # Create fake allocation
            cursor.execute("""
                INSERT INTO allocations
                (allocation_id, symbol, execution_system, amount_usd, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                allocation_id,
                symbol,
                "unknown",  # We don't know which system
                market_value,
                "active",
                (now - timedelta(days=30)).isoformat(),  # Assume old
                now.isoformat(),
            ))
            
            # Create position_binding entry
            cursor.execute("""
                INSERT INTO position_binding
                (transaction_id, alpaca_order_id, alpaca_position_id, symbol,
                 execution_system, allocation_id, allocated_amount_usd,
                 predicted_qty, predicted_entry_price, predicted_market_value,
                 actual_qty, actual_entry_price, actual_market_value, actual_slippage_pct,
                 status, created_at, created_by, state_updated_at, state_updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"retroactive_{symbol}_{now.timestamp()}",  # transaction_id
                f"retroactive_{symbol}",  # alpaca_order_id (fake)
                symbol,  # alpaca_position_id (use symbol as placeholder)
                symbol,
                "unknown",
                allocation_id,
                market_value,
                qty,  # predicted_qty
                entry_price,
                market_value,
                qty,  # actual_qty
                entry_price,
                market_value,
                0.0,  # No slippage for retroactive
                PositionStatus.FILLED.value,
                (now - timedelta(days=30)).isoformat(),  # Created long ago
                "reconciliation_script",
                now.isoformat(),
                "reconciliation_script",
            ))
            
            # Record audit
            cursor.execute("""
                INSERT INTO position_binding_audit
                (binding_id, event_type, new_status, details, changed_by, changed_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                cursor.lastrowid,
                "created",
                PositionStatus.FILLED.value,
                f'{{"reason": "retroactive_reconciliation", "symbol": "{symbol}", "qty": {qty}}}',
                "reconciliation_script",
                now.isoformat(),
            ))
            
            conn.commit()
            conn.close()
            
            logger.info(f"✅ Reconciled {symbol}: qty={qty} @ ${entry_price:.2f}")
            reconciled += 1
            
        except Exception as e:
            logger.error(f"❌ Failed to reconcile {pos['symbol']}: {e}")
    
    logger.info(f"\n{'='*60}")
    logger.info(f"RECONCILIATION COMPLETE: {reconciled}/{len(orphans)} positions added")
    logger.info(f"{'='*60}\n")
    
    # Optional: close all orphan positions
    if close:
        logger.info("Closing all orphan positions...")
        for pos in orphans:
            try:
                symbol = pos['symbol']
                qty = abs(int(pos['qty']))
                alpaca_client.close_position(symbol, qty)
                logger.info(f"✅ Closed {symbol}")
            except Exception as e:
                logger.error(f"❌ Failed to close {pos['symbol']}: {e}")
    
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reconcile orphan positions to position_binding table"
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually reconcile (default is dry-run)",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="Close all orphan positions after reconciling",
    )
    
    args = parser.parse_args()
    
    success = reconcile_orphans(
        dry_run=not args.no_dry_run,
        close=args.close,
    )
    
    sys.exit(0 if success else 1)
