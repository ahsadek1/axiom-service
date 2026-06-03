"""
crash_guard.py — VECTOR Diagnostic Crash Guard (V1).
Spec: vector-resilience-v1.md v1.1

Wraps vector_diagnostic.py main() with a crash boundary.
On exception: writes tombstone to persistent path + sends emergency Telegram.
On clean run: removes tombstone.

Tombstone path: /Users/ahmedsadek/nexus/vector/state/diagnostic_crashed
(persistent — survives reboot, not /tmp)

Guardian Angel (port 8009) checks for tombstone existence every 5 minutes.
VECTOR_DIAGNOSTIC_DOWN alert fires if tombstone age < 24h.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("vector.resilience.crash_guard")

ET = pytz.timezone("America/New_York")
VECTOR_STATE_DIR = Path("/Users/ahmedsadek/nexus/vector/state/")
TOMBSTONE_PATH   = VECTOR_STATE_DIR / "diagnostic_crashed"
TOMBSTONE_MAX_AGE_HOURS = 24


def run_with_crash_guard(main_fn: Callable) -> None:
    """
    Execute main_fn inside a crash guard boundary.

    On exception:
        1. Write tombstone to persistent TOMBSTONE_PATH
        2. Send emergency Telegram alert directly
        3. sys.exit(1)

    On clean completion:
        Remove tombstone (clears Guardian Angel alert).

    Args:
        main_fn: Zero-argument callable to execute (e.g. vector_diagnostic.main).
    """
    try:
        VECTOR_STATE_DIR.mkdir(parents=True, exist_ok=True)
        main_fn()
        clear_tombstone()
    except Exception as e:
        error_msg = str(e)[:500]
        logger.error("CRASH GUARD: vector_diagnostic CRASHED: %s", error_msg, exc_info=True)

        # Write persistent tombstone
        try:
            TOMBSTONE_PATH.write_text(json.dumps({
                "crashed_at": datetime.now(timezone.utc).isoformat(),
                "error": error_msg,
                "pid": os.getpid(),
            }))
        except Exception as write_err:
            logger.error("CRASH GUARD: tombstone write failed: %s", write_err)

        # Emergency Telegram (direct — not via bus)
        send_emergency_alert(f"🔴 VECTOR DIAGNOSTIC CRASHED: {error_msg[:300]}")

        sys.exit(1)


def send_emergency_alert(message: str) -> None:
    """
    Send a direct Telegram alert using BOT credentials from environment.

    Uses TELEGRAM_BOT_TOKEN + AHMED_CHAT_ID env vars.
    Falls back to TELEGRAM_CHAT_ID for backward compatibility.
    Never raises — logs any failures and continues.

    Args:
        message: Alert message text (truncated to 4000 chars if too long).
    """
    import requests

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # F2 fix: standardize to AHMED_CHAT_ID (matches alerts.py + alert_router.py)
    # Fall back to TELEGRAM_CHAT_ID for backward compatibility if AHMED_CHAT_ID not set
    chat_id = os.getenv("AHMED_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.warning("send_emergency_alert: TELEGRAM_BOT_TOKEN or AHMED_CHAT_ID not set")
        return

    text = f"🚨 VECTOR EMERGENCY\n{message}"[:4000]

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if r.status_code == 200:
            logger.info("send_emergency_alert: sent successfully")
        else:
            logger.warning("send_emergency_alert: HTTP %d: %s", r.status_code, r.text[:100])
    except Exception as e:
        logger.error("send_emergency_alert: failed: %s", e)


def is_tombstone_active() -> bool:
    """
    Check if the crash tombstone is present and recent.

    A tombstone is "active" if it exists AND was written within the last 24 hours.
    Older tombstones are stale and should not trigger alerts.

    Returns:
        True if tombstone exists and is <24h old.
    """
    if not TOMBSTONE_PATH.exists():
        return False

    try:
        age_hours = (time.time() - TOMBSTONE_PATH.stat().st_mtime) / 3600
        return age_hours < TOMBSTONE_MAX_AGE_HOURS
    except Exception as e:
        logger.warning("is_tombstone_active: stat failed: %s", e)
        return False


def clear_tombstone() -> None:
    """
    Remove the crash tombstone.

    Called after a clean diagnostic run. Automatically clears any
    Guardian Angel alert on the next GA check cycle.
    """
    try:
        TOMBSTONE_PATH.unlink(missing_ok=True)
        logger.debug("clear_tombstone: tombstone cleared")
    except Exception as e:
        logger.warning("clear_tombstone: failed to remove tombstone: %s", e)
