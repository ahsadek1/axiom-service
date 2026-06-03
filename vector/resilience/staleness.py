"""
staleness.py — VECTOR Log Scan Staleness Guard (V3).
Spec: vector-resilience-v1.md v1.1

Prevents false "all clear" reports when log rotation empties files.
Lock dir: /Users/ahmedsadek/nexus/vector/state/locks/ (persistent — not /tmp).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("vector.resilience.staleness")

ET = pytz.timezone("America/New_York")
LOCK_DIR = Path("/Users/ahmedsadek/nexus/vector/state/locks/")


class InterventionInProgress(Exception):
    """Raised when an intervention lock is already active for the target service."""
    pass


@contextmanager
def intervention_lock(service_name: str, timeout: int = 60) -> Generator[None, None, None]:
    """
    Persistent per-service lock to prevent concurrent interventions.

    Lock file is in LOCK_DIR (persistent path — survives reboot, not /tmp).
    If a lock file exists AND its age < timeout seconds: raise InterventionInProgress.
    Lock is always removed on exit (even on exception).

    Args:
        service_name: Service being intervened on.
        timeout:      Seconds before an existing lock is considered stale (default 60).

    Raises:
        InterventionInProgress: If an active lock already exists for this service.

    Yields:
        None — use as `with intervention_lock("axiom"):`.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"{service_name}.lock"

    # Check for existing lock
    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age < timeout:
                raise InterventionInProgress(
                    f"Intervention already in progress for {service_name} "
                    f"(lock age={age:.0f}s, timeout={timeout}s)"
                )
            else:
                # Stale lock — remove and proceed
                logger.warning(
                    "intervention_lock: stale lock for %s (age=%.0fs) — removing",
                    service_name, age
                )
                lock_path.unlink(missing_ok=True)
        except InterventionInProgress:
            raise
        except Exception as e:
            logger.warning("intervention_lock: failed to check lock for %s: %s", service_name, e)

    try:
        lock_path.write_text(datetime.now(ET).isoformat())
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def check_log_staleness(log_path: str, threshold_minutes: int = 20) -> Optional[str]:
    """
    Check whether a log file has been written to recently.

    A log file that hasn't been modified in >threshold_minutes may indicate
    a service deadlock or crash (log rotation false-clean case).

    Args:
        log_path:          Absolute path to the log file to check.
        threshold_minutes: Minutes without writes before flagging as stale (default 20).

    Returns:
        Warning string if stale, None if fresh OR file doesn't exist.
    """
    if not os.path.exists(log_path):
        # File doesn't exist yet — service may not have started; not an error
        return None

    try:
        mtime = os.path.getmtime(log_path)
        age_minutes = (time.time() - mtime) / 60

        if age_minutes > threshold_minutes:
            return (
                f"LOG_STALE: {log_path} last written {age_minutes:.0f}m ago "
                f"(threshold {threshold_minutes}m)"
            )
        return None

    except Exception as e:
        logger.warning("check_log_staleness: failed to stat %s: %s", log_path, e)
        return None
