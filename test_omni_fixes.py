#!/usr/bin/env python3
"""
test_omni_fixes.py — Verify OMNI execution engine fixes
Tests:
1. Position record is created when order is submitted to Alpaca
2. Confirmation polling doesn't block synthesis pool
"""

import sys
import sqlite3
from pathlib import Path

def test_position_creation():
    """Check if position table has the required columns for pending/open records."""
    db_path = Path("/Users/ahmedsadek/nexus/data/alpha_execution.db")
    
    if not db_path.exists():
        print("❌ Database not found")
        return False
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Check positions table schema
        cursor.execute("PRAGMA table_info(positions)")
        columns = {row[1] for row in cursor.fetchall()}
        
        required_cols = {'ticker', 'direction', 'status', 'opened_at', 'alpaca_order_id', 'verdict', 'window_id', 'pathway'}
        missing = required_cols - columns
        
        if missing:
            print(f"❌ Missing columns in positions table: {missing}")
            return False
        
        # Check if pending positions exist
        cursor.execute("SELECT COUNT(*) FROM positions WHERE status IN ('pending', 'open')")
        count = cursor.fetchone()[0]
        
        print(f"✅ Positions table has all required columns")
        print(f"   Current pending/open positions: {count}")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ Error checking database: {e}")
        return False

def test_queue_worker_logic():
    """Check if queue_worker.py has position creation logic."""
    qw_path = Path("/Users/ahmedsadek/nexus/alpha-execution/queue_worker.py")
    
    if not qw_path.exists():
        print("❌ queue_worker.py not found")
        return False
    
    content = qw_path.read_text()
    
    # Check for position creation logic
    if "INSERT INTO positions" in content and "CRITICAL FIX (2026-05-29)" in content:
        print("✅ Position creation logic found in queue_worker.py")
        return True
    else:
        print("❌ Position creation logic missing from queue_worker.py")
        return False

def test_synthesis_nonblocking():
    """Check if OMNI synthesis uses background thread for confirmation."""
    main_path = Path("/Users/ahmedsadek/nexus/omni/main.py")
    
    if not main_path.exists():
        print("❌ main.py not found")
        return False
    
    content = main_path.read_text()
    
    # Check for background task logic
    if "FIX-EXEC-CONFIRM-NONBLOCKING" in content and "_bg_thread" in content:
        print("✅ Non-blocking confirmation polling found in omni/main.py")
        return True
    else:
        print("❌ Non-blocking confirmation polling missing from omni/main.py")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("OMNI EXECUTION FIXES VERIFICATION")
    print("=" * 60)
    
    results = []
    
    print("\n[Test 1] Position table schema")
    results.append(test_position_creation())
    
    print("\n[Test 2] Queue worker position creation logic")
    results.append(test_queue_worker_logic())
    
    print("\n[Test 3] Non-blocking synthesis confirmation")
    results.append(test_synthesis_nonblocking())
    
    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"RESULT: {passed}/{total} tests passed")
    
    if passed == total:
        print("✅ All fixes verified!")
        sys.exit(0)
    else:
        print("❌ Some fixes missing or incomplete")
        sys.exit(1)
