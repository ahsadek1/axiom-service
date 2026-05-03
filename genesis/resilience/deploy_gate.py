"""
deploy_gate.py — GENESIS Pre-Deploy Contract Gate (G1).
Spec: genesis-resilience-v1.md v1.2

Prevents broken code reaching live services via:
  G1A — Two-tier circuit registry (local cache authoritative, CHRONICLE refresh-only)
  G1B — STANDBY mode on startup failure (no sys.exit → no crash loops)
  G1C — WAL write-ahead log (advisory — never blocks critical operations)

WAL rotation: 50MB or 7 days, whichever first. Runs at 4 AM ET via LaunchAgent.
Archive: last 30 rotations retained.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("genesis.resilience.deploy_gate")

# ── Paths ─────────────────────────────────────────────────────────────────────
REGISTRY_PATH = Path("/Users/ahmedsadek/nexus/shared/resilience/circuit_registry.json")
WAL_DB_PATH   = Path("/Users/ahmedsadek/nexus/shared/resilience/wal.db")
WAL_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
WAL_MAX_DAYS  = 7
WAL_ARCHIVE_KEEP = 30

# ── Circuit Registry ──────────────────────────────────────────────────────────

def load_circuit_registry() -> dict:
    """
    Load circuit breaker registry from local cache.

    Never raises — returns empty dict on any error (missing file, bad JSON, etc.).
    CHRONICLE down = stale local cache, service starts normally.
    This is the authoritative source at startup; CHRONICLE is refresh-only.

    Returns:
        Dict mapping service names to circuit breaker configs.
    """
    try:
        if REGISTRY_PATH.exists():
            return json.loads(REGISTRY_PATH.read_text())
        return {}
    except Exception as e:
        logger.warning("load_circuit_registry: failed to load %s — returning empty: %s", REGISTRY_PATH, e)
        return {}


def refresh_registry_from_chronicle() -> None:
    """
    Background refresh of circuit registry from CHRONICLE.

    Failures are logged but never propagated — CHRONICLE down must not
    affect service startup or operation. Runs after service is healthy.

    Side effects:
        Updates REGISTRY_PATH if CHRONICLE returns valid data.
    """
    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from chronicle_reader import chronicle_read  # type: ignore
        data = chronicle_read("circuit_registry")
        if data:
            REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            REGISTRY_PATH.write_text(json.dumps(data, indent=2))
            logger.info("refresh_registry_from_chronicle: updated local cache")
    except Exception as e:
        logger.warning("refresh_registry_from_chronicle: CHRONICLE unavailable — using cached: %s", e)


# ── WAL helpers ───────────────────────────────────────────────────────────────

def _ensure_wal_schema(conn: sqlite3.Connection) -> None:
    """Create WAL table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wal_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT    NOT NULL,
            payload    TEXT    NOT NULL,
            written_at TEXT    NOT NULL
        )
    """)
    conn.commit()


@contextmanager
def _immediate_conn(db_path: Path):
    """Context manager for BEGIN IMMEDIATE SQLite transaction."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def wal_write(event_type: str, payload: dict) -> None:
    """
    Write an event to the write-ahead log.

    ALWAYS advisory — failures are logged loudly but NEVER propagated.
    The critical operation (order placement, DB write, deploy) must never
    be blocked by WAL failure.

    Args:
        event_type: Category of event (e.g. "deploy_started", "rollback_triggered").
        payload:    Structured dict with event context.
    """
    try:
        WAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Ensure schema exists first (plain conn, no transaction needed)
        setup_conn = sqlite3.connect(str(WAL_DB_PATH), timeout=10)
        _ensure_wal_schema(setup_conn)
        setup_conn.close()

        with _immediate_conn(WAL_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO wal_events (event_type, payload, written_at) VALUES (?,?,?)",
                (event_type, json.dumps(payload), datetime.utcnow().isoformat())
            )
    except Exception as e:
        logger.error(
            "WAL write FAILED (ADVISORY — operation continues): event_type=%s error=%s",
            event_type, e
        )
        # Never raise. Never return False. Caller proceeds regardless.


def rotate_wal_if_needed() -> bool:
    """
    Rotate WAL if it exceeds 50MB or is older than 7 days.

    Rotation: rename current WAL to wal_YYYY-MM-DD.db.
    Archive: keep last WAL_ARCHIVE_KEEP rotations.
    Safe to call even if WAL doesn't exist yet.

    Returns:
        True if rotation occurred, False otherwise.
    """
    if not WAL_DB_PATH.exists():
        return False

    try:
        stat = WAL_DB_PATH.stat()
        size_bytes = stat.st_size
        age_days = (time.time() - stat.st_mtime) / 86400

        needs_rotation = size_bytes >= WAL_MAX_BYTES or age_days >= WAL_MAX_DAYS
        if not needs_rotation:
            return False

        # Rename current WAL with date stamp
        date_str = datetime.utcnow().strftime("%Y-%m-%d-%H%M%S")
        archive_path = WAL_DB_PATH.parent / f"wal_{date_str}.db"
        WAL_DB_PATH.rename(archive_path)
        logger.info(
            "WAL rotated: %s (size=%.1fMB, age=%.1fd)",
            archive_path.name, size_bytes / 1024 / 1024, age_days
        )

        # Prune old archives (keep last WAL_ARCHIVE_KEEP)
        archives = sorted(WAL_DB_PATH.parent.glob("wal_*.db"))
        for old in archives[:-WAL_ARCHIVE_KEEP]:
            try:
                old.unlink()
                logger.info("WAL archive pruned: %s", old.name)
            except Exception as e:
                logger.warning("WAL archive prune failed for %s: %s", old.name, e)

        return True

    except Exception as e:
        logger.error("rotate_wal_if_needed failed: %s", e)
        return False


# ── Standby mode ──────────────────────────────────────────────────────────────

def enter_standby(service_name: str, reason: str) -> None:
    """
    Enter STANDBY mode on startup contract failure.

    Does NOT call sys.exit (which causes Railway crash loops).
    Instead: logs loudly, alerts SOVEREIGN, writes to WAL.
    Service should set a flag and return 503 on all endpoints until recovered.

    Args:
        service_name: Name of the service entering standby.
        reason:       Human-readable reason for standby entry.
    """
    logger.error("STANDBY ENTERED — service=%s reason=%s", service_name, reason)

    # WAL advisory log
    wal_write("standby_entered", {"service": service_name, "reason": reason,
                                   "timestamp": datetime.utcnow().isoformat()})

    # Alert SOVEREIGN (fail-open — don't block standby entry on alert failure)
    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from sovereign_comms import report, EscalationLevel  # type: ignore
        report(
            service_name,
            f"STANDBY ENTERED: {reason}",
            level=EscalationLevel.P1,
        )
    except Exception as e:
        logger.warning("enter_standby: SOVEREIGN alert failed (non-blocking): %s", e)

    # Explicitly: no sys.exit(), no raise. Caller handles standby state.
