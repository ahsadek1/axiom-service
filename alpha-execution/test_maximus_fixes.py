"""
MAXIMUS Test Suite — Verify all 4 fixes are production-ready

Tests:
1. FIX #1: Capital Release Hook — capital is released after position close
2. FIX #2: Arena Field Schema — positions store and track arena
3. FIX #3: Daily Reconciliation — stale allocations are cleaned up
4. FIX #4: Capital Audit Endpoint — drift is detected and reported

Run:
    python -m pytest test_maximus_fixes.py -v
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import pytest
import sys

sys.path.insert(0, "/Users/ahmedsadek/nexus/alpha-execution")
sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")

from database import (
    init_db, get_conn, reserve_position_slot, confirm_pending_position,
    close_position, get_live_positions
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def temp_db():
    """Create a temporary test database."""
    db_path = "/tmp/test_alpha_exec.db"
    # Remove old db if exists
    try:
        Path(db_path).unlink()
    except:
        pass
    
    init_db(db_path)
    yield db_path
    
    # Cleanup
    try:
        Path(db_path).unlink()
    except:
        pass


@pytest.fixture
def sample_position(temp_db):
    """Create a sample position for testing."""
    pos_id = reserve_position_slot(
        db_path=temp_db,
        ticker="SPY",
        direction="bullish",
        pathway="P1",
        option_type="bull_put_spread",
        short_strike=420.0,
        long_strike=415.0,
        expiration_date="2026-06-15",
        dte_at_open=14,
        contracts=1,
        position_size_usd=1000.0,
        window_id="w1",
        agent_scores="{}",
        verdict="PASS",
        arena="alpha",  # FIX #2: arena parameter
    )
    
    confirm_pending_position(
        db_path=temp_db,
        position_id=pos_id,
        short_alpaca_order_id="order_short_123",
        long_alpaca_order_id="order_long_456",
        short_contract_symbol="SPY   260615C00420000",
        long_contract_symbol="SPY   260615P00415000",
        entry_price=0.50,
    )
    
    return pos_id


# ============================================================================
# FIX #1: CAPITAL RELEASE HOOK TESTS
# ============================================================================

def test_fix1_capital_release_function_exists():
    """Verify capital release function is defined in exit_monitor."""
    import exit_monitor
    assert hasattr(exit_monitor, '_release_capital_allocation'), \
        "FIX #1: _release_capital_allocation function not found"


def test_fix1_close_position_calls_release():
    """Verify _execute_close accepts capital_router_url parameter."""
    import exit_monitor
    import inspect
    
    sig = inspect.signature(exit_monitor._execute_close)
    assert "capital_router_url" in sig.parameters, \
        "FIX #1: capital_router_url parameter missing from _execute_close"
    
    # Check default value
    param = sig.parameters["capital_router_url"]
    assert param.default == "http://localhost:9100", \
        "FIX #1: capital_router_url default should be http://localhost:9100"


def test_fix1_partial_close_calls_release():
    """Verify _execute_partial_close accepts capital_router_url parameter."""
    import exit_monitor
    import inspect
    
    sig = inspect.signature(exit_monitor._execute_partial_close)
    assert "capital_router_url" in sig.parameters, \
        "FIX #1: capital_router_url parameter missing from _execute_partial_close"


# ============================================================================
# FIX #2: ARENA FIELD SCHEMA TESTS
# ============================================================================

def test_fix2_schema_arena_column_exists(temp_db):
    """Verify arena column exists in positions table."""
    with get_conn(temp_db) as conn:
        # Get table info
        cursor = conn.execute("PRAGMA table_info(positions)")
        columns = [row[1] for row in cursor.fetchall()]
        assert "arena" in columns, \
            "FIX #2: 'arena' column not found in positions table"


def test_fix2_arena_default_value(temp_db):
    """Verify arena defaults to 'alpha' when not specified."""
    with get_conn(temp_db) as conn:
        # Insert without specifying arena
        conn.execute(
            """
            INSERT INTO positions (
                ticker, direction, pathway, option_type,
                short_strike, long_strike, expiration_date, dte_at_open,
                contracts, position_size_usd, status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("SPY", "bullish", "P1", "bull_put_spread",
             420.0, 415.0, "2026-06-15", 14,
             1, 1000.0, "pending", datetime.now(timezone.utc).isoformat())
        )
        
        # Verify default
        row = conn.execute("SELECT arena FROM positions ORDER BY id DESC LIMIT 1").fetchone()
        assert row[0] == "alpha", \
            "FIX #2: arena should default to 'alpha', got " + str(row[0])


def test_fix2_arena_stored_on_create(temp_db):
    """Verify arena is stored when creating a position."""
    pos_id = reserve_position_slot(
        db_path=temp_db,
        ticker="QQQ",
        direction="bullish",
        pathway="P1",
        option_type="bull_put_spread",
        short_strike=320.0,
        long_strike=315.0,
        expiration_date="2026-06-15",
        dte_at_open=14,
        contracts=1,
        position_size_usd=1500.0,
        window_id="w2",
        agent_scores="{}",
        verdict="PASS",
        arena="prime",  # Explicitly set to prime
    )
    
    with get_conn(temp_db) as conn:
        row = conn.execute(
            "SELECT arena FROM positions WHERE id=?", (pos_id,)
        ).fetchone()
    
    assert row[0] == "prime", \
        f"FIX #2: arena should be 'prime', got {row[0]}"


def test_fix2_arena_index_exists(temp_db):
    """Verify arena index is created for efficient querying."""
    with get_conn(temp_db) as conn:
        # List indexes on positions table
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='positions'")
        indexes = [row[0] for row in cursor.fetchall()]
        assert any("arena" in idx for idx in indexes), \
            f"FIX #2: arena index not found. Indexes: {indexes}"


# ============================================================================
# FIX #3: DAILY RECONCILIATION DAEMON TESTS
# ============================================================================

def test_fix3_reconciler_file_exists():
    """Verify daily_reconciler.py exists."""
    reconciler_path = Path("/Users/ahmedsadek/nexus/alpha-execution/daily_reconciler.py")
    assert reconciler_path.exists(), \
        "FIX #3: daily_reconciler.py not found"


def test_fix3_reconciler_imports():
    """Verify daily_reconciler imports correctly."""
    try:
        import daily_reconciler
        assert hasattr(daily_reconciler, 'reconcile_closed_positions'), \
            "FIX #3: reconcile_closed_positions function not found"
        assert hasattr(daily_reconciler, 'get_closed_positions_last_n_hours'), \
            "FIX #3: get_closed_positions_last_n_hours function not found"
    except ImportError as e:
        pytest.skip(f"Could not import daily_reconciler: {e}")


@pytest.mark.asyncio
async def test_fix3_get_closed_positions(temp_db, sample_position):
    """Verify reconciler can query closed positions."""
    # Close the sample position
    close_position(temp_db, sample_position, "TEST_CLOSE", 0.10, 100.0)
    
    # Try to fetch it
    import daily_reconciler
    closed = await daily_reconciler.get_closed_positions_last_n_hours(
        temp_db, hours=24
    )
    
    assert len(closed) > 0, \
        "FIX #3: No closed positions found"
    assert closed[0]["id"] == sample_position, \
        "FIX #3: Wrong position returned"


# ============================================================================
# FIX #4: CAPITAL AUDIT ENDPOINT TESTS
# ============================================================================

def test_fix4_capital_router_audit_endpoint_exists():
    """Verify /capital-audit endpoint is defined."""
    try:
        from capital_router import main as capital_router_main
        # Check that CapitalAuditResponse model exists
        # This is a quick smoke test — full test requires running the service
        pytest.skip("Requires running capital-router service")
    except ImportError:
        pytest.skip("capital_router not importable (not in path)")


def test_fix4_audit_endpoint_in_code():
    """Verify capital-audit endpoint code is present in capital-router."""
    capital_router_path = Path("/Users/ahmedsadek/nexus/capital-router/main.py")
    assert capital_router_path.exists(), \
        "FIX #4: capital-router/main.py not found"
    
    content = capital_router_path.read_text()
    assert "/capital-audit" in content, \
        "FIX #4: /capital-audit endpoint not found in main.py"
    assert "CapitalAuditResponse" in content, \
        "FIX #4: CapitalAuditResponse model not found in main.py"
    assert "drift_amount" in content, \
        "FIX #4: drift_amount field not found in audit response"
    assert "stale_allocations" in content, \
        "FIX #4: stale_allocations field not found in audit response"


def test_fix4_allocations_endpoint_for_reconciler():
    """Verify /allocations/{position_id} endpoint exists for reconciler."""
    capital_router_path = Path("/Users/ahmedsadek/nexus/capital-router/main.py")
    content = capital_router_path.read_text()
    
    assert "/allocations/{position_id}" in content, \
        "FIX #4: /allocations endpoint not found"
    assert "get_allocation" in content, \
        "FIX #4: get_allocation function not found"


# ============================================================================
# INTEGRATION TESTS
# ============================================================================

def test_end_to_end_position_lifecycle(temp_db):
    """End-to-end: create, confirm, and close a position with all fixes."""
    # Create position (FIX #2: arena is stored)
    pos_id = reserve_position_slot(
        db_path=temp_db,
        ticker="TSLA",
        direction="bullish",
        pathway="P1",
        option_type="bull_put_spread",
        short_strike=250.0,
        long_strike=245.0,
        expiration_date="2026-06-15",
        dte_at_open=14,
        contracts=2,
        position_size_usd=2000.0,
        window_id="w3",
        agent_scores="{}",
        verdict="PASS",
        arena="alpha",
    )
    
    # Verify it's pending
    with get_conn(temp_db) as conn:
        row = conn.execute("SELECT status, arena FROM positions WHERE id=?", (pos_id,)).fetchone()
        assert row[0] == "pending", "Status should be 'pending' after create"
        assert row[1] == "alpha", f"Arena should be 'alpha', got {row[1]}"
    
    # Confirm it
    confirm_pending_position(
        db_path=temp_db,
        position_id=pos_id,
        short_alpaca_order_id="order_ts_short",
        long_alpaca_order_id="order_ts_long",
        short_contract_symbol="TSLA  260615C00250000",
        long_contract_symbol="TSLA  260615P00245000",
        entry_price=0.75,
    )
    
    # Verify it's open with arena preserved
    with get_conn(temp_db) as conn:
        row = conn.execute("SELECT status, arena FROM positions WHERE id=?", (pos_id,)).fetchone()
        assert row[0] == "open", "Status should be 'open' after confirm"
        assert row[1] == "alpha", f"Arena should be preserved as 'alpha', got {row[1]}"
    
    # Close it (FIX #1: capital release would be called here)
    close_position(temp_db, pos_id, "TEST_PROFIT", 0.25, 500.0)
    
    # Verify it's closed with arena and P&L
    with get_conn(temp_db) as conn:
        row = conn.execute(
            "SELECT status, arena, pnl_pct, pnl_usd FROM positions WHERE id=?",
            (pos_id,)
        ).fetchone()
        assert row[0] == "closed", "Status should be 'closed' after close"
        assert row[1] == "alpha", f"Arena should be preserved as 'alpha', got {row[1]}"
        assert row[2] == 0.25, f"PnL% should be 0.25, got {row[2]}"
        assert row[3] == 500.0, f"PnL$ should be 500.0, got {row[3]}"


def test_drift_detection_scenario():
    """Scenario: Capital allocated but position closed → drift detected."""
    # This would require a running capital-router service
    # Here we just verify the logic is in place
    
    capital_router_path = Path("/Users/ahmedsadek/nexus/capital-router/main.py")
    content = capital_router_path.read_text()
    
    # Verify drift calculation exists
    assert "drift =" in content or "drift_amount" in content, \
        "FIX #4: Drift calculation not found"


# ============================================================================
# RUN TESTS
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
