"""
FallbackChronicleClient — Local SQLite fallback for THESIS persistence.

When the CHRONICLE database on Mac Mini 2 (.42) is unreachable, THESIS
writes to this local fallback database instead. On CHRONICLE recovery,
the ResilientChronicleClient syncs all unsynced fallback rows back.

Schema mirrors CHRONICLE's thesis tables with an added `synced` column.
Every public method catches all exceptions and returns safe sentinels.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

from models import ThesisContext, ThesisEvent

logger = logging.getLogger(__name__)

_DEFAULT_FALLBACK_PATH = "/Users/ahs/.openclaw/workspaceTHESIS/thesis_fallback.db"

# ---------------------------------------------------------------------------
# DDL for fallback schema (same as CHRONICLE + synced column)
# ---------------------------------------------------------------------------

_CREATE_FALLBACK_CONTEXT = """
CREATE TABLE IF NOT EXISTS thesis_context (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    layer                 TEXT    NOT NULL,
    valid_from            TEXT    NOT NULL,
    valid_until           TEXT    NOT NULL,
    trading_posture       TEXT    NOT NULL,
    sizing_multiplier     REAL    NOT NULL,
    favored_sectors       TEXT,
    avoid_sectors         TEXT,
    favored_strategies    TEXT,
    confidence_adjustment INTEGER,
    macro_gate            TEXT    NOT NULL,
    risk_reward_gate      TEXT    NOT NULL,
    thesis_sentence       TEXT    NOT NULL,
    primary_authority     TEXT,
    full_thesis           TEXT,
    created_at            TEXT    DEFAULT CURRENT_TIMESTAMP,
    synced                INTEGER DEFAULT 0
);
"""

_CREATE_FALLBACK_EVENTS = """
CREATE TABLE IF NOT EXISTS thesis_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type        TEXT    NOT NULL,
    detected_at       TEXT    NOT NULL,
    description       TEXT    NOT NULL,
    affected_sectors  TEXT,
    thesis_paused     INTEGER DEFAULT 0,
    thesis_updated_at TEXT,
    resolved_at       TEXT,
    synced            INTEGER DEFAULT 0
);
"""

_CREATE_CONTEXT_IDX = """
CREATE INDEX IF NOT EXISTS idx_fb_context_layer
    ON thesis_context (layer, valid_from DESC);
"""

_CREATE_EVENTS_IDX = """
CREATE INDEX IF NOT EXISTS idx_fb_events_detected
    ON thesis_events (detected_at DESC);
"""


class FallbackChronicleClient:
    """Local SQLite fallback persistence for THESIS.

    Used when the primary CHRONICLE database is unreachable.
    Stores thesis contexts and events with a `synced` flag so they can
    be replayed to CHRONICLE once it recovers.

    Args:
        db_path: Path to the local fallback SQLite file.
            Defaults to THESIS_FALLBACK_DB_PATH env var or _DEFAULT_FALLBACK_PATH.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
    ) -> None:
        """Initialize, creating parent directory and schema if needed."""
        self.db_path = db_path or os.getenv("THESIS_FALLBACK_DB_PATH", _DEFAULT_FALLBACK_PATH)
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Create parent directory and apply schema if not already present."""
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            with conn:
                conn.execute(_CREATE_FALLBACK_CONTEXT)
                conn.execute(_CREATE_FALLBACK_EVENTS)
                conn.execute(_CREATE_CONTEXT_IDX)
                conn.execute(_CREATE_EVENTS_IDX)
            conn.close()
            logger.info("FallbackChronicleClient: schema ready at %s", self.db_path)
        except Exception as exc:
            logger.error("FallbackChronicleClient: schema init failed — %s", exc, exc_info=True)

    def _connect(self) -> sqlite3.Connection:
        """Open and return a configured SQLite connection.

        Returns:
            sqlite3.Connection with WAL mode and row_factory set.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    @staticmethod
    def _serialize_list(values: List[str]) -> str:
        """Serialize a list to compact JSON string.

        Args:
            values: List of strings.

        Returns:
            JSON-encoded string.
        """
        return json.dumps(values, separators=(",", ":"))

    @staticmethod
    def _deserialize_json_list(raw: Optional[str]) -> List[str]:
        """Deserialize a JSON list column.

        Args:
            raw: Raw JSON string or None.

        Returns:
            Parsed list, or [] on failure.
        """
        if not raw:
            return []
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _row_to_thesis_context(self, row: sqlite3.Row) -> ThesisContext:
        """Convert a Row to a ThesisContext model.

        Args:
            row: sqlite3.Row from thesis_context table.

        Returns:
            Populated ThesisContext.
        """
        return ThesisContext(
            id=row["id"],
            layer=row["layer"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            trading_posture=row["trading_posture"],
            sizing_multiplier=float(row["sizing_multiplier"]),
            favored_sectors=self._deserialize_json_list(row["favored_sectors"]),
            avoid_sectors=self._deserialize_json_list(row["avoid_sectors"]),
            favored_strategies=self._deserialize_json_list(row["favored_strategies"]),
            confidence_adjustment=int(row["confidence_adjustment"] or 0),
            macro_gate=row["macro_gate"],
            risk_reward_gate=row["risk_reward_gate"],
            thesis_sentence=row["thesis_sentence"],
            primary_authority=row["primary_authority"] or "",
            full_thesis=row["full_thesis"] or "",
            created_at=row["created_at"],
        )

    def _row_to_thesis_event(self, row: sqlite3.Row) -> ThesisEvent:
        """Convert a Row to a ThesisEvent model.

        Args:
            row: sqlite3.Row from thesis_events table.

        Returns:
            Populated ThesisEvent.
        """
        return ThesisEvent(
            id=row["id"],
            event_type=row["event_type"],
            detected_at=row["detected_at"],
            description=row["description"],
            affected_sectors=self._deserialize_json_list(row["affected_sectors"]),
            thesis_paused=bool(row["thesis_paused"]),
            thesis_updated_at=row["thesis_updated_at"],
            resolved_at=row["resolved_at"],
        )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def write_thesis_context(self, context: ThesisContext) -> Optional[int]:
        """Insert a thesis context record into the fallback database.

        Args:
            context: ThesisContext to persist.

        Returns:
            Inserted row id, or None on failure.
        """
        sql = """
            INSERT INTO thesis_context (
                layer, valid_from, valid_until, trading_posture,
                sizing_multiplier, favored_sectors, avoid_sectors,
                favored_strategies, confidence_adjustment, macro_gate,
                risk_reward_gate, thesis_sentence, primary_authority,
                full_thesis, synced
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """
        try:
            conn = self._connect()
            with conn:
                cursor = conn.execute(sql, (
                    context.layer, context.valid_from, context.valid_until,
                    context.trading_posture, context.sizing_multiplier,
                    self._serialize_list(context.favored_sectors),
                    self._serialize_list(context.avoid_sectors),
                    self._serialize_list(context.favored_strategies),
                    context.confidence_adjustment, context.macro_gate,
                    context.risk_reward_gate, context.thesis_sentence,
                    context.primary_authority, context.full_thesis,
                ))
                row_id: int = cursor.lastrowid
            conn.close()
            logger.warning(
                "FallbackChronicleClient: wrote thesis_context id=%d layer=%s to fallback",
                row_id, context.layer,
            )
            return row_id
        except sqlite3.Error as exc:
            logger.error("FallbackChronicleClient.write_thesis_context: %s", exc, exc_info=True)
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def write_thesis_event(self, event: ThesisEvent) -> Optional[int]:
        """Insert a thesis event record into the fallback database.

        Args:
            event: ThesisEvent to persist.

        Returns:
            Inserted row id, or None on failure.
        """
        sql = """
            INSERT INTO thesis_events (
                event_type, detected_at, description, affected_sectors,
                thesis_paused, thesis_updated_at, resolved_at, synced
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """
        try:
            conn = self._connect()
            with conn:
                cursor = conn.execute(sql, (
                    event.event_type, event.detected_at, event.description,
                    self._serialize_list(event.affected_sectors),
                    int(event.thesis_paused), event.thesis_updated_at,
                    event.resolved_at,
                ))
                row_id: int = cursor.lastrowid
            conn.close()
            logger.warning(
                "FallbackChronicleClient: wrote thesis_event id=%d type=%s to fallback",
                row_id, event.event_type,
            )
            return row_id
        except sqlite3.Error as exc:
            logger.error("FallbackChronicleClient.write_thesis_event: %s", exc, exc_info=True)
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def save_thesis(self, context: ThesisContext) -> bool:
        """Alias for write_thesis_context(); returns bool.

        Args:
            context: ThesisContext to save.

        Returns:
            True if saved successfully.
        """
        return self.write_thesis_context(context) is not None

    def save_event(self, event: ThesisEvent) -> bool:
        """Alias for write_thesis_event(); returns bool.

        Args:
            event: ThesisEvent to save.

        Returns:
            True if saved successfully.
        """
        return self.write_thesis_event(event) is not None

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_current_thesis(self, layer: str = "weekly") -> Optional[ThesisContext]:
        """Retrieve the most recent valid thesis from the fallback database.

        Args:
            layer: Thesis layer — 'weekly' or 'daily'.

        Returns:
            Most recent active ThesisContext, or None.
        """
        sql = """
            SELECT * FROM thesis_context
            WHERE layer = ?
              AND valid_until > datetime('now')
            ORDER BY created_at DESC
            LIMIT 1
        """
        try:
            conn = self._connect()
            cursor = conn.execute(sql, (layer,))
            row = cursor.fetchone()
            conn.close()
            return self._row_to_thesis_context(row) if row else None
        except sqlite3.Error as exc:
            logger.error(
                "FallbackChronicleClient.get_current_thesis (layer=%s): %s",
                layer, exc, exc_info=True,
            )
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_recent_events(self, hours: int = 24) -> List[ThesisEvent]:
        """Return thesis events from the last N hours.

        Args:
            hours: Look-back window in hours.

        Returns:
            List of ThesisEvent objects, newest first.
        """
        sql = """
            SELECT * FROM thesis_events
            WHERE detected_at >= datetime('now', ? || ' hours')
            ORDER BY detected_at DESC
        """
        try:
            conn = self._connect()
            cursor = conn.execute(sql, (f"-{hours}",))
            rows = cursor.fetchall()
            conn.close()
            return [self._row_to_thesis_event(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error(
                "FallbackChronicleClient.get_recent_events (hours=%d): %s",
                hours, exc, exc_info=True,
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Sync operations
    # ------------------------------------------------------------------

    def get_unsynced_contexts(self) -> List[tuple]:
        """Return all unsynced thesis_context rows as raw tuples for sync.

        Returns:
            List of (id, layer, valid_from, valid_until, trading_posture,
            sizing_multiplier, favored_sectors, avoid_sectors,
            favored_strategies, confidence_adjustment, macro_gate,
            risk_reward_gate, thesis_sentence, primary_authority, full_thesis)
        """
        sql = "SELECT * FROM thesis_context WHERE synced = 0 ORDER BY id ASC"
        try:
            conn = self._connect()
            cursor = conn.execute(sql)
            rows = [tuple(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except sqlite3.Error as exc:
            logger.error("FallbackChronicleClient.get_unsynced_contexts: %s", exc, exc_info=True)
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_unsynced_events(self) -> List[tuple]:
        """Return all unsynced thesis_events rows as raw tuples for sync.

        Returns:
            List of raw row tuples with synced=0.
        """
        sql = "SELECT * FROM thesis_events WHERE synced = 0 ORDER BY id ASC"
        try:
            conn = self._connect()
            cursor = conn.execute(sql)
            rows = [tuple(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except sqlite3.Error as exc:
            logger.error("FallbackChronicleClient.get_unsynced_events: %s", exc, exc_info=True)
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def mark_context_synced(self, fallback_id: int) -> bool:
        """Mark a fallback thesis_context row as synced.

        Args:
            fallback_id: Primary key in the fallback database.

        Returns:
            True if update succeeded.
        """
        try:
            conn = self._connect()
            with conn:
                conn.execute(
                    "UPDATE thesis_context SET synced = 1 WHERE id = ?",
                    (fallback_id,),
                )
            conn.close()
            return True
        except sqlite3.Error as exc:
            logger.error("FallbackChronicleClient.mark_context_synced: %s", exc, exc_info=True)
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def mark_event_synced(self, fallback_id: int) -> bool:
        """Mark a fallback thesis_events row as synced.

        Args:
            fallback_id: Primary key in the fallback database.

        Returns:
            True if update succeeded.
        """
        try:
            conn = self._connect()
            with conn:
                conn.execute(
                    "UPDATE thesis_events SET synced = 1 WHERE id = ?",
                    (fallback_id,),
                )
            conn.close()
            return True
        except sqlite3.Error as exc:
            logger.error("FallbackChronicleClient.mark_event_synced: %s", exc, exc_info=True)
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def db_reachable(self) -> bool:
        """Check if the fallback database is accessible.

        Returns:
            True if the database responds to SELECT 1.
        """
        try:
            conn = self._connect()
            conn.execute("SELECT 1")
            conn.close()
            return True
        except sqlite3.Error as exc:
            logger.error("FallbackChronicleClient.db_reachable: %s", exc)
            return False
