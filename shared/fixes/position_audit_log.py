"""
position_audit_log.py — Immutable Position Audit Trail

VAR#6 FIX: Comprehensive audit log for every position lifecycle event.
Enables forensic analysis of orphans/ghosts without cross-service log parsing.

Schema:
  CREATE TABLE IF NOT EXISTS position_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    order_id TEXT,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    service TEXT,
    reason TEXT,
    timestamp TEXT NOT NULL,
    duration_sec FLOAT
  );
"""

import sqlite3
import logging
import json
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class AuditAction(Enum):
    """Position lifecycle events."""
    ALLOCATION_CREATED = "allocation_created"
    ALLOCATION_RELEASED = "allocation_released"
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_PARTIAL_FILL = "order_partial_fill"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_PARTIAL_CLOSE = "position_partial_close"
    DB_UPDATE_STARTED = "db_update_started"
    DB_UPDATE_SUCCEEDED = "db_update_succeeded"
    DB_UPDATE_FAILED = "db_update_failed"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    ORPHAN_DETECTED = "orphan_detected"
    ORPHAN_RECOVERED = "orphan_recovered"
    CLOSE_INITIATED = "close_initiated"
    CLOSE_CONFIRMED = "close_confirmed"


class PositionAuditLog:
    """
    Immutable audit log for position lifecycle.
    
    Usage:
        log = PositionAuditLog("/path/to/audit.db")
        log.record(
            position_id="pos_123",
            action=AuditAction.ORDER_PLACED,
            before_state="pending_order",
            after_state="order_placed",
            service="alpha-execution",
            reason="exit_rule_35_percent",
            extra={"order_id": "ord_456"}
        )
    """
    
    def __init__(self, db_path: str):
        """Initialize audit log database."""
        self.db_path = db_path
        self._init_schema()
    
    def _init_schema(self):
        """Create audit table if not exists."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    order_id TEXT,
                    ticker TEXT,
                    action TEXT NOT NULL,
                    before_state TEXT,
                    after_state TEXT,
                    service TEXT,
                    reason TEXT,
                    extra_data TEXT,
                    timestamp TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_position_id ON position_audit(position_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON position_audit(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON position_audit(action)")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to initialize audit schema: {e}")
    
    def record(
        self,
        position_id: str,
        action: AuditAction,
        before_state: Optional[str] = None,
        after_state: Optional[str] = None,
        service: Optional[str] = None,
        reason: Optional[str] = None,
        order_id: Optional[str] = None,
        ticker: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Record a position audit event.
        
        Args:
            position_id: Unique position identifier
            action: AuditAction enum
            before_state: State before this action (e.g., "open")
            after_state: State after this action (e.g., "closing")
            service: Service name that initiated action (e.g., "alpha-execution")
            reason: Why this action occurred (e.g., "exit_rule_35_percent")
            order_id: Alpaca order ID (if applicable)
            ticker: Stock ticker
            extra: Extra context dict
        """
        try:
            extra_json = json.dumps(extra or {})
            
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO position_audit
                (position_id, order_id, ticker, action, before_state, after_state,
                 service, reason, extra_data, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position_id,
                order_id,
                ticker,
                action.value,
                before_state,
                after_state,
                service,
                reason,
                extra_json,
                datetime.utcnow().isoformat()
            ))
            conn.commit()
            conn.close()
            
            logger.debug(
                f"[AUDIT] {position_id} {action.value} {before_state}→{after_state} ({reason})"
            )
        
        except Exception as e:
            logger.error(f"Failed to record audit event: {e}")
    
    def get_position_timeline(self, position_id: str) -> list:
        """
        Retrieve complete timeline for a position.
        
        Returns:
            List of dicts with {timestamp, action, before_state, after_state, reason, service, ticker}
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            rows = conn.execute("""
                SELECT 
                    timestamp, action, before_state, after_state, reason, service, ticker, extra_data
                FROM position_audit
                WHERE position_id = ?
                ORDER BY timestamp ASC
            """, (position_id,)).fetchall()
            
            conn.close()
            
            return [
                {
                    "timestamp": r["timestamp"],
                    "action": r["action"],
                    "before_state": r["before_state"],
                    "after_state": r["after_state"],
                    "reason": r["reason"],
                    "service": r["service"],
                    "ticker": r["ticker"],
                    "extra": json.loads(r["extra_data"] or "{}")
                }
                for r in rows
            ]
        
        except Exception as e:
            logger.error(f"Failed to retrieve position timeline: {e}")
            return []
    
    def find_orphans(self, time_window_hours: int = 24) -> list:
        """
        Find recent orphan detections in audit log.
        
        Returns:
            List of {position_id, first_orphan_detected, last_update, service}
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            # Find positions with ORPHAN_DETECTED but no subsequent ORPHAN_RECOVERED
            rows = conn.execute(f"""
                SELECT DISTINCT
                    position_id,
                    MIN(CASE WHEN action = 'orphan_detected' THEN timestamp END) as orphan_detected_at,
                    MAX(timestamp) as last_update,
                    service
                FROM position_audit
                WHERE 
                    action IN ('orphan_detected', 'orphan_recovered')
                    AND datetime(timestamp) > datetime('now', '-{time_window_hours} hours')
                GROUP BY position_id
                HAVING COUNT(CASE WHEN action = 'orphan_recovered' THEN 1 END) = 0
                ORDER BY orphan_detected_at DESC
            """).fetchall()
            
            conn.close()
            
            return [dict(r) for r in rows]
        
        except Exception as e:
            logger.error(f"Failed to find orphans: {e}")
            return []
    
    def find_sync_gaps(self, time_window_hours: int = 24) -> list:
        """
        Find reconciliation mismatches (sync gaps between DB and Alpaca).
        
        Returns:
            List of {position_id, mismatch_count, last_mismatch, ticker}
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            rows = conn.execute(f"""
                SELECT
                    position_id,
                    ticker,
                    COUNT(*) as mismatch_count,
                    MAX(timestamp) as last_mismatch
                FROM position_audit
                WHERE 
                    action = 'reconciliation_mismatch'
                    AND datetime(timestamp) > datetime('now', '-{time_window_hours} hours')
                GROUP BY position_id
                ORDER BY mismatch_count DESC
            """).fetchall()
            
            conn.close()
            
            return [dict(r) for r in rows]
        
        except Exception as e:
            logger.error(f"Failed to find sync gaps: {e}")
            return []
    
    def export_incident_context(self, position_id: str) -> Dict[str, Any]:
        """
        Export complete context for incident forensics.
        
        Returns:
            Dict with timeline, sync gaps, orphan status, etc.
        """
        timeline = self.get_position_timeline(position_id)
        
        # Determine if position is orphaned
        is_orphaned = False
        orphan_since = None
        for event in reversed(timeline):
            if event["action"] == AuditAction.ORPHAN_DETECTED.value:
                is_orphaned = True
                orphan_since = event["timestamp"]
                break
            elif event["action"] == AuditAction.ORPHAN_RECOVERED.value:
                is_orphaned = False
                break
        
        return {
            "position_id": position_id,
            "timeline": timeline,
            "is_orphaned": is_orphaned,
            "orphan_since": orphan_since,
            "total_events": len(timeline),
            "first_event": timeline[0] if timeline else None,
            "last_event": timeline[-1] if timeline else None,
        }
