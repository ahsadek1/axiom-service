"""
order_confirmation_poller.py — EMERGENCY FIX for Stuck 'submitted' Orders

Root cause: Queue worker submits orders but never polls Alpaca to check if they filled.
Orders stuck in 'submitted' status forever until startup reconciliation closes them.

This poller:
1. Finds all 'submitted' orders in execution_queue
2. Polls Alpaca for their actual status (filled/cancelled/pending)
3. Updates DB accordingly
4. Creates open positions for filled orders

Deployed: 2026-06-03 08:00 ET (EMERGENCY)
Author: SOVEREIGN
"""

import logging
import sqlite3
import time
from datetime import datetime, timedelta
import pytz
from typing import Optional, Dict, List

ET = pytz.timezone("America/New_York")
logger = logging.getLogger("alpha_exec.confirmation_poller")


class OrderConfirmationPoller:
    """Poll Alpaca for order status and update DB."""
    
    def __init__(self, db_path: str, alpaca_client, logger_obj=None):
        self.db_path = db_path
        self.alpaca_client = alpaca_client
        self.logger = logger_obj or logger
        self.running = False
    
    def poll_submitted_orders(self) -> Dict[str, int]:
        """
        Poll all 'submitted' orders in execution_queue.
        Returns dict with counts: {filled, cancelled, pending, failed}
        """
        results = {"filled": 0, "cancelled": 0, "pending": 0, "failed": 0}
        
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()
            
            # Find all submitted orders
            cursor.execute("""
                SELECT id, ticker, order_id, created_at, pathway
                FROM execution_queue
                WHERE status = 'submitted'
                ORDER BY created_at ASC
            """)
            
            submitted_orders = cursor.fetchall()
            conn.close()
            
            if not submitted_orders:
                self.logger.info("No submitted orders to poll")
                return results
            
            self.logger.info(f"Polling {len(submitted_orders)} submitted orders from Alpaca")
            
            for queue_id, ticker, order_id, created_at, pathway in submitted_orders:
                try:
                    # Query Alpaca for this specific order
                    alpaca_order = self._get_order_from_alpaca(order_id)
                    
                    if not alpaca_order:
                        self.logger.warning(f"Queue {queue_id}: Order {order_id} not found in Alpaca")
                        self._mark_order_failed(queue_id, "Order not found in Alpaca (likely rejected)")
                        results["failed"] += 1
                        continue
                    
                    status = alpaca_order.get('status', '').lower()
                    
                    if status == 'filled':
                        self.logger.info(f"Queue {queue_id}: Order {order_id} FILLED in Alpaca")
                        self._mark_order_filled(queue_id, ticker, order_id, pathway, alpaca_order)
                        results["filled"] += 1
                    
                    elif status == 'canceled':
                        self.logger.warning(f"Queue {queue_id}: Order {order_id} CANCELLED in Alpaca")
                        self._mark_order_cancelled(queue_id, order_id)
                        results["cancelled"] += 1
                    
                    elif status in ['pending_new', 'accepted', 'partial_fill']:
                        self.logger.debug(f"Queue {queue_id}: Order {order_id} still PENDING ({status})")
                        results["pending"] += 1
                        
                        # Check if order is stale (>2 hours old) — if so, cancel it
                        created = datetime.fromisoformat(created_at)
                        age = datetime.now(ET) - created
                        if age > timedelta(hours=2):
                            self.logger.warning(f"Queue {queue_id}: Order {order_id} is {age.total_seconds()/3600:.1f}h old, canceling")
                            self._cancel_order_in_alpaca(order_id)
                            self._mark_order_cancelled(queue_id, order_id)
                            results["cancelled"] += 1
                            results["pending"] -= 1
                    
                    else:
                        self.logger.error(f"Queue {queue_id}: Unknown order status '{status}'")
                        results["failed"] += 1
                
                except Exception as e:
                    self.logger.error(f"Queue {queue_id}: Error polling order {order_id}: {e}")
                    results["failed"] += 1
                
                # Small delay between Alpaca queries to avoid rate limit
                time.sleep(0.1)
            
        except Exception as e:
            self.logger.error(f"Poll loop error: {e}", exc_info=True)
        
        return results
    
    def _get_order_from_alpaca(self, order_id: str) -> Optional[Dict]:
        """Query Alpaca for a specific order."""
        try:
            order = self.alpaca_client.get_order(order_id)
            return dict(order) if order else None
        except Exception as e:
            self.logger.debug(f"Could not fetch order {order_id} from Alpaca: {e}")
            return None
    
    def _cancel_order_in_alpaca(self, order_id: str) -> bool:
        """Cancel order in Alpaca."""
        try:
            self.alpaca_client.cancel_order(order_id)
            self.logger.info(f"Cancelled order {order_id} in Alpaca")
            return True
        except Exception as e:
            self.logger.warning(f"Could not cancel order {order_id}: {e}")
            return False
    
    def _mark_order_filled(self, queue_id: int, ticker: str, order_id: str, pathway: str, alpaca_order: Dict):
        """Mark order as filled and create position record."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()
            
            # Update execution_queue
            cursor.execute("""
                UPDATE execution_queue 
                SET status = 'filled', filled_at = ?
                WHERE id = ?
            """, (datetime.now(ET).isoformat(), queue_id))
            
            # Create active position record
            qty = alpaca_order.get('filled_qty', alpaca_order.get('qty', 1))
            filled_price = alpaca_order.get('filled_avg_price', 0)
            
            cursor.execute("""
                INSERT OR IGNORE INTO active_positions_v2
                (ticker, arena, client_order_id, status, strategy, direction,
                 allocated_usd, pathway, alpaca_order_id, filled_qty, avg_fill_price, opened_at)
                VALUES (?, 'alpha', ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker,
                f"queue-{queue_id}",
                'equity',  # pathway.startswith('ATG') and 'equity' or 'options',
                'long' if alpaca_order.get('side') == 'buy' else 'short',
                qty * filled_price if filled_price else 1000,  # position size USD
                pathway,
                order_id,
                qty,
                filled_price,
                datetime.now(ET).isoformat()
            ))
            
            conn.commit()
            conn.close()
            
            self.logger.info(
                f"Queue {queue_id}: Marked filled + created position record. "
                f"Ticker={ticker}, qty={qty}, price=${filled_price:.2f}"
            )
        except Exception as e:
            self.logger.error(f"Queue {queue_id}: Failed to mark as filled: {e}")
    
    def _mark_order_cancelled(self, queue_id: int, order_id: str):
        """Mark order as cancelled."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE execution_queue
                SET status = 'cancelled', error = ?
                WHERE id = ?
            """, ("Order cancelled in Alpaca (or timed out)", queue_id))
            
            conn.commit()
            conn.close()
            
            self.logger.info(f"Queue {queue_id}: Marked as cancelled")
        except Exception as e:
            self.logger.error(f"Queue {queue_id}: Failed to mark as cancelled: {e}")
    
    def _mark_order_failed(self, queue_id: int, reason: str):
        """Mark order as failed."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE execution_queue
                SET status = 'alpaca_failed', error = ?
                WHERE id = ?
            """, (reason, queue_id))
            
            conn.commit()
            conn.close()
            
            self.logger.info(f"Queue {queue_id}: Marked as failed ({reason})")
        except Exception as e:
            self.logger.error(f"Queue {queue_id}: Failed to mark as failed: {e}")


# ─── RUN STANDALONE ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from alpaca_client import get_alpaca_client
    from config import ALPHA_DB_PATH
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    logger.info("ORDER CONFIRMATION POLLER — EMERGENCY FIX (2026-06-03 08:00 ET)")
    logger.info(f"Database: {ALPHA_DB_PATH}")
    
    alpaca = get_alpaca_client()
    poller = OrderConfirmationPoller(ALPHA_DB_PATH, alpaca)
    
    results = poller.poll_submitted_orders()
    
    logger.info("=" * 80)
    logger.info(f"POLL RESULTS: Filled={results['filled']}, Cancelled={results['cancelled']}, Pending={results['pending']}, Failed={results['failed']}")
    logger.info("=" * 80)
    
    sys.exit(0)
