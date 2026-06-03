"""
db_write_retry.py — Exponential Backoff Retry Logic for Database Writes

VAR#4 FIX: Prevents silent orphaning when DB writes fail.
Implements circuit-breaker pattern with exponential backoff.

Pattern:
    with DBWriteRetry(max_attempts=3, base_wait_sec=0.5) as retry:
        retry.execute(
            lambda: db.execute("UPDATE positions SET status=?", ("closed",)),
            operation_name="close_position"
        )
"""

import time
import logging
import sqlite3
from typing import Callable, Optional, Any
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


class RetryStrategy(Enum):
    """Retry backoff strategies."""
    EXPONENTIAL = "exponential"  # 0.5s, 1s, 2s, 4s, ...
    LINEAR = "linear"             # 0.5s, 1s, 1.5s, 2s, ...
    FIXED = "fixed"               # 0.5s, 0.5s, 0.5s, ...


class DBWriteRetry:
    """
    Context manager for database write operations with automatic retry.
    
    Usage:
        try:
            with DBWriteRetry(
                max_attempts=3,
                base_wait_sec=0.5,
                strategy=RetryStrategy.EXPONENTIAL
            ) as retry:
                retry.execute(
                    lambda: update_position_db(order_id),
                    operation_name="update_position"
                )
        except DBWriteError:
            logger.error("DB write failed after retries")
    """
    
    def __init__(
        self,
        max_attempts: int = 3,
        base_wait_sec: float = 0.5,
        strategy: RetryStrategy = RetryStrategy.EXPONENTIAL,
        jitter: bool = True
    ):
        """
        Initialize retry handler.
        
        Args:
            max_attempts: Max number of retry attempts (including first)
            base_wait_sec: Base wait duration in seconds
            strategy: Backoff strategy
            jitter: Add random jitter to prevent thundering herd
        """
        self.max_attempts = max_attempts
        self.base_wait_sec = base_wait_sec
        self.strategy = strategy
        self.jitter = jitter
        self.attempt = 0
        self.last_error = None
    
    def __enter__(self):
        """Enter context manager."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager."""
        return False
    
    def execute(
        self,
        operation: Callable[[], Any],
        operation_name: str = "db_write"
    ) -> Any:
        """
        Execute operation with automatic retry on failure.
        
        Args:
            operation: Callable that performs the DB operation
            operation_name: Human-readable name for logging
        
        Returns:
            Result of operation if successful
        
        Raises:
            DBWriteError if all attempts exhausted
        """
        for attempt in range(1, self.max_attempts + 1):
            self.attempt = attempt
            
            try:
                logger.debug(f"[RETRY] {operation_name} attempt {attempt}/{self.max_attempts}")
                result = operation()
                
                if attempt > 1:
                    logger.info(f"[RETRY] {operation_name} succeeded on attempt {attempt}")
                
                return result
            
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                self.last_error = e
                
                if attempt >= self.max_attempts:
                    logger.error(
                        f"[RETRY] {operation_name} FAILED after {self.max_attempts} attempts: {e}"
                    )
                    raise DBWriteError(
                        f"DB write failed: {operation_name}",
                        operation_name=operation_name,
                        attempts=self.max_attempts,
                        last_error=e
                    )
                
                # Calculate backoff duration
                wait_sec = self._calculate_backoff(attempt)
                logger.warning(
                    f"[RETRY] {operation_name} attempt {attempt} failed: {e} | "
                    f"retrying in {wait_sec:.2f}s"
                )
                time.sleep(wait_sec)
            
            except Exception as e:
                # Non-retryable error
                logger.error(f"[RETRY] {operation_name} non-retryable error: {e}")
                raise DBWriteError(
                    f"Non-retryable error: {operation_name}",
                    operation_name=operation_name,
                    attempts=attempt,
                    last_error=e
                )
    
    def _calculate_backoff(self, attempt: int) -> float:
        """
        Calculate wait duration before retry.
        
        Args:
            attempt: Current attempt number (1-indexed)
        
        Returns:
            Wait duration in seconds
        """
        if self.strategy == RetryStrategy.EXPONENTIAL:
            wait = self.base_wait_sec * (2 ** (attempt - 1))
        elif self.strategy == RetryStrategy.LINEAR:
            wait = self.base_wait_sec * attempt
        else:  # FIXED
            wait = self.base_wait_sec
        
        # Add jitter: ±25%
        if self.jitter:
            import random
            jitter_factor = 1.0 + random.uniform(-0.25, 0.25)
            wait *= jitter_factor
        
        return wait


class DBWriteError(Exception):
    """Raised when database write fails after all retries."""
    
    def __init__(
        self,
        message: str,
        operation_name: str,
        attempts: int,
        last_error: Optional[Exception] = None
    ):
        super().__init__(message)
        self.operation_name = operation_name
        self.attempts = attempts
        self.last_error = last_error
        self.timestamp = datetime.utcnow().isoformat()


class DeadLetterQueue:
    """
    Queue for failed DB operations. Allows manual retry or forensics.
    
    Used when DBWriteRetry exhausts all attempts — store operation intent
    for later retry instead of losing the update.
    
    Example:
        dlq = DeadLetterQueue("/path/to/dlq.db")
        dlq.enqueue(
            operation="close_position",
            context={"order_id": "ord_123", "status": "closed"},
            error="database locked"
        )
        
        # Later: process failed operations
        for item in dlq.get_all():
            retry_result = retry_operation(item)
            dlq.mark_processed(item["id"])
    """
    
    def __init__(self, db_path: str):
        """Initialize DLQ database."""
        self.db_path = db_path
        self._init_schema()
    
    def _init_schema(self):
        """Create DLQ table if not exist."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    context TEXT NOT NULL,
                    error TEXT,
                    enqueued_at TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    last_attempt_at TEXT,
                    processed INTEGER DEFAULT 0,
                    processed_at TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_operation ON dead_letter_queue(operation)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_processed ON dead_letter_queue(processed)")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to initialize DLQ: {e}")
    
    def enqueue(
        self,
        operation: str,
        context: dict,
        error: str = ""
    ) -> int:
        """
        Enqueue a failed operation for later retry.
        
        Args:
            operation: Operation name (e.g., "close_position")
            context: Context dict with operation parameters
            error: Error message from the failure
        
        Returns:
            DLQ item ID
        """
        import json
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("""
                INSERT INTO dead_letter_queue
                (operation, context, error, enqueued_at)
                VALUES (?, ?, ?, ?)
            """, (
                operation,
                json.dumps(context),
                error,
                datetime.utcnow().isoformat()
            ))
            conn.commit()
            dlq_id = cursor.lastrowid
            conn.close()
            
            logger.warning(
                f"[DLQ] Enqueued {operation} (id={dlq_id}): {error}"
            )
            return dlq_id
        
        except Exception as e:
            logger.error(f"Failed to enqueue DLQ item: {e}")
            raise
    
    def get_all(self, include_processed: bool = False) -> list:
        """
        Get all pending DLQ items.
        
        Returns:
            List of DLQ items
        """
        import json
        
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            query = "SELECT * FROM dead_letter_queue WHERE processed = 0"
            if include_processed:
                query = "SELECT * FROM dead_letter_queue"
            
            rows = conn.execute(query).fetchall()
            conn.close()
            
            return [
                {
                    "id": r["id"],
                    "operation": r["operation"],
                    "context": json.loads(r["context"]),
                    "error": r["error"],
                    "enqueued_at": r["enqueued_at"],
                    "attempts": r["attempts"],
                    "processed": bool(r["processed"])
                }
                for r in rows
            ]
        
        except Exception as e:
            logger.error(f"Failed to get DLQ items: {e}")
            return []
    
    def mark_processed(self, dlq_id: int) -> None:
        """Mark a DLQ item as successfully processed."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE dead_letter_queue SET processed=1, processed_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), dlq_id)
            )
            conn.commit()
            conn.close()
            
            logger.info(f"[DLQ] Marked item {dlq_id} as processed")
        
        except Exception as e:
            logger.error(f"Failed to mark DLQ item processed: {e}")
    
    def increment_attempts(self, dlq_id: int) -> None:
        """Increment retry attempt counter."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                UPDATE dead_letter_queue 
                SET attempts = attempts + 1, last_attempt_at = ?
                WHERE id = ?
            """, (datetime.utcnow().isoformat(), dlq_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to increment DLQ attempts: {e}")
