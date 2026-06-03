"""
partial_fill_tracker.py — Multi-Fill State Machine

VAR#2 FIX: Track partial fills properly across multiple fill events.
Prevents silent position drift from incomplete fill tracking.

Schema additions:
  CREATE TABLE IF NOT EXISTS position_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    fill_qty INTEGER NOT NULL,
    fill_price REAL,
    fill_time TEXT NOT NULL,
    sequence INTEGER NOT NULL  -- 1st fill, 2nd fill, etc.
  );
  
  ALTER TABLE positions ADD COLUMN filled_qty INTEGER DEFAULT 0;
  ALTER TABLE positions ADD COLUMN remaining_qty INTEGER DEFAULT 0;
  ALTER TABLE positions ADD COLUMN avg_fill_price REAL DEFAULT 0.0;
"""

import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FillEvent:
    """Single partial fill event."""
    order_id: str
    ticker: str
    fill_qty: int
    fill_price: float
    fill_time: str  # ISO format timestamp
    sequence: int  # 1st, 2nd, 3rd fill
    cumulative_qty: int  # Total filled so far


class PartialFillTracker:
    """
    Track partial fills for orders that execute in multiple pieces.
    
    Example:
        tracker = PartialFillTracker(db_path)
        
        # Record first fill
        tracker.record_fill(
            order_id="ord_123",
            ticker="NVDA",
            qty=50,
            price=140.50
        )
        
        # Record second fill (30 minutes later)
        tracker.record_fill(
            order_id="ord_123",
            ticker="NVDA",
            qty=30,
            price=141.25
        )
        
        # Query current state
        state = tracker.get_fill_state("ord_123")
        # Returns: {filled_qty: 80, remaining_qty: 20, avg_price: 140.775}
    """
    
    def __init__(self, db_path: str):
        """Initialize fill tracking database."""
        self.db_path = db_path
        self._init_schema()
    
    def _init_schema(self):
        """Create fill tracking tables if not exist."""
        try:
            conn = sqlite3.connect(self.db_path)
            
            # Position fills table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    fill_qty INTEGER NOT NULL,
                    fill_price REAL,
                    fill_time TEXT NOT NULL,
                    sequence INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_order_id ON position_fills(order_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON position_fills(ticker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fill_time ON position_fills(fill_time)")
            
            # Add new columns to positions table if not exist
            conn.execute("""
                ALTER TABLE positions ADD COLUMN filled_qty INTEGER DEFAULT 0;
            """)
            conn.execute("""
                ALTER TABLE positions ADD COLUMN remaining_qty INTEGER DEFAULT 0;
            """)
            conn.execute("""
                ALTER TABLE positions ADD COLUMN avg_fill_price REAL DEFAULT 0.0;
            """)
            
            conn.commit()
            conn.close()
        
        except sqlite3.OperationalError:
            # Columns already exist
            pass
        except Exception as e:
            logger.error(f"Failed to initialize fill schema: {e}")
    
    def record_fill(
        self,
        order_id: str,
        ticker: str,
        qty: int,
        price: float,
        fill_time: Optional[str] = None
    ) -> FillEvent:
        """
        Record a fill event for an order (partial or full).
        
        Args:
            order_id: Alpaca order ID
            ticker: Stock ticker
            qty: Qty filled in this event
            price: Price of this fill
            fill_time: ISO timestamp (defaults to now)
        
        Returns:
            FillEvent with cumulative info
        """
        fill_time = fill_time or datetime.utcnow().isoformat()
        
        try:
            conn = sqlite3.connect(self.db_path)
            
            # Get current fill count for this order
            sequence = conn.execute("""
                SELECT COUNT(*) FROM position_fills WHERE order_id = ?
            """, (order_id,)).fetchone()[0] + 1
            
            # Insert fill event
            conn.execute("""
                INSERT INTO position_fills
                (order_id, ticker, fill_qty, fill_price, fill_time, sequence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (order_id, ticker, qty, price, fill_time, sequence))
            
            # Update position record with cumulative fill info
            self._update_position_fill_state(conn, order_id, ticker)
            
            conn.commit()
            conn.close()
            
            # Get current state for return
            fill_state = self.get_fill_state(order_id)
            
            logger.info(
                f"[FILL TRACKER] {order_id} fill #{sequence}: "
                f"{qty}@${price:.2f} | cumulative: {fill_state['filled_qty']}qty "
                f"@${fill_state.get('avg_price', 0):.2f} avg"
            )
            
            return FillEvent(
                order_id=order_id,
                ticker=ticker,
                fill_qty=qty,
                fill_price=price,
                fill_time=fill_time,
                sequence=sequence,
                cumulative_qty=fill_state['filled_qty']
            )
        
        except Exception as e:
            logger.error(f"Failed to record fill for {order_id}: {e}")
            raise
    
    def _update_position_fill_state(self, conn: sqlite3.Connection, order_id: str, ticker: str):
        """
        Update positions table with cumulative fill data.
        Called after every fill record.
        """
        # Get all fills for this order
        rows = conn.execute("""
            SELECT fill_qty, fill_price FROM position_fills
            WHERE order_id = ?
            ORDER BY sequence ASC
        """, (order_id,)).fetchall()
        
        if not rows:
            return
        
        # Calculate cumulative stats
        total_qty = sum(r[0] for r in rows)
        total_cost = sum(r[0] * r[1] for r in rows)
        avg_price = total_cost / total_qty if total_qty > 0 else 0.0
        
        # Get original order qty from positions table
        original_qty = conn.execute("""
            SELECT qty FROM positions WHERE order_id = ?
        """, (order_id,)).fetchone()
        
        if original_qty:
            original_qty = original_qty[0]
            remaining_qty = original_qty - total_qty
        else:
            remaining_qty = 0
        
        # Update position record
        conn.execute("""
            UPDATE positions
            SET filled_qty = ?, remaining_qty = ?, avg_fill_price = ?
            WHERE order_id = ?
        """, (total_qty, remaining_qty, avg_price, order_id))
    
    def get_fill_state(self, order_id: str) -> Dict[str, Any]:
        """
        Get current fill state for an order.
        
        Returns:
            {filled_qty, remaining_qty, avg_price, fill_count, fills: [...]}
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            # Get fill events
            fills = conn.execute("""
                SELECT order_id, ticker, fill_qty, fill_price, fill_time, sequence
                FROM position_fills
                WHERE order_id = ?
                ORDER BY sequence ASC
            """, (order_id,)).fetchall()
            
            if not fills:
                return {
                    "order_id": order_id,
                    "filled_qty": 0,
                    "remaining_qty": 0,
                    "avg_price": 0.0,
                    "fill_count": 0,
                    "fills": []
                }
            
            # Calculate cumulative
            total_qty = sum(f["fill_qty"] for f in fills)
            total_cost = sum(f["fill_qty"] * f["fill_price"] for f in fills)
            avg_price = total_cost / total_qty if total_qty > 0 else 0.0
            
            # Get original qty
            orig = conn.execute(
                "SELECT qty FROM positions WHERE order_id = ?",
                (order_id,)
            ).fetchone()
            original_qty = orig[0] if orig else total_qty
            remaining_qty = original_qty - total_qty
            
            conn.close()
            
            return {
                "order_id": order_id,
                "filled_qty": total_qty,
                "remaining_qty": remaining_qty,
                "avg_price": round(avg_price, 2),
                "fill_count": len(fills),
                "fills": [
                    {
                        "sequence": f["sequence"],
                        "qty": f["fill_qty"],
                        "price": f["fill_price"],
                        "time": f["fill_time"]
                    }
                    for f in fills
                ]
            }
        
        except Exception as e:
            logger.error(f"Failed to get fill state for {order_id}: {e}")
            return {}
    
    def is_fully_filled(self, order_id: str) -> bool:
        """Check if order is fully filled."""
        state = self.get_fill_state(order_id)
        return state.get("remaining_qty", 0) == 0
    
    def get_unfilled_orders(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all partially-filled orders.
        
        Args:
            ticker: Filter by ticker (optional)
        
        Returns:
            List of order IDs with remaining qty
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            query = """
                SELECT DISTINCT p.order_id, p.ticker, p.qty, 
                    COALESCE(SUM(pf.fill_qty), 0) as filled_qty,
                    p.qty - COALESCE(SUM(pf.fill_qty), 0) as remaining_qty
                FROM positions p
                LEFT JOIN position_fills pf ON p.order_id = pf.order_id
                WHERE p.status IN ('open', 'partial')
                GROUP BY p.order_id
                HAVING remaining_qty > 0
            """
            
            if ticker:
                query += f" AND p.ticker = '{ticker}'"
            
            query += " ORDER BY p.created_at DESC"
            
            rows = conn.execute(query).fetchall()
            conn.close()
            
            return [dict(r) for r in rows]
        
        except Exception as e:
            logger.error(f"Failed to get unfilled orders: {e}")
            return []
