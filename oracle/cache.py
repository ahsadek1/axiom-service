"""
ORACLE Intelligence Hub — Cache Manager
L1: in-memory dict with TTL check (process lifetime)
L2: SQLite WAL mode (persists across restarts)
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)

# ── L1 Cache (in-memory) ──────────────────────────────────────────────────────
_l1: Dict[str, Dict[str, Any]] = {}  # key -> {"data": ..., "expires_at": float}

# ── Stats ─────────────────────────────────────────────────────────────────────
_hits = 0
_misses = 0


def _cache_key(ticker: str, engine: str, card_type: str = "full") -> str:
    """Build a consistent cache key."""
    return f"{ticker.upper()}:{engine}:{card_type}"


def _now() -> float:
    """Current UTC timestamp as float."""
    return time.time()


def _get_conn() -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection."""
    conn = sqlite3.connect(config.ORACLE_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all required tables on startup."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cache_entries (
                ticker      TEXT NOT NULL,
                engine      TEXT NOT NULL,
                card_type   TEXT NOT NULL,
                cached_at   TEXT DEFAULT (datetime('now')),
                ttl_seconds INTEGER NOT NULL,
                expires_at  TEXT NOT NULL,
                data_json   TEXT NOT NULL,
                PRIMARY KEY (ticker, engine, card_type)
            );

            CREATE TABLE IF NOT EXISTS api_calls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                engine      TEXT NOT NULL,
                platform    TEXT NOT NULL,
                ticker      TEXT,
                called_at   TEXT DEFAULT (datetime('now')),
                latency_ms  INTEGER,
                success     INTEGER NOT NULL DEFAULT 1,
                error_msg   TEXT,
                cache_hit   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ticker_cik_map (
                ticker       TEXT PRIMARY KEY,
                cik          TEXT NOT NULL,
                company_name TEXT NOT NULL,
                updated_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cycle_intelligence (
                cycle_id          TEXT PRIMARY KEY,
                cycle_time        TEXT NOT NULL,
                tickers_in_pool   TEXT NOT NULL,
                patterns_json     TEXT,
                echo_risk_tickers TEXT,
                created_at        TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS failover_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                engine           TEXT NOT NULL,
                platform         TEXT NOT NULL,
                failed_at        TEXT DEFAULT (datetime('now')),
                fallback_used    TEXT,
                recovered_at     TEXT,
                duration_seconds INTEGER
            );
        """)
    logger.info("ORACLE database tables initialized at %s", config.ORACLE_DB_PATH)


# ── Get ───────────────────────────────────────────────────────────────────────

def get(ticker: str, engine: str, card_type: str = "full") -> Optional[Any]:
    """
    Retrieve cached data. Checks L1 first, then L2.
    Returns None on miss or expired entry.
    """
    global _hits, _misses
    key = _cache_key(ticker, engine, card_type)
    now = _now()

    # L1 check
    if key in _l1:
        entry = _l1[key]
        if entry["expires_at"] > now:
            _hits += 1
            return entry["data"]
        else:
            del _l1[key]

    # L2 check
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT data_json, expires_at FROM cache_entries "
                "WHERE ticker=? AND engine=? AND card_type=?",
                (ticker.upper(), engine, card_type)
            ).fetchone()
            if row:
                data_json, expires_at_str = row
                expires_at = datetime.fromisoformat(expires_at_str).timestamp()
                if expires_at > now:
                    try:
                        data = json.loads(data_json)
                    except (json.JSONDecodeError, ValueError) as json_err:
                        # Adversarial fix #5: corrupted L2 cache entry — treat as miss,
                        # do not propagate the error to callers.
                        logger.warning(
                            "L2 cache JSON corruption for %s/%s — forcing miss: %s",
                            ticker, engine, json_err,
                        )
                        _misses += 1
                        return None
                    # Promote to L1
                    _l1[key] = {"data": data, "expires_at": expires_at}
                    _hits += 1
                    return data
    except sqlite3.Error as e:
        logger.error("L2 cache read error for %s/%s: %s", ticker, engine, e)

    _misses += 1
    return None


# ── Set ───────────────────────────────────────────────────────────────────────

def set(ticker: str, engine: str, data: Any, ttl_seconds: int,
        card_type: str = "full") -> None:
    """
    Write data to L1 and L2 cache simultaneously.
    """
    key = _cache_key(ticker, engine, card_type)
    now = _now()
    expires_at = now + ttl_seconds
    expires_at_str = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()

    # L1
    _l1[key] = {"data": data, "expires_at": expires_at}

    # L2
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO cache_entries
                   (ticker, engine, card_type, ttl_seconds, expires_at, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, engine, card_type) DO UPDATE SET
                   cached_at=datetime('now'), ttl_seconds=excluded.ttl_seconds,
                   expires_at=excluded.expires_at, data_json=excluded.data_json""",
                (ticker.upper(), engine, card_type, ttl_seconds,
                 expires_at_str, json.dumps(data))
            )
    except sqlite3.Error as e:
        logger.error("L2 cache write error for %s/%s: %s", ticker, engine, e)


# ── Stats ─────────────────────────────────────────────────────────────────────

def hit_rate() -> float:
    """Return cache hit rate as a fraction (0.0 – 1.0)."""
    total = _hits + _misses
    return _hits / total if total > 0 else 0.0


def warm_count() -> int:
    """Return number of non-expired L1 entries."""
    now = _now()
    return sum(1 for e in _l1.values() if e["expires_at"] > now)


# ── Log API call ──────────────────────────────────────────────────────────────

def log_api_call(engine: str, platform: str, ticker: Optional[str],
                 latency_ms: int, success: bool,
                 error_msg: Optional[str] = None,
                 cache_hit: bool = False) -> None:
    """Record an API call in the audit table."""
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO api_calls
                   (engine, platform, ticker, latency_ms, success, error_msg, cache_hit)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (engine, platform, ticker, latency_ms,
                 1 if success else 0, error_msg, 1 if cache_hit else 0)
            )
    except sqlite3.Error as e:
        logger.error("Failed to log API call: %s", e)


def log_failover(engine: str, platform: str, fallback: Optional[str]) -> None:
    """Record a failover event."""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO failover_events (engine, platform, fallback_used) VALUES (?,?,?)",
                (engine, platform, fallback)
            )
    except sqlite3.Error as e:
        logger.error("Failed to log failover event: %s", e)
