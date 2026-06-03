"""
watchdog.py — VECTOR Credential Watchdog Liveness (V4).
Spec: vector-resilience-v1.md v1.1

Watches the watcher: verifies that vector_credential_watchdog.py has run
recently. CHRONICLE is the heartbeat store. Fail-open on CHRONICLE failures.

Key fixes from Cipher adversarial review:
- chronicle_read() wrapped in try/except → CHRONICLE down never blocks heartbeat
- days_since(None) null guard → no throws on first-ever run
"""

from __future__ import annotations

import logging
import sys
import os
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("vector.resilience.watchdog")

WATCHDOG_KEY       = "_watchdog_last_run"
STALENESS_DAYS     = 8
CHRONICLE_SHARED   = "/Users/ahmedsadek/nexus/shared"


def days_since(iso_timestamp: Optional[str]) -> Optional[float]:
    """
    Calculate days elapsed since an ISO timestamp string.

    Null guard: returns None if iso_timestamp is None (no throws on first run).

    Args:
        iso_timestamp: ISO 8601 timestamp string, or None.

    Returns:
        Float days since timestamp, or None if input is None/unparseable.
    """
    if iso_timestamp is None:
        return None

    try:
        then = datetime.fromisoformat(iso_timestamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - then).total_seconds() / 86400
    except Exception as e:
        logger.warning("days_since: failed to parse %r: %s", iso_timestamp, e)
        return None


def check_watchdog_liveness() -> Optional[str]:
    """
    Verify credential_watchdog.py has run within the last STALENESS_DAYS days.

    Fail-open contract:
    - CHRONICLE unavailable → log warning, return None (heartbeat must not block)
    - Never-run (last_run is None) → log info, return None (first run expected)
    - >8 days since last run → return alert string

    Returns:
        Alert string if stale, None if healthy or CHRONICLE unavailable.
    """
    last_run_str: Optional[str] = None

    try:
        sys.path.insert(0, CHRONICLE_SHARED)
        from chronicle_reader import chronicle_read  # type: ignore
        last_run_str = chronicle_read(WATCHDOG_KEY)
    except Exception as e:
        logger.warning(
            "check_watchdog_liveness: CHRONICLE unavailable — skipping check: %s", e
        )
        return None  # Fail-open — do NOT block heartbeat

    if last_run_str is None:
        logger.info("check_watchdog_liveness: no prior run recorded (first run expected)")
        return None

    age = days_since(last_run_str)
    if age is None:
        return None  # Unparseable — treated as unknown, not an alert

    if age > STALENESS_DAYS:
        msg = (
            f"⚠️ VECTOR: credential_watchdog has not run in {age:.1f} days "
            f"(threshold {STALENESS_DAYS}d) — last run: {last_run_str}"
        )
        logger.warning(msg)
        return msg

    return None


def record_watchdog_run() -> None:
    """
    Write the current timestamp to CHRONICLE as credential_watchdog's last run.

    Fail-open — CHRONICLE failure is logged but never propagated.
    Call this at the end of every successful credential_watchdog.py run.
    """
    try:
        sys.path.insert(0, CHRONICLE_SHARED)
        from chronicle_reader import chronicle_write  # type: ignore
        chronicle_write(WATCHDOG_KEY, datetime.now(timezone.utc).isoformat())
        logger.info("record_watchdog_run: heartbeat written to CHRONICLE")
    except Exception as e:
        logger.warning("record_watchdog_run: CHRONICLE write failed (non-blocking): %s", e)
