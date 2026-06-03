"""
escalator.py — Sends escalation alerts to SOVEREIGN via Nexus message bus.

Primary target: agent:sovereign:main (OpenClaw session).
Fallback: Nexus message bus at NEXUS_BUS_URL.

This module is read-only from the Nexus perspective — it sends messages only.
Never modifies service state or takes corrective action.
"""

import logging

import requests

from config import NEXUS_BUS_URL
from models import CycleResult

logger = logging.getLogger(__name__)


def _format_message(result: CycleResult) -> str:
    """
    Format a human-readable escalation message for SOVEREIGN.

    Args:
        result: CycleResult containing diagnosis and service state.

    Returns:
        Formatted multi-line string for SOVEREIGN consumption.
    """
    severity = result.diagnosis.severity
    ts = result.cycle_ts[:16].replace("T", " ")

    alive = [name for name, snap in result.services.items() if snap.status == "UP"]
    scanner_statuses = []
    for name in ("cipher", "atlas", "sage"):
        snap = result.services.get(name)
        status = snap.status if snap else "UNKNOWN"
        scanner_statuses.append(f"{name} {status}")

    v1 = result.alpaca.get("v1")
    v2 = result.alpaca.get("v2")
    v1_str = (
        f"${v1.buying_power:,.0f}" if v1 and v1.status == "UP" else "UNAVAILABLE"
    )
    v2_str = (
        f"${v2.buying_power:,.0f}" if v2 and v2.status == "UP" else "UNAVAILABLE"
    )
    action = "YES" if severity == "CRITICAL" else "MONITOR"

    return (
        f"OUTCOME MONITOR [{severity}] — {ts} ET\n\n"
        f"Zero trades in last 30 min.\n"
        f"Diagnosis: {result.diagnosis.diagnosis}\n"
        f"Details: {result.diagnosis.details}\n\n"
        f"Services alive: {', '.join(alive)}\n"
        f"Scanners: {', '.join(scanner_statuses)}\n"
        f"Alpaca V1: {v1_str} | V2: {v2_str}\n\n"
        f"Action required: {action}"
    )


def escalate_to_sovereign(result: CycleResult) -> bool:
    """
    Send an escalation alert to SOVEREIGN via the Nexus message bus.

    Args:
        result: CycleResult with an escalation-triggering diagnosis.

    Returns:
        True if the message bus accepted the message, False on failure.
    """
    message = _format_message(result)
    payload = {
        "from": "outcome-monitor",
        "to": "sovereign",
        "message": message,
    }
    try:
        resp = requests.post(
            f"{NEXUS_BUS_URL}/send", json=payload, timeout=8
        )
        if resp.status_code in (200, 201):
            logger.info(
                "Escalated to SOVEREIGN: %s [%s]",
                result.diagnosis.diagnosis,
                result.diagnosis.severity,
            )
            return True
        logger.error(
            "Nexus bus returned HTTP %d for escalation", resp.status_code
        )
        return False
    except Exception as e:
        logger.error("Escalation to SOVEREIGN failed: %s", e)
        return False
