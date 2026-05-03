"""
rollback.py — GENESIS Atomic Rollback (G4).
Spec: genesis-resilience-v1.md v1.2

Every deploy tags pre_deploy_sha in RESILIENCE-DB.
If a service goes unhealthy twice within 5 minutes of a restart:
  1. Auto-revert to pre_deploy_sha
  2. Restart the service
  3. Verify health
  4. Log to CHRONICLE + RESILIENCE-DB
  5. One structured message to Ahmed
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("genesis.resilience.rollback")

RESILIENCE_DB_PATH = "/Users/ahmedsadek/nexus/data/resilience.db"
NEXUS_ROOT = "/Users/ahmedsadek/nexus"
HEALTH_TIMEOUT = 5

# Map service name → subdirectory in nexus repo
SERVICE_DIRS: dict[str, str] = {
    "axiom":           "axiom",
    "alpha-buffer":    "alpha-buffer",
    "prime-buffer":    "prime-buffer",
    "omni":            "omni",
    "alpha-execution": "alpha-execution",
    "prime-execution": "prime-execution",
    "oracle":          "oracle",
    "ails":            "ails",
    "guardian-angel":  "guardian-angel",
}

SERVICES_PORTS: dict[str, int] = {
    "axiom": 8001, "alpha-buffer": 8002, "prime-buffer": 8003,
    "omni": 8004, "alpha-execution": 8005, "prime-execution": 8006,
    "oracle": 8007, "ails": 8008, "guardian-angel": 8009,
}


@contextmanager
def _resilience_conn():
    """SQLite connection to RESILIENCE-DB with BEGIN IMMEDIATE."""
    conn = sqlite3.connect(RESILIENCE_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema() -> None:
    """Ensure deployment_history table exists in RESILIENCE-DB."""
    conn = sqlite3.connect(RESILIENCE_DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deployment_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            service         TEXT    NOT NULL,
            pre_deploy_sha  TEXT    NOT NULL,
            deploy_time     TEXT    NOT NULL,
            rollback_time   TEXT,
            rollback_sha    TEXT,
            rollback_status TEXT,
            notes           TEXT
        )
    """)
    conn.commit()
    conn.close()


def tag_pre_deploy(service_name: str, sha: str) -> None:
    """
    Record the pre-deploy git SHA in RESILIENCE-DB before any deploy.

    Call this immediately before deploying new code. This SHA is the
    rollback target if the deploy goes unhealthy.

    Args:
        service_name: Name of the service being deployed.
        sha:          Current git SHA (pre-deploy state).
    """
    try:
        os.makedirs(os.path.dirname(RESILIENCE_DB_PATH), exist_ok=True)
        _ensure_schema()
        with _resilience_conn() as conn:
            conn.execute(
                "INSERT INTO deployment_history (service, pre_deploy_sha, deploy_time) VALUES (?,?,?)",
                (service_name, sha, datetime.utcnow().isoformat())
            )
        logger.info("tag_pre_deploy: service=%s sha=%s", service_name, sha)
    except Exception as e:
        logger.error("tag_pre_deploy failed for %s: %s", service_name, e)


def _get_pre_deploy_sha(service_name: str) -> Optional[str]:
    """
    Retrieve most recent pre_deploy_sha for a service from RESILIENCE-DB.

    Args:
        service_name: Name of the service.

    Returns:
        SHA string, or None if not found.
    """
    try:
        conn = sqlite3.connect(RESILIENCE_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pre_deploy_sha FROM deployment_history WHERE service=? ORDER BY id DESC LIMIT 1",
            (service_name,)
        ).fetchone()
        conn.close()
        return row["pre_deploy_sha"] if row else None
    except Exception as e:
        logger.error("_get_pre_deploy_sha failed for %s: %s", service_name, e)
        return None


def check_post_deploy_health(service_name: str, port: int, window_minutes: int = 5) -> bool:
    """
    Poll service health twice within the post-deploy window.

    Requires two consecutive healthy responses within window_minutes to return True.
    Two unhealthy checks in the window → returns False (triggers rollback).

    Args:
        service_name:   Service to check.
        port:           Port number.
        window_minutes: Total time window for checks (default 5 min).

    Returns:
        True if both checks pass, False if either fails.
    """
    interval = (window_minutes * 60) / 2   # two checks evenly spaced
    checks_passed = 0

    for attempt in range(2):
        if attempt > 0:
            time.sleep(interval)
        try:
            r = requests.get(f"http://localhost:{port}/health", timeout=HEALTH_TIMEOUT)
            body = r.json() if r.status_code == 200 else {}
            healthy = r.status_code == 200 and body.get("status") in ("healthy", "active")
            if healthy:
                checks_passed += 1
                logger.info("check_post_deploy_health: %s check %d/2 PASS", service_name, attempt + 1)
            else:
                logger.warning(
                    "check_post_deploy_health: %s check %d/2 FAIL (status=%s http=%d)",
                    service_name, attempt + 1, body.get("status"), r.status_code
                )
                return False
        except Exception as e:
            logger.error(
                "check_post_deploy_health: %s check %d/2 ERROR: %s",
                service_name, attempt + 1, e
            )
            return False

    return checks_passed == 2


def auto_rollback(service_name: str) -> bool:
    """
    Execute atomic rollback to pre-deploy SHA.

    Steps:
        1. Retrieve pre_deploy_sha from RESILIENCE-DB
        2. git checkout pre_deploy_sha -- <service_dir>
        3. Restart service via launchctl
        4. Verify health
        5. Log outcome to CHRONICLE + RESILIENCE-DB
        6. Send one structured summary to Ahmed via Telegram

    Args:
        service_name: Service to roll back.

    Returns:
        True if rollback and health verification succeeded.
    """
    rollback_time = datetime.utcnow().isoformat()
    pre_sha = _get_pre_deploy_sha(service_name)
    port = SERVICES_PORTS.get(service_name)
    service_dir = SERVICE_DIRS.get(service_name)

    if not pre_sha:
        logger.error("auto_rollback: no pre_deploy_sha found for %s — cannot roll back", service_name)
        return False

    if not service_dir:
        logger.error("auto_rollback: unknown service dir for %s", service_name)
        return False

    logger.warning("auto_rollback: STARTING for %s to sha=%s", service_name, pre_sha)

    success = False
    error_msg = None

    try:
        # Step 1: git checkout to pre-deploy sha
        result = subprocess.run(
            ["git", "checkout", pre_sha, "--", service_dir],
            cwd=NEXUS_ROOT,
            capture_output=True, timeout=30, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"git checkout failed: {result.stderr.decode()[:200]}")

        logger.info("auto_rollback: git checkout complete for %s", service_name)

        # Step 2: Restart via launchctl
        label = f"ai.nexus.{service_name}"
        subprocess.run(["launchctl", "stop", label], capture_output=True, timeout=10, check=False)
        time.sleep(2)
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
            capture_output=True, timeout=15, check=False
        )
        time.sleep(5)

        # Step 3: Verify health
        if port:
            r = requests.get(f"http://localhost:{port}/health", timeout=HEALTH_TIMEOUT)
            body = r.json() if r.status_code == 200 else {}
            success = r.status_code == 200 and body.get("status") in ("healthy", "active")
        else:
            success = True  # No port to check — assume OK

    except Exception as e:
        error_msg = str(e)[:300]
        logger.error("auto_rollback: failed for %s: %s", service_name, error_msg)
        success = False

    # Step 4: Log to RESILIENCE-DB
    try:
        _ensure_schema()
        conn = sqlite3.connect(RESILIENCE_DB_PATH, timeout=10)
        conn.execute("""
            UPDATE deployment_history
            SET rollback_time=?, rollback_sha=?, rollback_status=?, notes=?
            WHERE service=? AND rollback_time IS NULL
            ORDER BY id DESC LIMIT 1
        """, (
            rollback_time, pre_sha,
            "success" if success else "failed",
            error_msg or "",
            service_name
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("auto_rollback: RESILIENCE-DB log failed: %s", e)

    # Step 5: Alert Ahmed (fail-open)
    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from sovereign_comms import report, EscalationLevel  # type: ignore
        status_icon = "✅" if success else "❌"
        msg = (
            f"{status_icon} ROLLBACK {'SUCCEEDED' if success else 'FAILED'}\n"
            f"Service: {service_name}\n"
            f"Reverted to: {pre_sha[:8]}\n"
            f"Time: {rollback_time}\n"
            f"{'Error: ' + error_msg if error_msg else 'Health verified'}"
        )
        report("genesis.rollback", msg, level=EscalationLevel.P0)
    except Exception as e:
        logger.warning("auto_rollback: Ahmed alert failed (non-blocking): %s", e)

    # Step 6: Log to CHRONICLE (fail-open)
    try:
        from chronicle_reader import chronicle_write  # type: ignore
        chronicle_write("rollback_event", {
            "service": service_name, "pre_sha": pre_sha,
            "success": success, "time": rollback_time, "error": error_msg
        })
    except Exception:
        pass

    return success
