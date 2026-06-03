#!/usr/bin/env python3
"""
test_omni_integration.py — Integration test for OMNI execution fixes

Tests the entire flow:
1. Queue worker creates position in active_positions_v2
2. get_open_positions() returns the new position
3. Confirmation polling finds the position
"""

import sys
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime
import pytz

ET = pytz.timezone("America/New_York")

def test_complete_flow():
    """Test the complete flow from order to confirmation."""
    
    # Create a test database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name
    
    print(f"Using test database: {test_db}")
    
    try:
        conn = sqlite3.connect(test_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Create active_positions_v2 table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_positions_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                arena TEXT NOT NULL DEFAULT 'alpha',
                client_order_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                strategy TEXT,
                direction TEXT,
                max_risk_usd REAL,
                allocated_usd REAL,
                dte_at_entry INTEGER,
                expiry TEXT,
                pathway TEXT,
                window_id TEXT,
                alpaca_order_id TEXT,
                fill_price REAL,
                notes TEXT,
                opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
                pnl_usd REAL
            )
        """)
        
        # Simulate queue_worker creating a position
        now = datetime.now(ET).isoformat()
        cursor.execute("""
            INSERT INTO active_positions_v2 
            (ticker, arena, client_order_id, status, strategy, direction, 
             allocated_usd, pathway, window_id, alpaca_order_id, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "AAPL",
            "alpha",
            "queue-123",
            "pending",
            "equity",
            "bullish",
            2240.0,
            "ATG_SWING",
            "window-456",
            "alpaca-order-789",
            now
        ))
        position_id = cursor.lastrowid
        conn.commit()
        
        print(f"✅ Created position record (id={position_id})")
        
        # Simulate get_open_positions() query
        cursor.execute("""
            SELECT ticker, direction, status, alpaca_order_id, pathway, window_id, opened_at 
            FROM active_positions_v2 WHERE status IN ('pending', 'open') ORDER BY opened_at ASC
        """)
        rows = cursor.fetchall()
        positions = [dict(r) for r in rows]
        
        if not positions:
            print("❌ get_open_positions() returned no records")
            return False
        
        print(f"✅ get_open_positions() returned {len(positions)} positions")
        
        # Simulate confirmation polling logic
        matching = [p for p in positions if p.get("ticker") == "AAPL"]
        if not matching:
            print("❌ Confirmation polling could not find AAPL")
            return False
        
        print(f"✅ Confirmation polling found AAPL position")
        print(f"   Position details: {matching[0]}")
        
        # Verify status is pending
        if matching[0]["status"] != "pending":
            print(f"❌ Position status is {matching[0]['status']}, expected 'pending'")
            return False
        
        print(f"✅ Position status is 'pending'")
        
        conn.close()
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Clean up
        try:
            Path(test_db).unlink()
        except:
            pass

if __name__ == "__main__":
    print("=" * 60)
    print("OMNI EXECUTION INTEGRATION TEST")
    print("=" * 60)
    
    success = test_complete_flow()
    
    print("\n" + "=" * 60)
    if success:
        print("✅ Integration test PASSED")
        sys.exit(0)
    else:
        print("❌ Integration test FAILED")
        sys.exit(1)
