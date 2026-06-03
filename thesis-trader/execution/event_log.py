"""
event_log.py — Position Event Audit Trail
===========================================
Records every position state transition for forensics and audit.

Tables created:
  - position_events: immutable log of all state changes

Events logged:
  - CREATED: position entered system
  - ORDER_PLACED: order submitted
  - FILLED: one or both legs filled
  - PARTIAL: partial fill detected
  - RECONCILED: position verified against broker
  - EXITED: position closed
  - ERROR: any failure in lifecycle
"""
import logging
import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

log = logging.getLogger("thesis.event_log")
_ET = ZoneInfo("America/New_York")


def init_event_log_schema(db_path: str) -> None:
    """Initialize event log tables."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS position_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT,
            status TEXT,
            details TEXT,
            FOREIGN KEY(position_id) REFERENCES positions(position_id),
            INDEX idx_position_time (position_id, timestamp)
        );
    """)
    conn.commit()
    conn.close()
    log.info("Event log schema initialized")


def log_event(
    db_path: str,
    position_id: str,
    event_type: str,
    actor: str = "system",
    status: str = "OK",
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Log a position event atomically.
    
    Args:
        db_path: path to thesis_positions.db
        position_id: position ID
        event_type: "CREATED", "FILLED", "EXITED", "RECONCILED", "ERROR", etc.
        actor: "order_guard", "exit_monitor", "reconciler", etc.
        status: "OK", "WARN", "ERROR"
        details: event-specific JSON details
    
    Returns:
        True if logged successfully, False otherwise
    
    Guarantees:
        - Atomic write (SQLite transaction)
        - Immutable log (no updates)
        - Searchable by position_id + timestamp
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        timestamp = datetime.now(timezone.utc).isoformat()
        details_json = json.dumps(details or {})
        
        conn.execute("""
            INSERT INTO position_events
            (position_id, event_type, timestamp, actor, status, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            position_id,
            event_type,
            timestamp,
            actor,
            status,
            details_json,
        ))
        conn.commit()
        conn.close()
        
        log.info(
            "Event logged: %s/%s — %s [%s] — %s",
            position_id, event_type, actor, status, details_json[:50]
        )
        return True
    
    except Exception as exc:
        log.error("Failed to log event for %s: %s", position_id, exc)
        return False


def get_position_events(
    db_path: str,
    position_id: str,
) -> list[Dict[str, Any]]:
    """
    Retrieve all events for a position in chronological order.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM position_events
            WHERE position_id = ?
            ORDER BY timestamp ASC
        """, (position_id,)).fetchall()
        conn.close()
        
        return [dict(r) for r in rows]
    
    except Exception as exc:
        log.error("Failed to retrieve events for %s: %s", position_id, exc)
        return []


def get_position_timeline(
    db_path: str,
    position_id: str,
) -> str:
    """
    Get human-readable timeline of position lifecycle.
    """
    events = get_position_events(db_path, position_id)
    if not events:
        return f"No events for {position_id}"
    
    lines = [f"Position {position_id} — Event Timeline"]
    lines.append("=" * 60)
    
    for event in events:
        timestamp = event.get("timestamp", "?")[:16]  # YYYY-MM-DDTHH:MM
        event_type = event.get("event_type", "?")
        actor = event.get("actor", "?")
        status = event.get("status", "?")
        
        # Parse details
        try:
            details = json.loads(event.get("details", "{}"))
            detail_str = " — " + str(details)[:40] if details else ""
        except:
            detail_str = ""
        
        lines.append(
            f"  {timestamp} [{status:5s}] {event_type:15s} ({actor}){detail_str}"
        )
    
    return "\n".join(lines)


def get_recent_errors(
    db_path: str,
    minutes: int = 60,
) -> list[Dict[str, Any]]:
    """Get all ERROR events from last N minutes."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        
        rows = conn.execute("""
            SELECT * FROM position_events
            WHERE event_type = 'ERROR'
              AND datetime(timestamp) > datetime('now', ? || ' minutes')
            ORDER BY timestamp DESC
        """, (f"-{minutes}",)).fetchall()
        
        conn.close()
        return [dict(r) for r in rows]
    
    except Exception as exc:
        log.error("Failed to retrieve recent errors: %s", exc)
        return []


def export_position_audit(
    db_path: str,
    position_id: str,
) -> Dict[str, Any]:
    """
    Export complete audit record for a position.
    Includes position data + full event log.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        
        # Get position
        pos = conn.execute(
            "SELECT * FROM positions WHERE position_id = ?",
            (position_id,)
        ).fetchone()
        
        # Get events
        events = conn.execute(
            "SELECT * FROM position_events WHERE position_id = ? ORDER BY timestamp ASC",
            (position_id,)
        ).fetchall()
        
        conn.close()
        
        if not pos:
            return {"status": "NOT_FOUND", "position_id": position_id}
        
        return {
            "position": dict(pos),
            "events": [dict(e) for e in events],
            "timeline": get_position_timeline(db_path, position_id),
        }
    
    except Exception as exc:
        log.error("Failed to export audit for %s: %s", position_id, exc)
        return {"status": "ERROR", "error": str(exc)}
