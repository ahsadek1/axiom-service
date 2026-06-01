"""
ChronicleClient — Direct SQLite interface to the CHRONICLE database.

THESIS and CHRONICLE share the same Mac Mini (.42), so all reads/writes
bypass HTTP and go straight to the SQLite file.  Every public method
catches sqlite3.Error and returns a safe sentinel (None / [] / False)
rather than propagating exceptions to the caller.
"""

from __future__ import annotations

import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
try:
    from resilience.chronicle_result import ChronicleResult
except ImportError:
    # Fallback: inline minimal version
    from dataclasses import dataclass
    from typing import Generic, TypeVar, Optional as _Optional
    _T = TypeVar("_T")
    @dataclass
    class ChronicleResult(Generic[_T]):
        value: _Optional[_T]
        ok: bool
        error: _Optional[str] = None
        @classmethod
        def found(cls, v): return cls(value=v, ok=True)
        @classmethod
        def not_found(cls): return cls(value=None, ok=True)
        @classmethod
        def failed(cls, e): return cls(value=None, ok=False, error=str(e))


import json
import logging
import sqlite3
from typing import List, Optional

from models import ThesisContext, ThesisEvent

logger = logging.getLogger(__name__)


class ChronicleClient:
    """Direct-SQLite client for the CHRONICLE persistence layer.

    Opens a new connection per method call so that the class is safe to
    share across threads without an external lock.

    Args:
        db_path: Absolute path to chronicle.db on the local filesystem.
    """

    def __init__(self, db_path: str) -> None:
        """Store the database path; no connection is opened yet."""
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open and return a new SQLite connection with row_factory set.

        Returns:
            A configured sqlite3.Connection instance.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for better concurrent read performance.
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    @staticmethod
    def _deserialize_json_list(raw: Optional[str]) -> List[str]:
        """Deserialize a JSON-encoded list column; return [] on failure.

        Args:
            raw: Raw JSON string from the database column, or None.

        Returns:
            Parsed list of strings, or an empty list if parsing fails.
        """
        if not raw:
            return []
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                return value
            return []
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _serialize_list(values: List[str]) -> str:
        """Serialize a list to a compact JSON string for storage.

        Args:
            values: List of strings to serialize.

        Returns:
            JSON-encoded string representation.
        """
        return json.dumps(values, separators=(",", ":"))

    def _row_to_thesis_context(self, row: sqlite3.Row) -> ThesisContext:
        """Convert a sqlite3.Row from thesis_context to a ThesisContext model.

        Args:
            row: A row fetched from the thesis_context table.

        Returns:
            Populated ThesisContext instance.
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
            confidence_adjustment=int(row["confidence_adjustment"]),
            macro_gate=row["macro_gate"],
            risk_reward_gate=row["risk_reward_gate"],
            thesis_sentence=row["thesis_sentence"],
            primary_authority=row["primary_authority"] or "",
            full_thesis=row["full_thesis"] or "",
            created_at=row["created_at"],
        )

    def _row_to_thesis_event(self, row: sqlite3.Row) -> ThesisEvent:
        """Convert a sqlite3.Row from thesis_events to a ThesisEvent model.

        Args:
            row: A row fetched from the thesis_events table.

        Returns:
            Populated ThesisEvent instance.
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
    # ThesisContext operations
    # ------------------------------------------------------------------

    def write_thesis_context(self, context: ThesisContext) -> Optional[int]:
        """Insert or update a thesis context record in CHRONICLE.

        If a record already exists for the same layer and the same ISO week
        (derived from valid_from), the existing row is updated in place.
        Otherwise a new row is inserted.

        Args:
            context: The ThesisContext to persist.

        Returns:
            The row id of the inserted or updated record, or None on failure.
        """
        sql_check = """
            SELECT id FROM thesis_context
            WHERE layer = ?
              AND strftime('%Y-%W', valid_from) = strftime('%Y-%W', ?)
            ORDER BY created_at DESC
            LIMIT 1
        """
        sql_insert = """
            INSERT INTO thesis_context (
                layer, valid_from, valid_until, trading_posture,
                sizing_multiplier, favored_sectors, avoid_sectors,
                favored_strategies, confidence_adjustment, macro_gate,
                risk_reward_gate, thesis_sentence, primary_authority,
                full_thesis
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        sql_update = """
            UPDATE thesis_context SET
                valid_from        = ?,
                valid_until       = ?,
                trading_posture   = ?,
                sizing_multiplier = ?,
                favored_sectors   = ?,
                avoid_sectors     = ?,
                favored_strategies = ?,
                confidence_adjustment = ?,
                macro_gate        = ?,
                risk_reward_gate  = ?,
                thesis_sentence   = ?,
                primary_authority = ?,
                full_thesis       = ?
            WHERE id = ?
        """
        favored_sectors_json = self._serialize_list(context.favored_sectors)
        avoid_sectors_json = self._serialize_list(context.avoid_sectors)
        favored_strategies_json = self._serialize_list(context.favored_strategies)

        try:
            conn = self._connect()
            with conn:
                cursor = conn.execute(sql_check, (context.layer, context.valid_from))
                existing = cursor.fetchone()

                if existing:
                    row_id: int = existing["id"]
                    conn.execute(
                        sql_update,
                        (
                            context.valid_from,
                            context.valid_until,
                            context.trading_posture,
                            context.sizing_multiplier,
                            favored_sectors_json,
                            avoid_sectors_json,
                            favored_strategies_json,
                            context.confidence_adjustment,
                            context.macro_gate,
                            context.risk_reward_gate,
                            context.thesis_sentence,
                            context.primary_authority,
                            context.full_thesis,
                            row_id,
                        ),
                    )
                    logger.info(
                        "ChronicleClient: updated thesis_context id=%d layer=%s",
                        row_id,
                        context.layer,
                    )
                    return row_id
                else:
                    cursor = conn.execute(
                        sql_insert,
                        (
                            context.layer,
                            context.valid_from,
                            context.valid_until,
                            context.trading_posture,
                            context.sizing_multiplier,
                            favored_sectors_json,
                            avoid_sectors_json,
                            favored_strategies_json,
                            context.confidence_adjustment,
                            context.macro_gate,
                            context.risk_reward_gate,
                            context.thesis_sentence,
                            context.primary_authority,
                            context.full_thesis,
                        ),
                    )
                    row_id = cursor.lastrowid
                    logger.info(
                        "ChronicleClient: inserted thesis_context id=%d layer=%s",
                        row_id,
                        context.layer,
                    )
                    return row_id
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.write_thesis_context failed: %s", exc, exc_info=True
            )
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_current_thesis(self, layer: str = "weekly") -> Optional[ThesisContext]:
        """Retrieve the most recent valid thesis for the given layer.

        'Valid' means valid_until is in the future relative to the database
        server's current time.

        Args:
            layer: The thesis layer to query, e.g. ``"weekly"`` or ``"daily"``.

        Returns:
            The most recent active ThesisContext, or None if none exists or on
            any database error.
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
            if row is None:
                logger.debug(
                    "ChronicleClient.get_current_thesis: no active thesis for layer=%s",
                    layer,
                )
                return None
            return self._row_to_thesis_context(row)
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.get_current_thesis failed (layer=%s): %s",
                layer,
                exc,
                exc_info=True,
            )
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_all_recent_theses(
        self, layer: str, limit: int = 10
    ) -> List[ThesisContext]:
        """Retrieve the most recent thesis records for a layer, newest first.

        Args:
            layer: The thesis layer to query.
            limit: Maximum number of records to return (default 10).

        Returns:
            List of ThesisContext objects, or an empty list on any error.
        """
        sql = """
            SELECT * FROM thesis_context
            WHERE layer = ?
            ORDER BY created_at DESC
            LIMIT ?
        """
        try:
            conn = self._connect()
            cursor = conn.execute(sql, (layer, limit))
            rows = cursor.fetchall()
            conn.close()
            return [self._row_to_thesis_context(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.get_all_recent_theses failed (layer=%s): %s",
                layer,
                exc,
                exc_info=True,
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # ThesisEvent operations
    # ------------------------------------------------------------------

    def write_thesis_event(self, event: ThesisEvent) -> Optional[int]:
        """Insert a new thesis event into CHRONICLE.

        Args:
            event: The ThesisEvent to persist.

        Returns:
            The row id of the newly inserted event, or None on failure.
        """
        sql = """
            INSERT INTO thesis_events (
                event_type, detected_at, description, affected_sectors,
                thesis_paused, thesis_updated_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        affected_sectors_json = self._serialize_list(event.affected_sectors)
        try:
            conn = self._connect()
            with conn:
                cursor = conn.execute(
                    sql,
                    (
                        event.event_type,
                        event.detected_at,
                        event.description,
                        affected_sectors_json,
                        int(event.thesis_paused),
                        event.thesis_updated_at,
                        event.resolved_at,
                    ),
                )
                row_id: int = cursor.lastrowid
            conn.close()
            logger.info(
                "ChronicleClient: inserted thesis_event id=%d type=%s",
                row_id,
                event.event_type,
            )
            return row_id
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.write_thesis_event failed: %s", exc, exc_info=True
            )
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_recent_events(self, limit: int = 20) -> List[ThesisEvent]:
        """Retrieve the most recent thesis events, newest first.

        Args:
            limit: Maximum number of events to return (default 20).

        Returns:
            List of ThesisEvent objects, or an empty list on any error.
        """
        sql = """
            SELECT * FROM thesis_events
            ORDER BY detected_at DESC
            LIMIT ?
        """
        try:
            conn = self._connect()
            cursor = conn.execute(sql, (limit,))
            rows = cursor.fetchall()
            conn.close()
            return [self._row_to_thesis_event(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.get_recent_events failed: %s", exc, exc_info=True
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def update_thesis_event(
        self,
        event_id: int,
        thesis_updated_at: Optional[str],
        resolved_at: Optional[str],
    ) -> bool:
        """Update the thesis_updated_at and/or resolved_at fields of an event.

        Args:
            event_id: Primary key of the event row to update.
            thesis_updated_at: ISO-8601 timestamp indicating when the thesis
                was revised in response to this event, or None to leave unchanged.
            resolved_at: ISO-8601 timestamp indicating when the event was
                resolved, or None to leave unchanged.

        Returns:
            True if the update succeeded, False on any error.
        """
        sql = """
            UPDATE thesis_events
            SET thesis_updated_at = COALESCE(?, thesis_updated_at),
                resolved_at       = COALESCE(?, resolved_at)
            WHERE id = ?
        """
        try:
            conn = self._connect()
            with conn:
                conn.execute(sql, (thesis_updated_at, resolved_at, event_id))
            conn.close()
            logger.info(
                "ChronicleClient: updated thesis_event id=%d "
                "thesis_updated_at=%s resolved_at=%s",
                event_id,
                thesis_updated_at,
                resolved_at,
            )
            return True
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.update_thesis_event failed (id=%d): %s",
                event_id,
                exc,
                exc_info=True,
            )
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def db_reachable(self) -> bool:
        """Perform a lightweight connectivity check against the database.

        Executes ``SELECT 1`` to confirm the file is accessible and the
        SQLite engine can respond.

        Returns:
            True if the database is reachable, False otherwise.
        """
        try:
            conn = self._connect()
            conn.execute("SELECT 1")
            conn.close()
            return True
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.db_reachable: database unreachable: %s", exc
            )
            return False

    # ------------------------------------------------------------------
    # Alias methods (used by thesis_engine, layers, and main)
    # ------------------------------------------------------------------

    def save_thesis(self, context: "ThesisContext") -> bool:
        """Alias for write_thesis_context(); returns bool instead of Optional[int].

        Args:
            context: ThesisContext to save.

        Returns:
            True if saved successfully, False otherwise.
        """
        result = self.write_thesis_context(context)
        return result is not None

    def save_event(self, event: "ThesisEvent") -> bool:
        """Alias for write_thesis_event(); returns bool instead of Optional[int].

        Args:
            event: ThesisEvent to save.

        Returns:
            True if saved successfully, False otherwise.
        """
        result = self.write_thesis_event(event)
        return result is not None

    def get_recent_events(self, hours: int = 24) -> "List[ThesisEvent]":  # type: ignore[override]
        """Return events from the last N hours (overrides parent signature).

        Args:
            hours: Number of hours to look back (default 24).

        Returns:
            List of ThesisEvent objects from that window.
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
                "ChronicleClient.get_recent_events(hours=%d) failed: %s",
                hours, exc, exc_info=True,
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_all_thesis_history(self) -> "List[ThesisContext]":
        """Return all thesis_context records, newest first.

        Returns:
            List of all ThesisContext objects in the database.
        """
        sql = "SELECT * FROM thesis_context ORDER BY created_at DESC"
        try:
            conn = self._connect()
            cursor = conn.execute(sql)
            rows = cursor.fetchall()
            conn.close()
            return [self._row_to_thesis_context(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error(
                "ChronicleClient.get_all_thesis_history failed: %s", exc, exc_info=True
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# ResilientChronicleClient — wraps primary + fallback with auto-sync
# ---------------------------------------------------------------------------

class ResilientChronicleClient:
    """Resilient CHRONICLE client with automatic local SQLite fallback.

    Wraps a primary ChronicleClient and a FallbackChronicleClient.
    On primary write failure, transparently writes to fallback.
    After any successful primary write, syncs unsynced fallback rows
    back to CHRONICLE automatically.

    Args:
        primary: ChronicleClient connected to the CHRONICLE database.
        fallback: FallbackChronicleClient connected to the local fallback DB.
    """

    def __init__(
        self,
        primary: "ChronicleClient",
        fallback: "FallbackChronicleClient",
    ) -> None:
        """Store primary and fallback clients."""
        self._primary = primary
        self._fallback = fallback

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def write_thesis_context(self, context: "ThesisContext") -> Optional[int]:
        """Write thesis context — primary first, fallback on failure.

        After a successful primary write, syncs any unsynced fallback rows.

        Args:
            context: ThesisContext to persist.

        Returns:
            Row id from primary or fallback, or None if both fail.
        """
        result = self._primary.write_thesis_context(context)
        if result is not None:
            self._sync_fallback()
            return result
        logger.warning(
            "ResilientChronicleClient: primary write failed for layer=%s — writing to fallback",
            context.layer,
        )
        return self._fallback.write_thesis_context(context)

    def write_thesis_event(self, event: "ThesisEvent") -> Optional[int]:
        """Write thesis event — primary first, fallback on failure.

        Args:
            event: ThesisEvent to persist.

        Returns:
            Row id from primary or fallback, or None if both fail.
        """
        result = self._primary.write_thesis_event(event)
        if result is not None:
            self._sync_fallback()
            return result
        logger.warning(
            "ResilientChronicleClient: primary write failed for event type=%s — writing to fallback",
            event.event_type,
        )
        return self._fallback.write_thesis_event(event)

    def save_thesis(self, context: "ThesisContext") -> bool:
        """Alias for write_thesis_context(); returns bool.

        Args:
            context: ThesisContext to save.

        Returns:
            True if saved to primary or fallback.
        """
        return self.write_thesis_context(context) is not None

    def save_event(self, event: "ThesisEvent") -> bool:
        """Alias for write_thesis_event(); returns bool.

        Args:
            event: ThesisEvent to save.

        Returns:
            True if saved to primary or fallback.
        """
        return self.write_thesis_event(event) is not None

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_current_thesis(self, layer: str = "weekly") -> Optional["ThesisContext"]:
        """Read current thesis — primary first, fallback on failure.

        Args:
            layer: Thesis layer — 'weekly' or 'daily'.

        Returns:
            ThesisContext or None.
        """
        result = self._primary.get_current_thesis(layer)
        if result is not None:
            return result
        logger.warning(
            "ResilientChronicleClient: primary read failed for layer=%s — trying fallback",
            layer,
        )
        return self._fallback.get_current_thesis(layer)

    def get_recent_events(self, hours: int = 24) -> list:
        """Read recent events — primary first, fallback on failure.

        Args:
            hours: Look-back window in hours.

        Returns:
            List of ThesisEvent objects.
        """
        try:
            result = self._primary.get_recent_events(hours=hours)
            if result is not None:
                return result
        except Exception:
            pass
        logger.warning("ResilientChronicleClient: primary events read failed — trying fallback")
        return self._fallback.get_recent_events(hours=hours)

    def get_all_thesis_history(self) -> list:
        """Read full thesis history from primary.

        Returns:
            List of ThesisContext objects.
        """
        return self._primary.get_all_thesis_history()

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _sync_fallback(self) -> None:
        """Sync all unsynced fallback rows to primary CHRONICLE.

        Called automatically after every successful primary write.
        Marks synced=1 on fallback rows that successfully replay.
        Logs count of synced records.
        """
        synced_count = 0

        # Sync thesis contexts
        for row in self._fallback.get_unsynced_contexts():
            # row columns: id, layer, valid_from, valid_until, trading_posture,
            # sizing_multiplier, favored_sectors, avoid_sectors,
            # favored_strategies, confidence_adjustment, macro_gate,
            # risk_reward_gate, thesis_sentence, primary_authority,
            # full_thesis, created_at, synced
            try:
                import json as _json

                def _djl(raw: Optional[str]) -> list:
                    if not raw:
                        return []
                    try:
                        v = _json.loads(raw)
                        return v if isinstance(v, list) else []
                    except Exception:
                        return []

                context = ThesisContext(
                    layer=row[1],
                    valid_from=row[2],
                    valid_until=row[3],
                    trading_posture=row[4],
                    sizing_multiplier=float(row[5]),
                    favored_sectors=_djl(row[6]),
                    avoid_sectors=_djl(row[7]),
                    favored_strategies=_djl(row[8]),
                    confidence_adjustment=int(row[9] or 0),
                    macro_gate=row[10],
                    risk_reward_gate=row[11],
                    thesis_sentence=row[12],
                    primary_authority=row[13] or "",
                    full_thesis=row[14] or "",
                )
                result = self._primary.write_thesis_context(context)
                if result is not None:
                    self._fallback.mark_context_synced(row[0])
                    synced_count += 1
            except Exception as exc:
                logger.error("ResilientChronicleClient._sync_fallback context row=%s: %s", row[0], exc)

        # Sync thesis events
        for row in self._fallback.get_unsynced_events():
            # row columns: id, event_type, detected_at, description,
            # affected_sectors, thesis_paused, thesis_updated_at, resolved_at, synced
            try:
                def _djl2(raw: Optional[str]) -> list:
                    if not raw:
                        return []
                    try:
                        import json as _j
                        v = _j.loads(raw)
                        return v if isinstance(v, list) else []
                    except Exception:
                        return []

                event = ThesisEvent(
                    event_type=row[1],
                    detected_at=row[2],
                    description=row[3],
                    affected_sectors=_djl2(row[4]),
                    thesis_paused=bool(row[5]),
                    thesis_updated_at=row[6],
                    resolved_at=row[7],
                )
                result = self._primary.write_thesis_event(event)
                if result is not None:
                    self._fallback.mark_event_synced(row[0])
                    synced_count += 1
            except Exception as exc:
                logger.error("ResilientChronicleClient._sync_fallback event row=%s: %s", row[0], exc)

        if synced_count > 0:
            logger.info(
                "ResilientChronicleClient: synced %d fallback records to CHRONICLE",
                synced_count,
            )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def db_reachable(self) -> bool:
        """Check if primary or fallback database is reachable.

        Returns:
            True if either primary or fallback responds.
        """
        return self._primary.db_reachable() or self._fallback.db_reachable()
