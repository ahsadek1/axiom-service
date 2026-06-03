"""
stale_monitor.py — GENESIS Stale Deploy Detection + Auto-Restart (G2).
Spec: genesis-resilience-v1.md v1.2

Polls all service /health endpoints every 2 minutes (via external scheduler).
If stale_deploy=true for >10 minutes: auto-restarts via launchctl.
Verifies health after restart.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("genesis.resilience.stale_monitor")

# ── Service registry ──────────────────────────────────────────────────────────
SERVICES: list[tuple[str, int]] = [
    ("axiom",           8001),
    ("alpha-buffer",    8002),
    ("prime-buffer",    8003),
    ("omni",            8004),
    ("alpha-execution", 8005),
    ("prime-execution", 8006),
    ("oracle",          8007),
    ("ails",            8008),
    ("guardian-angel",  8009),
]

STALE_THRESHOLD_MINUTES = 10
HEALTH_TIMEOUT_SECONDS  = 5

# Internal state: service_name → unix timestamp when stale first detected
_stale_since: dict[str, float] = {}


def check_stale_deploys() -> list[str]:
    """
    Poll all service /health endpoints and identify stale deployments.

    A service is considered stale if its /health response contains
    `stale_deploy: true` AND has been in that state for >STALE_THRESHOLD_MINUTES.

    Updates internal _stale_since tracking dict.
    Never raises.

    Returns:
        List of service names that have been stale for >10 minutes.
    """
    stale_services: list[str] = []
    now = time.time()

    for name, port in SERVICES:
        try:
            r = requests.get(
                f"http://localhost:{port}/health",
                timeout=HEALTH_TIMEOUT_SECONDS
            )
            body = r.json() if r.status_code == 200 else {}
            is_stale = bool(body.get("stale_deploy", False))

            if is_stale:
                if name not in _stale_since:
                    _stale_since[name] = now
                    logger.info("stale_deploy detected for %s — starting timer", name)
                else:
                    stale_minutes = (now - _stale_since[name]) / 60
                    if stale_minutes > STALE_THRESHOLD_MINUTES:
                        logger.warning(
                            "Service %s stale for %.1f min (threshold %d) — needs restart",
                            name, stale_minutes, STALE_THRESHOLD_MINUTES
                        )
                        stale_services.append(name)
            else:
                # Service is fresh — clear any stale tracking
                if name in _stale_since:
                    logger.info("Service %s is no longer stale — clearing timer", name)
                    del _stale_since[name]

        except Exception as e:
            logger.error("check_stale_deploys: failed to check %s: %s", name, e)
            # Do not mark as stale on connectivity failure — that's a different issue

    return stale_services


def _get_launchctl_label(service_name: str) -> str:
    """
    Map service name to launchctl label.

    Args:
        service_name: Nexus service name (e.g. "alpha-execution").

    Returns:
        launchctl domain label string.
    """
    label_map = {
        "axiom":           "ai.nexus.axiom",
        "alpha-buffer":    "ai.nexus.alpha-buffer",
        "prime-buffer":    "ai.nexus.prime-buffer",
        "omni":            "ai.nexus.omni",
        "alpha-execution": "ai.nexus.alpha-execution",
        "prime-execution": "ai.nexus.prime-execution",
        "oracle":          "ai.nexus.oracle",
        "ails":            "ai.nexus.ails",
        "guardian-angel":  "ai.nexus.guardian-angel",
    }
    return label_map.get(service_name, f"ai.nexus.{service_name}")


def auto_restart_stale(service_name: str) -> bool:
    """
    Restart a stale service via launchctl and verify health.

    Steps:
        1. launchctl kickstart (or stop + start)
        2. Wait 5 seconds for service to come up
        3. GET /health — check status field

    Args:
        service_name: Name of the service to restart.

    Returns:
        True if health check passes post-restart, False otherwise.
    """
    label = _get_launchctl_label(service_name)
    port = next((p for n, p in SERVICES if n == service_name), None)
    if port is None:
        logger.error("auto_restart_stale: unknown service %s", service_name)
        return False

    logger.info("auto_restart_stale: restarting %s (label=%s)", service_name, label)

    try:
        # Stop then kickstart
        subprocess.run(
            ["launchctl", "stop", label],
            capture_output=True, timeout=10, check=False
        )
        time.sleep(1)
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
            capture_output=True, timeout=15, check=False
        )
        if result.returncode != 0:
            # Fallback: try start
            subprocess.run(
                ["launchctl", "start", label],
                capture_output=True, timeout=10, check=False
            )
    except Exception as e:
        logger.error("auto_restart_stale: launchctl failed for %s: %s", service_name, e)
        return False

    # Wait for service to initialize
    time.sleep(5)

    # Verify health
    try:
        r = requests.get(f"http://localhost:{port}/health", timeout=HEALTH_TIMEOUT_SECONDS)
        body = r.json() if r.status_code == 200 else {}
        healthy = r.status_code == 200 and body.get("status") in ("healthy", "active")
        still_stale = bool(body.get("stale_deploy", False))

        if healthy and not still_stale:
            # Clear stale timer on successful restart
            _stale_since.pop(service_name, None)
            logger.info("auto_restart_stale: %s restarted successfully", service_name)
            return True
        else:
            logger.warning(
                "auto_restart_stale: %s restart — health=%s stale=%s",
                service_name, healthy, still_stale
            )
            return False
    except Exception as e:
        logger.error("auto_restart_stale: post-restart health check failed for %s: %s", service_name, e)
        return False
