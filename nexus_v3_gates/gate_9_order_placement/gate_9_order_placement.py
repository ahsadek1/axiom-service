"""
gate_9_order_placement.py — Order Placement Gate

Submits validated order envelopes from GATE 7 to Alpaca, monitors fills,
and records positions to SQLite. Returns verdict: PASS / CONDITIONAL / FAIL.

Key features:
- Idempotent submissions via client_order_id (safe retry)
- 3-minute fill polling with exponential backoff
- Direct SQLite position recording
- Partial fill escalation (≥70% triggers CONDITIONAL verdict)
- Network error recovery via order lookup
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger("gate_9.order_placement")


class OrderPlacementGate:
    """
    Submit and monitor execution of options orders via Alpaca.
    
    Args:
        alpaca_client: AlpacaClient instance for API calls.
        db_path: Path to SQLite database for position recording.
        logger: Optional logger instance.
        poll_interval: Seconds between fill polls (default 30s).
        poll_timeout: Total timeout for fill polling in seconds (default 180s).
        partial_fill_threshold: Fill % to trigger CONDITIONAL verdict (default 70).
    """
    
    def __init__(
        self,
        alpaca_client,
        db_path: str,
        logger_instance=None,
        poll_interval: int = 30,
        poll_timeout: int = 180,
        partial_fill_threshold: float = 0.70,
    ):
        self.alpaca = alpaca_client
        self.db_path = db_path
        self.logger = logger_instance or logger
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.partial_fill_threshold = partial_fill_threshold
        self._init_db()
    
    # ── Database Initialization ────────────────────────────────────────────
    
    def _init_db(self):
        """Initialize positions table if not exists."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Read and execute schema
            schema_sql = """
            CREATE TABLE IF NOT EXISTS positions (
              order_id TEXT PRIMARY KEY,
              client_order_id TEXT UNIQUE,
              underlying TEXT NOT NULL,
              strategy TEXT NOT NULL,
              legs_json TEXT,
              side TEXT,
              qty INT NOT NULL,
              fill_qty INT DEFAULT 0,
              status TEXT DEFAULT 'PENDING',
              entry_price REAL,
              entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              fill_price REAL,
              fill_time TIMESTAMP,
              macro_regime TEXT,
              risk_limit REAL,
              alpaca_order_id TEXT,
              verdict TEXT,
              notes TEXT,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
            cursor.execute(schema_sql)
            
            # Create indexes
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_underlying_status "
                "ON positions(underlying, status);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_created_at "
                "ON positions(created_at DESC);"
            )
            
            conn.commit()
            conn.close()
            self.logger.info(f"Initialized positions schema at {self.db_path}")
        except Exception as e:
            self.logger.error(f"Failed to initialize DB: {e}")
            raise
    
    # ── Order Submission ───────────────────────────────────────────────────
    
    def place_order(self, order_envelope: dict) -> Tuple[str, Optional[dict]]:
        """
        Submit order envelope to Alpaca.
        
        Args:
            order_envelope: Order dict with legs, qty, limit_price, etc.
        
        Returns:
            Tuple of (verdict, alpaca_order_dict)
            verdict: 'PASS' if submitted successfully, 'FAIL' if error
            alpaca_order_dict: Order response from Alpaca (or None on error)
        """
        order_id = order_envelope.get("order_id")
        contract_type = order_envelope.get("contract_type", "spread")
        legs = order_envelope.get("legs", [])
        qty = order_envelope.get("qty", 1)
        order_type = order_envelope.get("order_type", "limit")
        limit_price = order_envelope.get("limit_price")
        
        # Generate client_order_id for idempotency (max 48 chars)
        client_order_id = order_id[:48] if order_id else f"gate9-{int(time.time())}"
        
        try:
            if contract_type == "spread" and len(legs) > 1:
                # Multi-leg spread order
                self.logger.info(
                    f"Submitting spread order {order_id}: {len(legs)} legs, "
                    f"qty={qty}, limit={limit_price}"
                )
                alpaca_order = self.alpaca.place_spread_order(
                    legs=legs,
                    qty=qty,
                    order_type=order_type,
                    limit_debit=limit_price,
                    client_order_id=client_order_id,
                )
            else:
                # Single-leg order
                symbol = legs[0]["symbol"] if legs else order_envelope.get("symbol")
                side = order_envelope.get("side", "buy")
                self.logger.info(
                    f"Submitting single-leg order {order_id}: {symbol}, "
                    f"side={side}, qty={qty}, limit={limit_price}"
                )
                alpaca_order = self.alpaca.place_option_order(
                    contract_symbol=symbol,
                    qty=qty,
                    side=side,
                    order_type=order_type,
                    limit_price=limit_price,
                )
            
            self.logger.info(
                f"Order {order_id} submitted successfully. "
                f"Alpaca order_id: {alpaca_order.get('id')}"
            )
            return "PASS", alpaca_order
        
        except Exception as e:
            self.logger.error(f"Failed to submit order {order_id}: {e}")
            # Attempt recovery via client_order_id lookup
            existing = self.alpaca.get_order_by_client_id(client_order_id)
            if existing:
                self.logger.info(
                    f"Recovered existing order via COID {client_order_id}: {existing.get('id')}"
                )
                return "PASS", existing
            return "FAIL", None
    
    # ── Fill Polling ───────────────────────────────────────────────────────
    
    def poll_fills(
        self,
        order_id: str,
        alpaca_order_id: str,
        timeout_sec: Optional[int] = None,
    ) -> Tuple[str, Optional[dict]]:
        """
        Poll Alpaca for fill status until timeout or filled.
        
        Args:
            order_id: Our internal order_id (for logging).
            alpaca_order_id: Alpaca's order_id.
            timeout_sec: Override poll timeout (default from __init__).
        
        Returns:
            Tuple of (verdict, final_order_dict)
            verdict: 'PASS' (filled), 'CONDITIONAL' (partial), 'FAIL' (timeout/error)
        """
        timeout_sec = timeout_sec or self.poll_timeout
        start_time = time.time()
        poll_count = 0
        
        while time.time() - start_time < timeout_sec:
            poll_count += 1
            try:
                order = self.alpaca.get_order(alpaca_order_id)
                if not order:
                    self.logger.warning(f"Order {alpaca_order_id} not found on poll #{poll_count}")
                    time.sleep(self.poll_interval)
                    continue
                
                status = order.get("status")
                filled_qty = float(order.get("filled_qty", 0))
                qty = float(order.get("qty", 0))
                fill_pct = (filled_qty / qty * 100) if qty > 0 else 0
                
                self.logger.debug(
                    f"Poll #{poll_count} for {order_id}: status={status}, "
                    f"filled={filled_qty}/{qty} ({fill_pct:.1f}%)"
                )
                
                # Filled completely
                if status == "filled":
                    self.logger.info(
                        f"Order {order_id} filled completely. "
                        f"Filled qty: {filled_qty}, Price: {order.get('filled_avg_price')}"
                    )
                    return "PASS", order
                
                # Partially filled (≥threshold)
                if status == "partially_filled" and fill_pct >= (self.partial_fill_threshold * 100):
                    self.logger.warning(
                        f"Order {order_id} partially filled ({fill_pct:.1f}%). "
                        f"Escalating to CONDITIONAL."
                    )
                    return "CONDITIONAL", order
                
                # Cancelled or expired
                if status in ["cancelled", "expired"]:
                    self.logger.warning(f"Order {order_id} {status}. Filled: {filled_qty}/{qty}")
                    return "FAIL", order
                
            except Exception as e:
                self.logger.warning(f"Poll error for {order_id} (#{poll_count}): {e}")
            
            # Sleep before next poll
            time.sleep(self.poll_interval)
        
        # Timeout reached
        self.logger.error(
            f"Order {order_id} poll timeout after {poll_count} attempts ({timeout_sec}s). "
            f"Attempting to cancel."
        )
        try:
            self.alpaca.cancel_order_safe(alpaca_order_id)
        except Exception as e:
            self.logger.error(f"Failed to cancel {alpaca_order_id}: {e}")
        
        return "FAIL", None
    
    # ── Position Recording ─────────────────────────────────────────────────
    
    def record_position(
        self,
        order_envelope: dict,
        alpaca_order: Optional[dict],
        verdict: str,
    ) -> bool:
        """
        Record position to SQLite positions table.
        
        Args:
            order_envelope: Original order dict from GATE 7.
            alpaca_order: Order response from Alpaca (may be None on failure).
            verdict: PASS, CONDITIONAL, or FAIL.
        
        Returns:
            True if recorded successfully, False otherwise.
        """
        try:
            order_id = order_envelope.get("order_id")
            client_order_id = order_id[:48] if order_id else None
            
            # Extract fill info from alpaca_order if available
            fill_qty = 0
            fill_price = None
            fill_time = None
            alpaca_order_id = None
            status = "PENDING" if verdict == "PASS" else "FAILED"
            
            if alpaca_order:
                alpaca_order_id = alpaca_order.get("id")
                fill_qty = int(alpaca_order.get("filled_qty", 0))
                fill_price = float(alpaca_order.get("filled_avg_price", 0)) or None
                fill_time = alpaca_order.get("filled_at")
                order_status = alpaca_order.get("status", "unknown")
                
                if order_status == "filled":
                    status = "FILLED"
                elif order_status == "partially_filled":
                    status = "PARTIAL"
                elif verdict == "FAIL":
                    status = "FAILED"
            
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Upsert: insert or update if order_id already exists
            cursor.execute(
                """
                INSERT OR REPLACE INTO positions (
                  order_id, client_order_id, underlying, strategy, legs_json,
                  side, qty, fill_qty, status, entry_price, entry_time,
                  fill_price, fill_time, macro_regime, risk_limit,
                  alpaca_order_id, verdict, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    client_order_id,
                    order_envelope.get("underlying"),
                    order_envelope.get("strategy"),
                    json.dumps(order_envelope.get("legs", [])),
                    order_envelope.get("side"),
                    order_envelope.get("qty", 1),
                    fill_qty,
                    status,
                    order_envelope.get("entry_price"),
                    order_envelope.get("submission_time"),
                    fill_price,
                    fill_time,
                    order_envelope.get("macro_regime"),
                    order_envelope.get("risk_per_position"),
                    alpaca_order_id,
                    verdict,
                    f"Recorded at {datetime.utcnow().isoformat()}",
                    datetime.utcnow().isoformat(),
                ),
            )
            
            conn.commit()
            conn.close()
            
            self.logger.info(
                f"Position recorded: {order_id} (status={status}, verdict={verdict})"
            )
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to record position {order_envelope.get('order_id')}: {e}")
            return False
    
    # ── Main Execution Flow ────────────────────────────────────────────────
    
    def execute(self, order_envelope: dict) -> dict:
        """
        Main gate execution: submit → poll → record → return verdict.
        
        Args:
            order_envelope: Order dict from GATE 7 with all required fields.
        
        Returns:
            Gate verdict dict:
            {
              'verdict': 'PASS' | 'CONDITIONAL' | 'FAIL',
              'order_id': order_id,
              'alpaca_order_id': alpaca_order_id or None,
              'fill_qty': int,
              'fill_price': float or None,
              'error': error message if FAIL,
              'timestamp': ISO timestamp
            }
        """
        order_id = order_envelope.get("order_id", "unknown")
        
        try:
            # Step 1: Submit order
            submit_verdict, alpaca_order = self.place_order(order_envelope)
            if submit_verdict == "FAIL":
                self.record_position(order_envelope, None, "FAIL")
                return {
                    "verdict": "FAIL",
                    "order_id": order_id,
                    "alpaca_order_id": None,
                    "fill_qty": 0,
                    "fill_price": None,
                    "error": "Order submission failed",
                    "timestamp": datetime.utcnow().isoformat(),
                }
            
            alpaca_order_id = alpaca_order.get("id")
            
            # Step 2: Poll fills
            poll_verdict, final_order = self.poll_fills(order_id, alpaca_order_id)
            
            # Step 3: Record position
            self.record_position(order_envelope, final_order, poll_verdict)
            
            # Step 4: Build and return verdict
            fill_qty = 0
            fill_price = None
            if final_order:
                fill_qty = int(final_order.get("filled_qty", 0))
                fill_price = final_order.get("filled_avg_price")
            
            return {
                "verdict": poll_verdict,
                "order_id": order_id,
                "alpaca_order_id": alpaca_order_id,
                "fill_qty": fill_qty,
                "fill_price": fill_price,
                "error": None if poll_verdict in ["PASS", "CONDITIONAL"] else "Order unfilled after polling",
                "timestamp": datetime.utcnow().isoformat(),
            }
        
        except Exception as e:
            self.logger.error(f"Unhandled error in execute for {order_id}: {e}", exc_info=True)
            self.record_position(order_envelope, None, "FAIL")
            return {
                "verdict": "FAIL",
                "order_id": order_id,
                "alpaca_order_id": None,
                "fill_qty": 0,
                "fill_price": None,
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


# ── Verdict Mapper ─────────────────────────────────────────────────────

def verdict_to_status(verdict: str) -> str:
    """
    Map gate verdict to human-readable status.
    
    Returns:
        'approved' for PASS, 'escalate' for CONDITIONAL, 'rejected' for FAIL
    """
    if verdict == "PASS":
        return "approved"
    elif verdict == "CONDITIONAL":
        return "escalate"
    else:
        return "rejected"
