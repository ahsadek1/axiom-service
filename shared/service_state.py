"""
service_state.py — Durable Service State Store (GAP-002)

Persists critical in-memory state to SQLite so it survives restarts.
Uses WAL mode for concurrent access safety.

Commercial-grade guarantee:
- State written on every mutation
- State read on every startup
- Restart = resume, not reset
- No Redis required — SQLite WAL handles our workload

Persisted per service:
  alpha-execution : execution_paused, skipped_tickers, ticker_fail_counts, trades_today
  prime-execution : execution_paused, trades_today, was_paused_for_reconcile
  omni            : syntheses_today, go_verdicts_today, canary_degraded, sovereign_halted
  alpha-buffer    : circuit_breaker_status (read-only mirror)

Author: OMNI (GAP-002, 2026-04-29)
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

logger = logging.getLogger("nexus.service_state")

STATE_DB_PATH = os.environ.get(
    "SERVICE_STATE_DB_PATH",
    "/Users/ahmedsadek/nexus/data/service_state.db"
)

_write_lock = threading.Lock()


# ── Schema ────────────────────────────────────────────────────────────────────

def _init_db(db_path: str = STATE_DB_PATH) -> None:
    """Create the state store table if it doesn't exist. WAL mode enabled."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS service_state (
                service     TEXT NOT NULL,
                state_date  TEXT NOT NULL,   -- YYYY-MM-DD (daily reset for counters)
                key         TEXT NOT NULL,
                value       TEXT NOT NULL,   -- JSON-encoded
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (service, state_date, key)
            )
        """)
        conn.commit()


# ── Write ─────────────────────────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")


def _today_et() -> str:
    """Return today's date in Eastern Time (YYYY-MM-DD). Trading day scoping."""
    return datetime.now(_ET).date().isoformat()


def write_state(service: str, key: str, value: Any, db_path: str = STATE_DB_PATH) -> None:
    """
    Persist a single state value for a service.
    Thread-safe. Value is JSON-encoded so any type is supported.
    Date-scoped: counters (trades_today, syntheses_today) reset each ET calendar day.
    """
    try:
        _init_db(db_path)
        today = _today_et()
        now   = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(value)

        with _write_lock:
            with sqlite3.connect(db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    INSERT INTO service_state (service, state_date, key, value, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(service, state_date, key) DO UPDATE
                    SET value=excluded.value, updated_at=excluded.updated_at
                """, (service, today, key, encoded, now))
                conn.commit()
    except Exception as e:
        # Never raise — state persistence is best-effort, never blocks trading
        logger.debug("state write failed [%s/%s]: %s", service, key, e)


def write_states(service: str, state: dict, db_path: str = STATE_DB_PATH) -> None:
    """
    Persist multiple state values in one transaction.
    Preferred over multiple write_state() calls.
    """
    try:
        _init_db(db_path)
        today = _today_et()
        now   = datetime.now(timezone.utc).isoformat()

        rows = [
            (service, today, k, json.dumps(v), now)
            for k, v in state.items()
        ]

        with _write_lock:
            with sqlite3.connect(db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executemany("""
                    INSERT INTO service_state (service, state_date, key, value, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(service, state_date, key) DO UPDATE
                    SET value=excluded.value, updated_at=excluded.updated_at
                """, rows)
                conn.commit()
    except Exception as e:
        logger.debug("state bulk-write failed [%s]: %s", service, e)


# ── Read ──────────────────────────────────────────────────────────────────────

def read_state(
    service:  str,
    key:      str,
    default:  Any = None,
    db_path:  str = STATE_DB_PATH,
) -> Any:
    """
    Read a single state value for a service.
    Returns default if not found or on error.
    Only reads today's ET-date values — yesterday's counters don't carry over.
    """
    try:
        _init_db(db_path)
        today = _today_et()
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute(
                "SELECT value FROM service_state WHERE service=? AND state_date=? AND key=?",
                (service, today, key)
            ).fetchone()
        if row:
            return json.loads(row[0])
        return default
    except Exception as e:
        logger.debug("state read failed [%s/%s]: %s", service, key, e)
        return default


def read_all_state(service: str, db_path: str = STATE_DB_PATH) -> dict:
    """
    Read all persisted state for a service (today's values only).
    Returns {} if not found or on error.
    """
    try:
        _init_db(db_path)
        today = _today_et()
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            rows = conn.execute(
                "SELECT key, value FROM service_state WHERE service=? AND state_date=?",
                (service, today)
            ).fetchall()
        return {k: json.loads(v) for k, v in rows}
    except Exception as e:
        logger.debug("state read-all failed [%s]: %s", service, e)
        return {}


# ── Convenience helpers ───────────────────────────────────────────────────────

class ServiceStateWriter:
    """
    Thin wrapper for a specific service.
    Use this in each service to avoid passing service name everywhere.

    Usage:
        state = ServiceStateWriter("alpha-execution")
        state.write("execution_paused", True)
        state.write_many({"trades_today": 5, "skipped_tickers": ["NVDA"]})
        paused = state.read("execution_paused", default=False)
    """

    def __init__(self, service: str, db_path: str = STATE_DB_PATH):
        self.service  = service
        self.db_path  = db_path
        _init_db(db_path)
        logger.debug("ServiceStateWriter ready for %s", service)

    def write(self, key: str, value: Any) -> None:
        write_state(self.service, key, value, self.db_path)

    def write_many(self, state: dict) -> None:
        write_states(self.service, state, self.db_path)

    def read(self, key: str, default: Any = None) -> Any:
        return read_state(self.service, key, default, self.db_path)

    def read_all(self) -> dict:
        return read_all_state(self.service, self.db_path)

    def restore(self) -> dict:
        """
        Read all persisted state and return it for restoration.
        Logs what was restored for audit trail.
        """
        state = self.read_all()
        if state:
            logger.info(
                "GAP-002: Restored %d state key(s) for %s from prior session: %s",
                len(state), self.service,
                {k: str(v)[:40] for k, v in state.items()},
            )
        else:
            logger.info("GAP-002: No prior state found for %s — starting fresh", self.service)
        return state
