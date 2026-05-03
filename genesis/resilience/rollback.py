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

# F1 fix (Cipher adversarial review 2026-05-03):
# Use canonical RESILIENCE-DB via triad_db — not a separate orphaned file.
# rollback.py was writing to /nexus/data/resilience.db while triad_db uses
# /nexus/shared/resilience/resilience.db. Audit trail was silently missing.
from shared.resilience.triad_db import (
    log_deployment as _triad_log_deployment,
    mark_deployment_healthy as _triad_mark_healthy,
    mark_deployment_rolled_back as _triad_mark_rolled_back,
)

logger = logging.getLogger("genesis.resilience.rollback")

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


# Module-level registry: service_name -> triad_db deployment_id
# Populated by tag_pre_deploy(), consumed by auto_rollback()
_deployment_ids: dict[str, int] = {}
# Module-level registry: service_name -> pre_deploy_sha
_pre_deploy_shas: dict[str, str] = {}


def tag_pre_deploy(service_name: str, sha: str) -> None:
    """
    Record the pre-deploy git SHA in canonical RESILIENCE-DB (triad_db) before any deploy.

    Uses triad_db.log_deployment() — writes to the same DB as all other
    resilience events (not an orphaned separate file).

    Args:
        service_name: Name of the service being deployed.
        sha:          Current git SHA (pre-deploy state).
    """
    deployment_id = _triad_log_deployment(
        service=service_name,
        pre_deploy_sha=sha,
        post_deploy_sha="pending",  # updated on health verification
        deployed_by="genesis",
    )
    _deployment_ids[service_name] = deployment_id
    _pre_deploy_shas[service_name] = sha
    logger.info("tag_pre_deploy: service=%s sha=%s deployment_id=%d", service_name, sha, deployment_id)


def _get_pre_deploy_sha(service_name: str) -> Optional[str]:
    """
    Retrieve the pre-deploy SHA stored by tag_pre_deploy().

    Args:
        service_name: Name of the service.

    Returns:
        SHA string, or None if not found.
    """
    return _pre_deploy_shas.get(service_name)


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

    # Step 4: Log to canonical RESILIENCE-DB via triad_db (F1 fix)
    deployment_id = _deployment_ids.get(service_name, -1)
    if deployment_id > 0:
        if success:
            _triad_mark_healthy(deployment_id)
        _triad_mark_rolled_back(
            deployment_id,
            reason=f"auto_rollback: {'success' if success else 'failed'} — {error_msg or 'ok'}"
        )
    else:
        logger.warning(
            "auto_rollback: no deployment_id for %s — rollback not logged to RESILIENCE-DB",
            service_name
        )

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
