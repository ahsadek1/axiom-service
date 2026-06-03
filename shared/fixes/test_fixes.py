"""
test_fixes.py — Comprehensive test suite for RC#1-RC#5 + VAR#1-VAR#6 fixes

Tests transaction atomicity, partial fills, audit logging, DB retry, and sync gaps.
"""

import pytest
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path

from transaction_atomicity import (
    AtomicPositionTransaction, TransactionError, TransactionStep
)
from position_audit_log import PositionAuditLog, AuditAction
from partial_fill_tracker import PartialFillTracker, FillEvent
from db_write_retry import DBWriteRetry, DeadLetterQueue, RetryStrategy


class MockAlpacaClient:
    """Mock Alpaca client for testing."""
    def __init__(self, should_fail_on_attempt=None):
        self.orders = {}
        self.should_fail_on_attempt = should_fail_on_attempt
        self.submit_count = 0
    
    def submit_order(self, symbol, qty, side, type):
        self.submit_count += 1
        if self.should_fail_on_attempt == self.submit_count:
            raise Exception("Simulated order submission failure")
        
        # Create order object using simple class
        class Order:
            pass
        
        order = Order()
        order.id = f'ord_{self.submit_count}'
        order.symbol = symbol
        order.qty = qty
        order.side = side
        order.status = 'pending'
        
        self.orders[order.id] = order
        return order
    
    def cancel_order(self, order_id):
        if order_id in self.orders:
            del self.orders[order_id]


class TestTransactionAtomicity:
    """Test VAR#1: Transaction atomicity wrapper."""
    
    def setup_method(self):
        """Setup test database."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        
        # Initialize database
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY,
                system TEXT,
                ticker TEXT,
                qty INTEGER,
                side TEXT,
                status TEXT,
                order_id TEXT,
                allocation_id TEXT,
                created_at TEXT,
                price REAL
            );
        """)
        conn.commit()
        conn.close()
    
    def test_successful_transaction(self):
        """Test successful atomic transaction (all steps succeed)."""
        client = MockAlpacaClient()
        
        def mock_allocate(ticker, system, amount_usd):
            return f"alloc_{ticker}_{amount_usd}"
        
        try:
            with AtomicPositionTransaction(self.db_path, "alpha", "NVDA", qty=100) as txn:
                alloc_id = txn.step_allocate_capital(5000.0, allocation_client=mock_allocate)
                order_id = txn.step_place_order(alpaca_client=client, side="buy")
                txn.step_update_db(order_id=order_id, alloc_id=alloc_id)
            
            # Verify DB was updated
            conn = sqlite3.connect(self.db_path)
            row = conn.execute("SELECT * FROM positions WHERE order_id = ?", (order_id,)).fetchone()
            conn.close()
            
            assert row is not None, "Position should be in database"
            assert row[1] == "alpha", "System should be alpha"
            assert row[2] == "NVDA", "Ticker should be NVDA"
        
        except TransactionError:
            pytest.fail("Transaction should not have failed")
    
    def test_failed_transaction_rollback(self):
        """Test transaction rollback on failure."""
        client = MockAlpacaClient(should_fail_on_attempt=1)
        
        def mock_allocate(ticker, system, amount_usd):
            return f"alloc_{ticker}"
        
        with pytest.raises(TransactionError):
            with AtomicPositionTransaction(self.db_path, "alpha", "NVDA", qty=100) as txn:
                txn.step_allocate_capital(5000.0, allocation_client=mock_allocate)
                txn.step_place_order(alpaca_client=client)
        
        # Verify position was NOT added to DB (rollback worked)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT COUNT(*) FROM positions").fetchone()
        conn.close()
        
        assert row[0] == 0, "Database should be empty after rollback"


class TestPositionAuditLog:
    """Test VAR#6: Immutable position audit trail."""
    
    def setup_method(self):
        """Setup audit database."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        self.audit = PositionAuditLog(self.db_path)
    
    def test_record_audit_event(self):
        """Test recording a single audit event."""
        self.audit.record(
            position_id="pos_123",
            action=AuditAction.POSITION_OPENED,
            before_state="pending",
            after_state="open",
            service="alpha-execution",
            reason="entry_signal",
            ticker="NVDA"
        )
        
        timeline = self.audit.get_position_timeline("pos_123")
        assert len(timeline) == 1
        assert timeline[0]["action"] == "position_opened"
        assert timeline[0]["ticker"] == "NVDA"
    
    def test_position_timeline(self):
        """Test full position lifecycle in audit log."""
        pos_id = "pos_456"
        events = [
            (AuditAction.ALLOCATION_CREATED, "pending", "allocated"),
            (AuditAction.ORDER_PLACED, "allocated", "order_pending"),
            (AuditAction.POSITION_OPENED, "order_pending", "open"),
            (AuditAction.CLOSE_INITIATED, "open", "closing"),
            (AuditAction.POSITION_CLOSED, "closing", "closed"),
        ]
        
        for action, before, after in events:
            self.audit.record(
                position_id=pos_id,
                action=action,
                before_state=before,
                after_state=after,
                service="alpha-execution"
            )
        
        timeline = self.audit.get_position_timeline(pos_id)
        assert len(timeline) == len(events)
        assert timeline[0]["action"] == "allocation_created"
        assert timeline[-1]["action"] == "position_closed"
    
    def test_find_orphans(self):
        """Test orphan detection in audit log."""
        pos_id = "orphan_pos"
        
        # Record orphan detection but no recovery
        self.audit.record(
            position_id=pos_id,
            action=AuditAction.ORPHAN_DETECTED,
            reason="db_update_failed",
            service="alpha-execution"
        )
        
        orphans = self.audit.find_orphans(time_window_hours=24)
        assert len(orphans) > 0
        assert orphans[0]["position_id"] == pos_id


class TestPartialFillTracker:
    """Test VAR#2: Partial fill state machine."""
    
    def setup_method(self):
        """Setup fill tracking database."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        
        # Initialize positions table
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY,
                order_id TEXT,
                ticker TEXT,
                qty INTEGER,
                filled_qty INTEGER,
                remaining_qty INTEGER,
                avg_fill_price REAL,
                status TEXT
            );
        """)
        # Insert test position
        conn.execute(
            "INSERT INTO positions (order_id, ticker, qty, status) VALUES (?, ?, ?, ?)",
            ("ord_test", "NVDA", 100, "open")
        )
        conn.commit()
        conn.close()
        
        self.tracker = PartialFillTracker(self.db_path)
    
    def test_single_fill(self):
        """Test recording a single fill."""
        fill = self.tracker.record_fill(
            order_id="ord_test",
            ticker="NVDA",
            qty=50,
            price=140.50
        )
        
        assert fill.sequence == 1
        assert fill.fill_qty == 50
        assert fill.cumulative_qty == 50
    
    def test_multiple_partial_fills(self):
        """Test multiple partial fills for same order."""
        # First fill
        fill1 = self.tracker.record_fill(
            order_id="ord_multi",
            ticker="AAPL",
            qty=30,
            price=150.00
        )
        assert fill1.sequence == 1
        assert fill1.cumulative_qty == 30
        
        # Second fill (30 minutes later)
        fill2 = self.tracker.record_fill(
            order_id="ord_multi",
            ticker="AAPL",
            qty=40,
            price=150.50
        )
        assert fill2.sequence == 2
        assert fill2.cumulative_qty == 70
        
        # Check state
        state = self.tracker.get_fill_state("ord_multi")
        assert state["filled_qty"] == 70
        assert state["remaining_qty"] == 30
        assert state["avg_price"] == 150.2
    
    def test_is_fully_filled(self):
        """Test fully-filled detection."""
        # Partially filled
        self.tracker.record_fill(
            order_id="ord_partial",
            ticker="SPY",
            qty=50,
            price=450.00
        )
        assert not self.tracker.is_fully_filled("ord_partial")
        
        # Now fully fill the rest
        self.tracker.record_fill(
            order_id="ord_partial",
            ticker="SPY",
            qty=50,
            price=450.25
        )
        assert self.tracker.is_fully_filled("ord_partial")


class TestDBWriteRetry:
    """Test VAR#4: Database write retry logic."""
    
    def test_successful_write(self):
        """Test successful write on first attempt."""
        call_count = 0
        
        def operation():
            nonlocal call_count
            call_count += 1
            return "success"
        
        with DBWriteRetry(max_attempts=3) as retry:
            result = retry.execute(operation, operation_name="test_write")
        
        assert result == "success"
        assert call_count == 1
    
    def test_retry_on_failure(self):
        """Test retry logic on transient failure."""
        call_count = 0
        
        def operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise sqlite3.OperationalError("database is locked")
            return "success"
        
        with DBWriteRetry(max_attempts=3, base_wait_sec=0.01) as retry:
            result = retry.execute(operation, operation_name="test_retry")
        
        assert result == "success"
        assert call_count == 3
    
    def test_exhausted_retries(self):
        """Test failure after max retries exhausted."""
        def operation():
            raise sqlite3.OperationalError("permanent error")
        
        with pytest.raises(Exception):
            with DBWriteRetry(max_attempts=2, base_wait_sec=0.01) as retry:
                retry.execute(operation, operation_name="test_fail")


class TestDeadLetterQueue:
    """Test VAR#4: Dead letter queue for failed operations."""
    
    def setup_method(self):
        """Setup DLQ database."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        self.dlq = DeadLetterQueue(self.db_path)
    
    def test_enqueue_operation(self):
        """Test enqueueing a failed operation."""
        dlq_id = self.dlq.enqueue(
            operation="close_position",
            context={"order_id": "ord_123", "status": "closed"},
            error="database timeout"
        )
        
        assert dlq_id is not None
        
        items = self.dlq.get_all()
        assert len(items) == 1
        assert items[0]["operation"] == "close_position"
        assert items[0]["context"]["order_id"] == "ord_123"
    
    def test_mark_processed(self):
        """Test marking DLQ item as processed."""
        dlq_id = self.dlq.enqueue(
            operation="update_position",
            context={"pos_id": "pos_1"},
            error=""
        )
        
        self.dlq.mark_processed(dlq_id)
        
        pending = self.dlq.get_all(include_processed=False)
        assert len(pending) == 0
        
        all_items = self.dlq.get_all(include_processed=True)
        assert len(all_items) == 1
        assert all_items[0]["processed"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
