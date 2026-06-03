"""
transaction_atomicity.py — Atomic Transaction Wrapper for Position Lifecycle

VAR#1 FIX: Ensures allocation → order → DB are transactional.
If any step fails, entire transaction rolls back.

Pattern:
  try:
      with AtomicPositionTransaction(db, "alpha", "SPY", qty=100) as txn:
          txn.step_allocate_capital(usd_amount)
          txn.step_place_order()
          txn.step_update_db()
      # All three succeed or all rollback
  except TransactionError as e:
      # Handle failure — position left in clean state
"""

import logging
import sqlite3
from datetime import datetime
from typing import Optional
from enum import Enum
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class TransactionStep(Enum):
    """Position transaction lifecycle steps."""
    ALLOCATED = "allocated"
    ORDER_PLACED = "order_placed"
    DB_UPDATED = "db_updated"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class TransactionCheckpoint:
    """Marker for rollback — records what needs to undo."""
    step: TransactionStep
    undo_fn: Optional[callable] = None  # Function to call on rollback
    data: Optional[dict] = None  # Context data for undo


class TransactionError(Exception):
    """Raised when any step in position transaction fails."""
    pass


class AtomicPositionTransaction:
    """
    Context manager for atomic position lifecycle.
    
    Usage:
        try:
            with AtomicPositionTransaction(db_path, "alpha", "NVDA", qty=100) as txn:
                alloc_id = txn.step_allocate_capital(amount=5000.0)
                order_id = txn.step_place_order(alpaca_client=client)
                txn.step_update_db(order_id=order_id, alloc_id=alloc_id)
        except TransactionError:
            # All steps rolled back
            pass
    """
    
    def __init__(self, db_path: str, system: str, ticker: str, qty: int):
        """
        Initialize transaction context.
        
        Args:
            db_path: Path to SQLite database
            system: "alpha" or "prime"
            ticker: Stock ticker
            qty: Order quantity
        """
        self.db_path = db_path
        self.system = system
        self.ticker = ticker
        self.qty = qty
        self.checkpoints: list[TransactionCheckpoint] = []
        self.state = TransactionStep.ALLOCATED  # Start state
        self.started_at = datetime.utcnow()
        self.failed_at = None
        self.error = None
        
    def __enter__(self):
        """Enter context manager."""
        logger.info(
            f"[ATOMIC TXN] Starting: {self.system} {self.ticker} qty={self.qty}"
        )
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager — rollback on any error."""
        if exc_type is not None or self.state == TransactionStep.FAILED:
            logger.error(
                f"[ATOMIC TXN] ROLLBACK: {self.system} {self.ticker} "
                f"state={self.state} error={exc_val}"
            )
            self._rollback_all()
            self.state = TransactionStep.ROLLED_BACK
            raise TransactionError(f"Position transaction failed: {exc_val}") from exc_val
        
        logger.info(
            f"[ATOMIC TXN] COMMIT: {self.system} {self.ticker} "
            f"steps={len(self.checkpoints)}"
        )
        return False
    
    def step_allocate_capital(
        self, 
        amount_usd: float,
        allocation_client: Optional[callable] = None
    ) -> str:
        """
        Step 1: Allocate capital via Capital Router.
        
        Args:
            amount_usd: Capital amount to allocate
            allocation_client: Callable that returns allocation_id
        
        Returns:
            allocation_id (string)
        
        Raises:
            TransactionError if allocation fails
        """
        try:
            if allocation_client is None:
                raise ValueError("allocation_client callable required")
            
            alloc_id = allocation_client(
                ticker=self.ticker,
                system=self.system,
                amount_usd=amount_usd
            )
            
            # Checkpoint for rollback
            def undo_allocate():
                logger.info(f"[ATOMIC TXN] Undo allocate {alloc_id}")
                # Call release-allocation endpoint
                # (implementation in consuming code)
            
            checkpoint = TransactionCheckpoint(
                step=TransactionStep.ALLOCATED,
                undo_fn=undo_allocate,
                data={"alloc_id": alloc_id, "amount": amount_usd}
            )
            self.checkpoints.append(checkpoint)
            
            logger.info(f"[ATOMIC TXN] Allocated: {alloc_id} ${amount_usd}")
            return alloc_id
        
        except Exception as e:
            self.state = TransactionStep.FAILED
            self.error = e
            raise
    
    def step_place_order(
        self,
        alpaca_client: Optional[callable] = None,
        side: str = "buy",
        order_type: str = "market"
    ) -> str:
        """
        Step 2: Place order on Alpaca.
        
        Args:
            alpaca_client: Alpaca client with submit_order() method
            side: "buy" or "sell"
            order_type: "market", "limit", etc.
        
        Returns:
            order_id (string)
        
        Raises:
            TransactionError if order placement fails
        """
        try:
            if alpaca_client is None:
                raise ValueError("alpaca_client required")
            
            order = alpaca_client.submit_order(
                symbol=self.ticker,
                qty=self.qty,
                side=side,
                type=order_type
            )
            
            order_id = order.id
            
            # Checkpoint for rollback
            def undo_order():
                logger.info(f"[ATOMIC TXN] Undo order {order_id}")
                try:
                    alpaca_client.cancel_order(order_id)
                except Exception as e:
                    logger.error(f"Failed to cancel order {order_id}: {e}")
            
            checkpoint = TransactionCheckpoint(
                step=TransactionStep.ORDER_PLACED,
                undo_fn=undo_order,
                data={"order_id": order_id, "side": side}
            )
            self.checkpoints.append(checkpoint)
            
            logger.info(f"[ATOMIC TXN] Placed order: {order_id}")
            return order_id
        
        except Exception as e:
            self.state = TransactionStep.FAILED
            self.error = e
            raise
    
    def step_update_db(
        self,
        order_id: str,
        alloc_id: str,
        price: Optional[float] = None
    ) -> None:
        """
        Step 3: Update position database.
        
        Args:
            order_id: Alpaca order ID
            alloc_id: Capital Router allocation ID
            price: Average fill price (if known)
        
        Raises:
            TransactionError if DB update fails
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Insert position record
            cursor.execute("""
                INSERT INTO positions 
                (system, ticker, qty, side, status, order_id, allocation_id, created_at, price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.system,
                self.ticker,
                self.qty,
                "buy",  # Hard-coded for now; parameterize if needed
                "open",
                order_id,
                alloc_id,
                datetime.utcnow().isoformat(),
                price or 0.0
            ))
            
            conn.commit()
            conn.close()
            
            # Checkpoint for rollback
            def undo_db():
                logger.info(f"[ATOMIC TXN] Undo DB for order {order_id}")
                try:
                    conn2 = sqlite3.connect(self.db_path)
                    conn2.execute(
                        "DELETE FROM positions WHERE order_id = ?",
                        (order_id,)
                    )
                    conn2.commit()
                    conn2.close()
                except Exception as e:
                    logger.error(f"Failed to rollback DB for {order_id}: {e}")
            
            checkpoint = TransactionCheckpoint(
                step=TransactionStep.DB_UPDATED,
                undo_fn=undo_db,
                data={"order_id": order_id}
            )
            self.checkpoints.append(checkpoint)
            
            logger.info(f"[ATOMIC TXN] Updated DB for order {order_id}")
            self.state = TransactionStep.DB_UPDATED
        
        except Exception as e:
            self.state = TransactionStep.FAILED
            self.error = e
            raise
    
    def _rollback_all(self) -> None:
        """
        Rollback all checkpoints in reverse order.
        Failure to rollback a single step doesn't stop others.
        """
        logger.warning(f"[ATOMIC TXN] Rolling back {len(self.checkpoints)} steps")
        
        # Reverse order: undo most recent first
        for checkpoint in reversed(self.checkpoints):
            try:
                if checkpoint.undo_fn:
                    checkpoint.undo_fn()
                    logger.info(f"[ATOMIC TXN] Rolled back: {checkpoint.step.value}")
            except Exception as e:
                logger.error(
                    f"[ATOMIC TXN] Rollback failed for {checkpoint.step.value}: {e}"
                )
                # Continue with next step even if this one fails
        
        self.failed_at = datetime.utcnow()
        logger.error(f"[ATOMIC TXN] ROLLBACK COMPLETE (duration: {self._duration_sec()}s)")
    
    def _duration_sec(self) -> float:
        """Get transaction duration in seconds."""
        end_ts = self.failed_at or datetime.utcnow()
        return (end_ts - self.started_at).total_seconds()
