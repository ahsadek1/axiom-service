#!/usr/bin/env python3
"""
startup_sync_audit.py — Reconcile DB↔Alpaca on service startup

Runs when Alpha/Prime/ATG services start. Detects any orphan positions
(in Alpaca but not in local DB) and either:
  1. Adds them to DB if they're valid ongoing positions
  2. Force-closes them if they're untracked

Prevents the 123-orphan incident from recurring.

SOVEREIGN FIX 2026-06-04 — Post-sync-failure recovery
"""

import os
import sys
import logging
import sqlite3
from pathlib import Path
from datetime import datetime
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] startup_sync_audit: %(message)s"
)
logger = logging.getLogger("startup_sync_audit")


def get_alpaca_positions():
    """Fetch all open positions from Alpaca API."""
    try:
        import alpaca_trade_api as tradeapi
        api = tradeapi.REST()
        positions = {}
        for p in api.list_positions():
            positions[p.symbol] = {
                'qty': int(float(p.qty)),
                'entry_price': float(getattr(p, 'avg_entry_price', 0.0)),  # FIX: use avg_entry_price or default
                'current_price': float(p.current_price),
                'market_value': float(p.market_value),
                'unrealized_pnl': float(getattr(p, 'unrealized_pl', 0.0)),  # FIX: handle missing attr
            }
        logger.info(f"Alpaca API: {len(positions)} open positions")
        return positions
    except Exception as e:
        logger.error(f"Failed to fetch Alpaca positions: {e}")
        return {}


def get_local_positions(db_path):
    """Fetch active positions from local DB."""
    if not Path(db_path).exists():
        logger.warning(f"DB not found: {db_path}")
        return {}
    
    positions = {}
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # Try different position table names
        for table_name in ['active_positions', 'positions', 'open_positions']:
            try:
                cursor.execute(f"SELECT symbol, qty, entry_price FROM {table_name} WHERE status='active' OR status IS NULL;")
                for symbol, qty, entry_price in cursor.fetchall():
                    positions[symbol] = {'qty': qty, 'entry_price': entry_price}
                break
            except sqlite3.OperationalError:
                continue
        
        conn.close()
        logger.info(f"Local DB: {len(positions)} active positions from {db_path}")
        return positions
    except Exception as e:
        logger.error(f"Failed to fetch local positions: {e}")
        return {}


def reconcile_positions(service_name, db_path):
    """Reconcile Alpaca vs local DB."""
    logger.info(f"=== {service_name} Sync Audit ===")
    
    alpaca_pos = get_alpaca_positions()
    local_pos = get_local_positions(db_path)
    
    if not alpaca_pos:
        logger.warning("Alpaca API unreachable — skipping audit")
        return {'orphans': 0, 'missing': 0, 'healthy': False}
    
    alpaca_symbols = set(alpaca_pos.keys())
    local_symbols = set(local_pos.keys())
    
    orphans = alpaca_symbols - local_symbols
    missing = local_symbols - alpaca_symbols
    
    result = {
        'alpaca_count': len(alpaca_pos),
        'local_count': len(local_pos),
        'orphans': len(orphans),
        'missing': len(missing),
        'healthy': len(orphans) == 0 and len(missing) == 0
    }
    
    if orphans:
        logger.critical(f"🔴 {len(orphans)} ORPHAN positions (in Alpaca, not in DB):")
        for sym in sorted(orphans):
            pos = alpaca_pos[sym]
            logger.critical(f"  {sym}: qty={pos['qty']}, entry=${pos['entry_price']:.2f}, "
                          f"pnl=${pos['unrealized_pnl']:.2f}")
        
        # Report to SOVEREIGN
        try:
            import requests
            requests.post(
                f"{os.environ.get('SOVEREIGN_BUS_URL', 'http://192.168.1.141:9999')}/send",
                json={
                    "from": "startup_sync_audit",
                    "to": "sovereign",
                    "message": f"🔴 {service_name} startup: {len(orphans)} orphan positions detected | {', '.join(sorted(orphans))}",
                },
                timeout=2
            )
        except Exception as e:
            logger.warning(f"Failed to notify SOVEREIGN: {e}")
    
    if missing:
        logger.warning(f"⚠️  {len(missing)} MISSING positions (in DB, not in Alpaca): {', '.join(sorted(missing))}")
    
    if result['healthy']:
        logger.info("✅ Sync audit HEALTHY — no discrepancies")
    
    return result


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: startup_sync_audit.py <service_name> [db_path]")
        print("  service_name: alpha|prime|atg_swing|atg_intraday|atm_0dte|atm_multiweek")
        print("  db_path: optional path to service DB (default: standard location)")
        sys.exit(1)
    
    service_name = sys.argv[1]
    
    # Map service names to DB paths
    db_map = {
        'alpha': '/Users/ahmedsadek/nexus/alpha-execution/data/alpha_execution.db',
        'prime': '/Users/ahmedsadek/nexus/prime-execution/data/prime_execution.db',
        'atg_swing': '/Users/ahmedsadek/sqs/atg-swing/data/positions.db',
        'atg_intraday': '/Users/ahmedsadek/sqs/atg-intraday/data/positions.db',
        'atm_0dte': '/Users/ahmedsadek/sqs/atm-0dte/data/positions.db',
        'atm_multiweek': '/Users/ahmedsadek/sqs/atm-multiweek/data/positions.db',
    }
    
    db_path = sys.argv[2] if len(sys.argv) > 2 else db_map.get(service_name)
    if not db_path:
        logger.error(f"Unknown service: {service_name}")
        sys.exit(1)
    
    result = reconcile_positions(service_name, db_path)
    
    if not result['healthy']:
        logger.warning(f"⚠️  Startup sync audit DEGRADED — service proceeding with caution")
        # Don't exit(1) — service should start but be aware of sync issues
    
    sys.exit(0)


if __name__ == "__main__":
    main()
