"""
stale_deploy_monitor.py — GENESIS G3: Stale Deploy Auto-Restart
Spec: genesis-resilience-v1.md v1.2 — BLOCK G2
Author: GENESIS | Built: 2026-05-02

Monitors all Nexus services for stale_deploy:true.
If a service reports stale_deploy for >10 minutes after a commit → auto-restart.

Never raises. Restart failures are escalated via Alert Router.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("resilience.stale_deploy_monitor")

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

SERVICES = [
    ("axiom",           8001, "ai.nexus.axiom"),
    ("alpha-buffer",    8002, "ai.nexus.alpha-buffer"),
    ("prime-buffer",    8003, "ai.nexus.prime-buffer"),
    ("omni",            8004, "ai.nexus.omni"),
    ("alpha-execution", 8005, "ai.nexus.alpha-execution"),
    ("prime-execution", 8006, "ai.nexus.prime-execution"),
    ("oracle",          8007, "ai.nexus.oracle"),
    ("ails",            8008, "ai.nexus.ails"),
    ("guardian-angel",  8009, "ai.nexus.guardian-angel"),
]

# How long stale_deploy must persist before auto-restart (seconds)
STALE_THRESHOLD_SECONDS = 10 * 60  # 10 minutes

# Per-service first-seen-stale timestamps
_stale_since: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

def _probe_health(name: str, port: int) -> Optional[dict]:
    """Probe a service /health endpoint. Returns parsed JSON or None."""
    try:
        import requests
        r = requests.get(f"http://localhost:{port}/health", timeout=5)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        logger.debug("Health probe failed for %s: %s", name, e)
        return None


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def _restart_service(name: str, plist_id: str) -> bool:
    """
    Restart a service via launchctl. Returns True if restart command succeeded.
    Does not verify recovery — caller handles verification.
    """
    try:
        subprocess.run(
            ["launchctl", "stop", plist_id],
            check=False, capture_output=True, timeout=10,
        )
        time.sleep(2)
        subprocess.run(
            ["launchctl", "start", plist_id],
            check=False, capture_output=True, timeout=10,
        )
        logger.info("Restart issued for %s (%s)", name, plist_id)
        return True
    except Exception as e:
        logger.error("Restart failed for %s: %s", name, e)
        return False


def _verify_recovery(name: str, port: int, wait_sec: int = 15) -> bool:
    """
    Wait up to wait_sec seconds for service to report healthy.
    Returns True only if HTTP 200 AND body status in ("healthy", "active").
    """
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        health = _probe_health(name, port)
        if health and health.get("status") in ("healthy", "active"):
            return True
        time.sleep(3)
    return False


# ---------------------------------------------------------------------------
# Main sweep — call this on a schedule (every 2 min during market hours)
# ---------------------------------------------------------------------------

def check_stale_deploys() -> list[dict]:
    """
    Sweep all services for stale_deploy:true.
    Auto-restarts any service stale for >STALE_THRESHOLD_SECONDS.

    Returns list of action dicts (for logging/testing).
    Never raises.
    """
    from shared.resilience.triad_db import log_intervention, log_deployment
    from shared.resilience.alert_router import route_alert

    actions = []
    now = time.time()

    for name, port, plist_id in SERVICES:
        try:
            health = _probe_health(name, port)
            if health is None:
                # Service unreachable — not a stale deploy issue, different monitor handles this
                _stale_since.pop(name, None)
                continue

            is_stale = health.get("stale_deploy", False)

            if not is_stale:
                _stale_since.pop(name, None)
                continue

            # First time seeing this service as stale
            if name not in _stale_since:
                _stale_since[name] = now
                logger.info("Service %s stale_deploy detected — starting timer", name)
                continue

            stale_age = now - _stale_since[name]
            if stale_age < STALE_THRESHOLD_SECONDS:
                logger.debug("Service %s stale for %.0fs — threshold not reached", name, stale_age)
                continue

            # Threshold exceeded — auto-restart
            logger.warning(
                "Service %s stale_deploy for %.0fmin — auto-restarting",
                name, stale_age / 60,
            )

            pre_hash = health.get("code_hash", "unknown")
            restart_ok = _restart_service(name, plist_id)

            if not restart_ok:
                route_alert(
                    agent="genesis",
                    issue_type="stale_deploy_restart_failed",
                    target=name,
                    severity="P1",
                    detail=f"Auto-restart command failed for {name} (stale for {stale_age/60:.0f}min)",
                )
                log_intervention(
                    agent="genesis",
                    action_type="service_restart",
                    target=name,
                    trigger=f"stale_deploy:{stale_age:.0f}s",
                    outcome="failed",
                    pre_state={"code_hash": pre_hash, "stale_age_sec": stale_age},
                    error="launchctl restart command failed",
                )
                actions.append({"service": name, "outcome": "restart_failed"})
                continue

            # Wait and verify
            time.sleep(5)
            recovered = _verify_recovery(name, port, wait_sec=30)
            post_health = _probe_health(name, port)
            post_hash = (post_health or {}).get("code_hash", "unknown")
            still_stale = (post_health or {}).get("stale_deploy", True)

            if recovered and not still_stale:
                logger.info("Service %s auto-restart successful — stale_deploy cleared", name)
                _stale_since.pop(name, None)
                log_intervention(
                    agent="genesis",
                    action_type="service_restart",
                    target=name,
                    trigger=f"stale_deploy:{stale_age:.0f}s",
                    outcome="recovered",
                    command=f"launchctl stop {plist_id} && launchctl start {plist_id}",
                    pre_state={"code_hash": pre_hash, "stale_deploy": True},
                    post_state={"code_hash": post_hash, "stale_deploy": False},
                )
                actions.append({"service": name, "outcome": "recovered", "pre_hash": pre_hash, "post_hash": post_hash})
            else:
                logger.error("Service %s still stale after restart — escalating", name)
                route_alert(
                    agent="genesis",
                    issue_type="stale_deploy_persists",
                    target=name,
                    severity="P1",
                    detail=f"{name} still stale after auto-restart (age {stale_age/60:.0f}min)",
                    context={"action_taken": f"launchctl restart {plist_id}"},
                )
                log_intervention(
                    agent="genesis",
                    action_type="service_restart",
                    target=name,
                    trigger=f"stale_deploy:{stale_age:.0f}s",
                    outcome="escalated",
                    command=f"launchctl stop {plist_id} && launchctl start {plist_id}",
                    pre_state={"code_hash": pre_hash},
                    post_state={"code_hash": post_hash, "still_stale": still_stale},
                )
                actions.append({"service": name, "outcome": "escalated"})

        except Exception as e:
            logger.error("check_stale_deploys error for %s (non-fatal): %s", name, e)

    return actions
