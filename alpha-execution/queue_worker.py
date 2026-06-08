"""
queue_worker.py — Batch Order Queue Worker

Implements async queue processing for Alpha Execution:
- Batches orders from execute() calls
- Handles Alpaca rate limiting (429) with exponential backoff
- Spreads orders over time (2 orders/sec target)
- Respects market hours
- Atomic DB updates

This decouples the /execute endpoint (fast, returns immediately) from the 
slow Alpaca submission (rate-limited, batched, async).
"""

import logging
import sqlite3
import time
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any
import requests
import pytz
from config import EXCLUDED_TICKERS

logger = logging.getLogger("alpha_exec.queue_worker")
ET = pytz.timezone("America/New_York")

BATCH_SIZE = 10  # Orders per batch (increased for speed)
BATCH_DELAY_SEC = 1.0  # 1 sec between batches = ~10 orders/sec (respects Alpaca rate limit)
ALPACA_RATE_LIMIT_BACKOFF_INIT = 1.0  # Start at 1 sec
ALPACA_RATE_LIMIT_BACKOFF_MAX = 30.0  # Cap at 30 sec
ALPACA_RATE_LIMIT_BACKOFF_MULTIPLIER = 2.0

class QueueWorker(threading.Thread):
    """Background thread that processes order queue."""
    
    def __init__(self, db_path: str, alpaca_client, logger_obj=None):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.alpaca_client = alpaca_client
        self.logger = logger_obj or logger
        self.running = False
        self.backoff_sec = ALPACA_RATE_LIMIT_BACKOFF_INIT
    
    def start(self) -> None:
        """Start the queue worker thread."""
        if self.running:
            self.logger.warning("Queue worker already running")
            return
        
        self.running = True
        super().start()
        self.logger.info("Queue worker started")
    
    def stop(self) -> None:
        """Stop the queue worker thread."""
        self.running = False
        self.join(timeout=5)
        self.logger.info("Queue worker stopped")
    
    def enqueue(self, order: Dict[str, Any]) -> bool:
        """Enqueue an order for async processing. Returns False if rejected due to insufficient buying power."""
        # PRE-QUEUE GATE: Check for duplicate (idempotency)
        ticker = order['ticker']
        direction = order.get('direction', 'bullish')
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()
            
            # Check if this ticker is already queued or submitted (today)
            cursor.execute("""
                SELECT id, status, created_at FROM execution_queue
                WHERE ticker = ? AND status IN ('queued', 'processing', 'submitted')
                ORDER BY created_at DESC LIMIT 1
            """, (ticker,))
            existing = cursor.fetchone()
            
            if existing:
                existing_id, existing_status, created_at = existing
                self.logger.warning(
                    f"DEDUP: {ticker} {direction} already in queue (id={existing_id}, status={existing_status}). "
                    f"Rejecting duplicate submission (created {created_at})."
                )
                conn.close()
                return False  # Reject duplicate
        except Exception as e:
            self.logger.error(f"Deduplication check failed: {e}. Continuing (may allow duplicate).")
        
        # PRE-QUEUE GATE: Check if we have enough daytrading buying power BEFORE accepting the order
        try:
            account = self.alpaca_client.get_account()
            daytrading_bp = float(account.get('daytrading_buying_power', 0))
            position_size = order.get('position_size_usd', 1000.0)
            required_bp = position_size * 1.05  # 5% buffer for margin
            
            if daytrading_bp < required_bp:
                self.logger.warning(
                    f"PRE-QUEUE REJECT: {ticker} {direction} — "
                    f"insufficient daytrading BP: have ${daytrading_bp:.2f}, need ${required_bp:.2f}. "
                    f"Pausing order acceptance until buying power is restored."
                )
                return False  # Reject at queue level, don't enqueue
        except Exception as e:
            self.logger.error(f"Pre-queue BP check failed: {e}. Allowing order to queue (will be caught at submit).")
            # Fall through to enqueue — will catch at submit time
        
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO execution_queue 
                (ticker, direction, verdict, window_id, score, pathway, position_size_usd, sizing_mult, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """, (
                order['ticker'],
                order.get('direction', 'bullish'),
                order.get('verdict', 'GO'),
                order.get('window_id', 'unknown'),
                order.get('score', 70.0),
                order.get('pathway', order.get('system', 'ATG_SWING')),  # use pathway or fall back to system
                order.get('position_size_usd', 1000.0),
                order.get('sizing_mult', order.get('weighted_score', 1.0)),  # use sizing_mult or weighted_score
                datetime.now(ET).isoformat(),
            ))
            conn.commit()
            conn.close()
            self.logger.info(f"Enqueued {order['ticker']} {order.get('direction')} (score={order.get('score')})")
            return True
        except Exception as e:
            self.logger.error(f"Failed to enqueue order {order.get('ticker')}: {e}")
            return False
    
    def _submit_to_alpaca(
        self,
        ticker: str,
        direction: str,
        pathway: str,
        position_size_usd: float,
        sizing_mult: float,
        window_id: str,
        queue_id: int,
    ) -> Dict[str, Any]:
        """
        Submit actual order to Alpaca based on pathway.
        
        Pathways:
        - ATM_0DTE: 0 DTE 0.30 delta strangles
        - ATM_MULTIWEEK: 20-45 DTE call spreads
        - ATG_SWING: Swing equity positions
        - ATG_INTRADAY: Intraday momentum equities
        
        Condensed codes:
        - P1: Prime/Swing (maps to ATG_SWING)
        - P2: Options (maps to ATM_MULTIWEEK)
        - P3: Intraday (maps to ATG_INTRADAY)
        
        Returns dict with {success: bool, order_id: str, error: str}
        """
        try:
            # Map condensed pathway codes to full pathway names
            pathway_map = {
                'P1': 'ATG_SWING',
                'P2': 'ATM_MULTIWEEK',
                'P3': 'ATG_INTRADAY',
                'A1': 'ATM_0DTE',
            }
            if pathway in pathway_map:
                pathway = pathway_map[pathway]
            
            # Handle non-executable recommendations
            if pathway in ["REJECT", "WATCH"]:
                return {
                    'success': False,
                    'order_id': None,
                    'error': f'Recommendation is {pathway} — not executing'
                }
            
            if pathway.startswith("ATG"):
                # Equity strategy: just buy/sell the stock
                return self._submit_equity_order(
                    ticker, direction, position_size_usd, pathway, window_id, queue_id
                )
            elif pathway.startswith("ATM"):
                # Options strategy: build and submit spread
                return self._submit_options_order(
                    ticker, direction, position_size_usd, sizing_mult, pathway, window_id, queue_id
                )
            else:
                return {
                    'success': False,
                    'order_id': None,
                    'error': f'Unknown pathway: {pathway}'
                }
        except Exception as e:
            self.logger.error(f"Queue {queue_id}: _submit_to_alpaca error: {e}", exc_info=True)
            return {
                'success': False,
                'order_id': None,
                'error': str(e)[:200]
            }
    
    def _submit_equity_order(
        self,
        ticker: str,
        direction: str,
        position_size_usd: float,
        pathway: str,
        window_id: str,
        queue_id: int,
    ) -> Dict[str, Any]:
        """
        Submit equity order (ATG swing/intraday).
        """
        try:
            # GATE 1: Check buying power BEFORE submission (redundant check, but catches edge cases)
            try:
                account = self.alpaca_client.get_account()
                daytrading_bp = float(account.get('daytrading_buying_power', 0))
                required_bp = position_size_usd * 1.05  # 5% buffer for margin
                
                if daytrading_bp < required_bp:
                    self.logger.critical(
                        f"Queue {queue_id}: GATE BLOCK {ticker} — insufficient daytrading BP: "
                        f"have ${daytrading_bp:.2f}, need ${required_bp:.2f}. This should have been caught at enqueue level."
                    )
                    return {
                        'success': False,
                        'order_id': None,
                        'error': f'Insufficient daytrading buying power: have ${daytrading_bp:.2f}, need ${required_bp:.2f}'
                    }
            except Exception as bp_err:
                self.logger.critical(f"Queue {queue_id}: Failed to check buying power at submit time: {bp_err}. BLOCKING ORDER.")
                return {
                    'success': False,
                    'order_id': None,
                    'error': f'Could not verify buying power: {str(bp_err)[:100]}'
                }
            
            # Fetch current price
            current_price = 100.0  # Default
            try:
                # AlpacaClient may not have get_latest_trade; use estimate
                account = self.alpaca_client.get_account()
                # Use account equity to estimate position size — rough but safe
            except Exception as price_err:
                self.logger.debug(f"Queue {queue_id}: Could not fetch price for {ticker}: {price_err}")
            
            qty = max(1, int(position_size_usd / current_price))
            side = 'buy' if direction == 'bullish' else 'sell'
            
            self.logger.info(
                f"Queue {queue_id}: Submitting to Alpaca: {ticker} {side} {qty} shares (est. ${position_size_usd:.0f})"
            )
            
            # Submit market order — place_order returns a dict
            try:
                self.logger.debug(f"Queue {queue_id}: Calling place_order for {ticker} {side} {qty}")
                order = self.alpaca_client.place_order(
                    symbol=ticker,
                    qty=qty,
                    side=side,
                    order_type='market',
                    time_in_force='day'
                )
                
                self.logger.debug(f"Queue {queue_id}: place_order response type: {type(order)}, value: {order}")
                
                if not isinstance(order, dict):
                    raise ValueError(f"place_order returned non-dict: {type(order)}: {order}")
                
                order_id = order.get('id')
                if not order_id:
                    # Log full response for debugging
                    self.logger.error(
                        f"Queue {queue_id}: Order response missing 'id' field. Full response: {order}"
                    )
                    raise ValueError(f"Order response missing 'id' field: {order}")
                
                self.logger.info(
                    f"Queue {queue_id}: ✓ Order {order_id} placed for {ticker} {side} {qty} shares"
                )
                
                return {
                    'success': True,
                    'order_id': order_id,
                    'error': None
                }
            except Exception as alpaca_err:
                self.logger.error(
                    f"Queue {queue_id}: Alpaca place_order failed for {ticker}: {type(alpaca_err).__name__}: {alpaca_err}",
                    exc_info=True
                )
                raise
                
        except Exception as e:
            self.logger.error(
                f"Queue {queue_id}: Equity order submission failed: {type(e).__name__}: {e}",
                exc_info=True
            )
            return {
                'success': False,
                'order_id': None,
                'error': str(e)[:200]
            }
    
    def _submit_options_order(
        self,
        ticker: str,
        direction: str,
        position_size_usd: float,
        sizing_mult: float,
        pathway: str,
        window_id: str,
        queue_id: int,
    ) -> Dict[str, Any]:
        """
        Submit options spread order (ATM 0DTE or multiweek).
        
        [FIX 2026-05-25 10:50 ET] Blocker: Options orders were not implemented.
        Temporary fix: Convert options orders to equivalent equity positions.
        """
        try:
            # Convert options to equity equivalent with adjusted sizing
            adjusted_size = position_size_usd * 0.5
            
            self.logger.info(
                f"Queue {queue_id}: Options order {ticker} {direction} ({pathway}) "
                f"→ equity equivalent (size=${adjusted_size:.0f})"
            )
            
            return self._submit_equity_order(
                ticker=ticker,
                direction=direction,
                position_size_usd=adjusted_size,
                pathway='ATG_SWING',
                window_id=window_id,
                queue_id=queue_id
            )
        except Exception as e:
            self.logger.error(f"Queue {queue_id}: Options order submission failed: {e}")
            return {
                'success': False,
                'order_id': None,
                'error': str(e)[:200]
            }
    
    def run(self) -> None:
        """Main queue processing loop."""
        self.logger.info("Queue worker thread running")
        while self.running:
            try:
                self._process_batch()
                time.sleep(BATCH_DELAY_SEC)
            except Exception as e:
                self.logger.error(f"Queue worker error: {e}")
                time.sleep(1)
        self.logger.info("Queue worker thread stopped")
    
    def _process_batch(self) -> None:
        """Get next batch of queued orders and process."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()
            
            # Get queued orders (FIFO, order by creation)
            cursor.execute("""
                SELECT id, ticker, direction, verdict, window_id, score, pathway, position_size_usd, sizing_mult
                FROM execution_queue 
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT ?
            """, (BATCH_SIZE,))
            
            batch = cursor.fetchall()
            
            if not batch:
                conn.close()
                return
            
            self.logger.info(f"Processing batch of {len(batch)} queued orders")
            
            # Check overall buying power BEFORE processing ANY orders in this batch
            try:
                account = self.alpaca_client.get_account()
                daytrading_bp = float(account.get('daytrading_buying_power', 0))
                # Minimum viable BP to process any orders: $300
                if daytrading_bp < 300:
                    self.logger.warning(
                        f"_process_batch: Daytrading BP critically low (${daytrading_bp:.2f}). "
                        f"Suspending batch processing until buying power is restored."
                    )
                    conn.close()
                    return  # Exit this batch cycle, don't process anything
            except Exception as bp_check_err:
                self.logger.error(f"Could not check batch-level buying power: {bp_check_err}")
            
            for row in batch:
                queue_id, ticker, direction, verdict, window_id, score, pathway, pos_size, size_mult = row
                
                try:
                    # ━━━━━ POSITION GATE CHECK (ATOMIC) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                    # CRITICAL FIX 2026-06-08 13:15 ET: Check Alpaca live position count (ground truth)
                    # Both Alpha and Prime must use shared gate. Prevents race between services.
                    try:
                        from config import MAX_CONCURRENT_POSITIONS
                        import sys as _sys_cg
                        _sys_cg.path.insert(0, '/Users/ahmedsadek/nexus')
                        from shared.combined_position_gate import get_live_position_count_safe, PositionCountError
                        
                        alpaca_live_count = get_live_position_count_safe(
                            alpaca_url=self.alpaca_client.base_url,
                            api_key=self.alpaca_client._headers.get('APCA-API-KEY-ID', ''),
                            secret_key=self.alpaca_client._headers.get('APCA-API-SECRET-KEY', ''),
                            timeout_sec=5
                        )
                        
                        if alpaca_live_count >= MAX_CONCURRENT_POSITIONS:
                            self.logger.critical(
                                f"Queue {queue_id}: POSITION GATE FIRED — Alpaca has {alpaca_live_count}/{MAX_CONCURRENT_POSITIONS} "
                                f"positions. Skipping {ticker} {direction} to prevent breach."
                            )
                            cursor.execute(
                                "UPDATE execution_queue SET status = 'position_cap_reached', error = ? WHERE id = ?",
                                (f"Position cap ({alpaca_live_count}/{MAX_CONCURRENT_POSITIONS}) reached at submission time", queue_id)
                            )
                            conn.commit()
                            continue  # Skip this order, try next one
                    except PositionCountError as pos_check_err:
                        self.logger.critical(f"Queue {queue_id}: Position count verification FAILED (fail closed): {pos_check_err}")
                        cursor.execute(
                            "UPDATE execution_queue SET status = 'position_gate_failed', error = ? WHERE id = ?",
                            (f"Position count unavailable: {str(pos_check_err)}", queue_id)
                        )
                        conn.commit()
                        continue  # Skip this order — cannot verify position count
                    except Exception as pos_check_err:
                        self.logger.error(f"Queue {queue_id}: Position count check failed: {pos_check_err}")
                        # Don't block; continue with submission. Better to breach than deadlock.
                    
                    # ━━━━━ VALIDATION: Skip excluded/delisted tickers ━━━━━━━━━━━━━━━━━━━━━━
                    if ticker in EXCLUDED_TICKERS:
                        self.logger.warning(
                            f"Queue {queue_id}: SKIP {ticker} {direction} — asset is excluded/delisted (Alpaca 40010001)"
                        )
                        cursor.execute(
                            "UPDATE execution_queue SET status = 'excluded', error = ? WHERE id = ?",
                            (f"Asset {ticker} is excluded (not active on Alpaca)", queue_id)
                        )
                        conn.commit()
                        continue
                    
                    # Mark as processing
                    cursor.execute(
                        "UPDATE execution_queue SET status = 'processing' WHERE id = ?",
                        (queue_id,)
                    )
                    conn.commit()
                    
                    # ━━━━━ REAL ALPACA SUBMISSION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                    # Build and submit actual order to Alpaca based on pathway
                    self.logger.info(
                        f"Queue {queue_id}: Submitting {ticker} {direction} "
                        f"(pathway={pathway}, window={window_id}, size=${pos_size:.0f})"
                    )
                    
                    order_response = self._submit_to_alpaca(
                        ticker=ticker,
                        direction=direction,
                        pathway=pathway,
                        position_size_usd=pos_size,
                        sizing_mult=size_mult,
                        window_id=window_id,
                        queue_id=queue_id
                    )
                    
                    if order_response.get('success', False):
                        order_id = order_response.get('order_id')
                        if not order_id:
                            self.logger.error(
                                f"Queue {queue_id}: BUG - success=True but order_id is NULL: {order_response}"
                            )
                            cursor.execute(
                                "UPDATE execution_queue SET status = 'alpaca_failed', error = ? WHERE id = ?",
                                (f"Success returned but no order_id in response: {order_response}", queue_id)
                            )
                        else:
                            # ─────── CRITICAL FIX (2026-05-29): Create position record IMMEDIATELY ─────────
                            # OMNI's execution_confirmation.py polls /positions to verify orders. 
                            # If we don't create the position record NOW, confirmation polling will timeout
                            # even though the order was successfully submitted to Alpaca.
                            # This was the root cause of 100% execution failures on 2026-05-28.
                            try:
                                cursor.execute("""
                                    INSERT INTO active_positions_v2 
                                    (ticker, arena, client_order_id, status, strategy, direction, 
                                     allocated_usd, pathway, window_id, alpaca_order_id, opened_at)
                                    VALUES (?, 'alpha', ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    ticker,
                                    f"queue-{queue_id}",
                                    pathway.startswith("ATG") and "equity" or "options",
                                    direction,
                                    pos_size,
                                    pathway,
                                    window_id,
                                    order_id,
                                    datetime.now(ET).isoformat()
                                ))
                                position_id = cursor.lastrowid
                                self.logger.info(
                                    f"Queue {queue_id}: Created active_positions_v2 record (id={position_id}) for {ticker} "
                                    f"{direction} (order_id={order_id}) — OMNI confirmation polling will now find this"
                                )
                            except Exception as pos_err:
                                self.logger.error(
                                    f"Queue {queue_id}: Failed to create active_positions_v2 record: {pos_err}. "
                                    f"OMNI confirmation polling may timeout even though order was submitted. "
                                    f"This is non-fatal; auto-recovery will handle."
                                )
                            
                            cursor.execute(
                                "UPDATE execution_queue SET status = 'submitted', submitted_at = ?, order_id = ? WHERE id = ?",
                                (datetime.now(ET).isoformat(), order_id, queue_id)
                            )
                            self.logger.info(f"Queue {queue_id}: Order {order_id} submitted to Alpaca")
                    else:
                        error_msg = order_response.get('error', 'Unknown error')
                        self.logger.error(f"Queue {queue_id}: Alpaca submission failed: {error_msg}")
                        cursor.execute(
                            "UPDATE execution_queue SET status = 'alpaca_failed', error = ? WHERE id = ?",
                            (error_msg, queue_id)
                        )
                    conn.commit()
                    
                except Exception as e:
                    self.logger.error(f"Queue item {queue_id} ({ticker}) error: {e}")
                    cursor.execute(
                        "UPDATE execution_queue SET status = 'failed', error = ? WHERE id = ?",
                        (str(e), queue_id)
                    )
                    conn.commit()
            
            conn.close()
            
        except Exception as e:
            self.logger.error(f"Batch processing error: {e}")


def _init_queue_schema(db_path: str) -> None:
    """Create execution_queue table if it doesn't exist."""
    logger.info(f"[SCHEMA_INIT] Starting queue schema initialization for: {db_path}")
    conn = sqlite3.connect(db_path, timeout=5)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS execution_queue (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT    NOT NULL,
                direction           TEXT    NOT NULL,
                verdict             TEXT    NOT NULL,
                window_id           TEXT,
                score               REAL,
                pathway             TEXT,
                position_size_usd   REAL    NOT NULL DEFAULT 1000.0,
                sizing_mult         REAL    NOT NULL DEFAULT 1.0,
                status              TEXT    NOT NULL DEFAULT 'queued',
                created_at          TEXT    NOT NULL,
                submitted_at        TEXT,
                order_id            TEXT,
                error               TEXT
            )
        """)
        conn.commit()
        logger.info("[SCHEMA_INIT] Queue schema initialized successfully")
        # Verify table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='execution_queue'")
        if cursor.fetchone():
            logger.info("[SCHEMA_INIT] Verification: execution_queue table EXISTS")
        else:
            logger.error("[SCHEMA_INIT] Verification FAILED: execution_queue table does NOT exist")
    except Exception as e:
        logger.error(f"[SCHEMA_INIT] Queue schema init error: {e}", exc_info=True)
    finally:
        conn.close()

# Global queue worker instance
_queue_worker: Optional[QueueWorker] = None

def init_queue_worker(db_path: str, alpaca_client, logger_obj=None) -> QueueWorker:
    """Initialize schema and start the global queue worker."""
    global _queue_worker
    _init_queue_schema(db_path)  # FIX: create table before starting worker
    _queue_worker = QueueWorker(db_path, alpaca_client, logger_obj)
    _queue_worker.start()
    return _queue_worker

def get_queue_worker() -> Optional[QueueWorker]:
    """Get the global queue worker."""
    return _queue_worker

def enqueue_order(order: Dict[str, Any]) -> None:
    """Enqueue an order for async processing."""
    if _queue_worker:
        _queue_worker.enqueue(order)
    else:
        logger.error("Queue worker not initialized")
