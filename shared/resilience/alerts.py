"""
alerts.py — Unified alert routing to Ahmed. Deduped, cooldown-protected.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
Status: Zero conflicts — replaces scattered direct Telegram bot calls.

Rule: alert_ahmed() is the ONE path for agent-to-Ahmed Telegram alerts.
Never call the bot API directly from service code.

Dedup: key= gives a per-alert-type cooldown (default 30 min).
       key=None → always sends (one-time critical events only).

C2 fix: cooldown state is persisted to SQLite so Railway dyno restarts and
rolling deploys don't reset the cooldown and fire duplicate alerts. In-memory
dict is used as a fast-path cache; DB is the source of truth.
If the DB is unavailable, we fall back to in-memory only (fail open —
missing an alert is worse than a duplicate).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("nexus.alerts")

ALERT_COOLDOWN = timedelta(minutes=30)

# Ahmed's Telegram chat ID (personal DM).
AHMED_CHAT_ID = "8573754783"

# BOT_TOKEN resolution order:
#   1. CIPHER_BOT_TOKEN env var (set in plist EnvironmentVariables)
#   2. NEXUS_BOT_TOKEN env var (generic fallback)
#
# No hardcoded token. If neither env var is set, alert_ahmed() will log
# an error and return False — fail loudly, never silently commit secrets.
_FALLBACK_BOT_TOKEN: str = ""  # Intentionally empty — must be set via env var

# C2: Persistent cooldown DB path.
# On Mac Mini: /Users/ahmedsadek/nexus/data/
# On Railway:  /data/ (mounted volume) or falls back to /tmp/ (ephemeral — still
#              better than pure in-memory since it survives within a session)
_ALERT_DB_PATH = os.environ.get(
    "NEXUS_ALERT_DB_PATH",
    "/Users/ahmedsadek/nexus/data/alert_cooldowns.db",
)

# In-process fast-path cache — avoids DB hit on every suppressed alert.
# Source of truth is the DB; this is populated on first check.
_alert_history: dict[str, datetime] = {}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _init_alert_db(db_path: str) -> None:
    """Create alert_cooldowns table if it doesn't exist."""
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_cooldowns (
                    key      TEXT PRIMARY KEY,
                    sent_at  TEXT NOT NULL
                )
            """)
            conn.commit()
    except Exception as e:
        logger.debug("alert_db init failed (in-memory fallback): %s", e)


def _is_suppressed_persistent(key: str, db_path: str) -> bool:
    """
    Check cooldown from persistent DB store.
    Returns True (suppress) if a record exists and is within cooldown window.
    Returns False (allow) if no record, expired, or DB unavailable.
    """
    try:
        with sqlite3.connect(db_path, timeout=3) as conn:
            row = conn.execute(
                "SELECT sent_at FROM alert_cooldowns WHERE key=?", (key,)
            ).fetchone()
        if row:
            sent_at = datetime.fromisoformat(row[0])
            if (datetime.utcnow() - sent_at) < ALERT_COOLDOWN:
                # Populate in-memory cache
                _alert_history[key] = sent_at
                return True
            # Expired — clean up
            with sqlite3.connect(db_path, timeout=3) as conn:
                conn.execute("DELETE FROM alert_cooldowns WHERE key=?", (key,))
                conn.commit()
    except Exception as e:
        logger.debug("alert_db read failed (in-memory fallback): %s", e)
        # Fall through to in-memory check
    return False


def _record_sent_persistent(key: str, db_path: str) -> None:
    """Record that an alert was sent. Updates both DB and in-memory cache."""
    now = datetime.utcnow()
    _alert_history[key] = now
    try:
        with sqlite3.connect(db_path, timeout=3) as conn:
            conn.execute("""
                INSERT INTO alert_cooldowns (key, sent_at) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET sent_at=excluded.sent_at
            """, (key, now.isoformat()))
            conn.commit()
    except Exception as e:
        logger.debug("alert_db write failed (in-memory only): %s", e)


def _get_bot_token() -> str:
    token = os.environ.get("CIPHER_BOT_TOKEN", "") or os.environ.get("NEXUS_BOT_TOKEN", "")
    if not token:
        logger.error(
            "CIPHER_BOT_TOKEN and NEXUS_BOT_TOKEN are both unset — "
            "alert_ahmed() will fail. Set one of these env vars."
        )
    return token


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def alert_ahmed(
    message: str,
    key: Optional[str] = None,
    severity: str = "WARNING",
) -> bool:
    """
    Send an alert to Ahmed via Telegram. Deduped by *key* within a 30-min
    cooldown window. Cooldown survives process restarts (persisted to DB).

    Args:
        message  — alert body text (plain or Markdown-safe)
        key      — dedup key; same key won't fire again for 30 min.
                   Pass key=None for one-time critical events that must
                   always deliver (e.g. service startup failures).
        severity — "INFO" | "WARNING" | "CRITICAL"

    Returns:
        True  — message sent
        False — suppressed by cooldown, or delivery failed
    """
    if key:
        # Fast path: check in-memory cache first
        last = _alert_history.get(key)
        if last and (datetime.utcnow() - last) < ALERT_COOLDOWN:
            logger.debug("Alert suppressed (in-memory cooldown): key=%s", key)
            return False
        # Slow path: check persistent DB (catches post-restart duplicates)
        if _is_suppressed_persistent(key, _ALERT_DB_PATH):
            logger.debug("Alert suppressed (persistent cooldown): key=%s", key)
            return False

    prefix = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🔴"}.get(severity, "⚠️")
    text = f"{prefix} *[{severity}]*\n{message}"

    token = _get_bot_token()
    if not token:
        logger.error("No bot token available — alert not delivered: %s", text[:100])
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": AHMED_CHAT_ID,
                "text": text,
                # No parse_mode — plain text avoids Markdown parse failures
                # from special chars like (), [], * in dynamic alert content
            },
            timeout=5,
        )
        resp.raise_for_status()
        # Record sent — both in DB and memory
        if key:
            _record_sent_persistent(key, _ALERT_DB_PATH)
        logger.info("Alert sent [%s] key=%s", severity, key)
        return True
    except Exception as e:
        logger.error("Alert delivery failed: %s — %s", message[:80], e)
        return False


def reset_cooldown(key: str) -> None:
    """
    Clear the cooldown for a given key — both in-memory and persistent DB.
    Call after a problem is resolved so the next recurrence fires immediately.

    Example:
        reset_cooldown("axiom_skip_threshold")
    """
    _alert_history.pop(key, None)
    try:
        with sqlite3.connect(_ALERT_DB_PATH, timeout=3) as conn:
            conn.execute("DELETE FROM alert_cooldowns WHERE key=?", (key,))
            conn.commit()
    except Exception as e:
        logger.debug("alert_db reset_cooldown failed: %s", e)


def reset_cooldowns() -> None:
    """
    Clear ALL active cooldowns — both in-memory and persistent DB.
    Used in tests and after full system restarts.
    """
    _alert_history.clear()
    try:
        with sqlite3.connect(_ALERT_DB_PATH, timeout=3) as conn:
            conn.execute("DELETE FROM alert_cooldowns")
            conn.commit()
    except Exception as e:
        logger.debug("alert_db reset_cooldowns failed: %s", e)


def cooldown_remaining(key: str) -> int:
    """
    Returns seconds remaining on a key's cooldown, or 0 if not active.
    Checks both in-memory and persistent DB.
    Useful for health endpoint diagnostics.
    """
    # Check in-memory first
    last = _alert_history.get(key)
    if not last:
        # Try DB
        try:
            with sqlite3.connect(_ALERT_DB_PATH, timeout=3) as conn:
                row = conn.execute(
                    "SELECT sent_at FROM alert_cooldowns WHERE key=?", (key,)
                ).fetchone()
            if row:
                last = datetime.fromisoformat(row[0])
        except Exception:
            pass
    if not last:
        return 0
    elapsed = (datetime.utcnow() - last).total_seconds()
    remaining = ALERT_COOLDOWN.total_seconds() - elapsed
    return max(0, int(remaining))


# Initialize DB on module load (non-blocking — failures are silent)
try:
    _init_alert_db(_ALERT_DB_PATH)
except Exception:
    pass
