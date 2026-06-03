"""
intervention_lock.py — NSP Intervention Lock (message bus claim protocol).

Spec (Escalation section):
  1. NSP posts claim to message bus:
       {service, incident_id, lock_holder: "nsp", expires_at: T+300}
  2. Escalation broadcast goes to all 5 agents
  3. Each agent checks bus for active claim before acting
  4. If locked by another agent: observe and advise only
  5. Lock expires after 5 minutes if no resolution confirmed

Also handles the EXTERNAL_SHADOW mode — when an external API (Polygon/Alpaca)
is down, NSP posts a MAINTENANCE_HOLD claim that suppresses anomaly alerts
on dependent services until the external source recovers.

Public API:
    claim_lock(service, incident_id)     → posts claim to bus, returns True/False
    release_lock(incident_id)            → posts resolution to bus
    check_active_claim(service)          → returns claim dict or None
    broadcast_escalation(incident_id, package)  → sends to all 5 agents
    post_maintenance_hold(source, affected)      → EXTERNAL_SHADOW hold
    lift_maintenance_hold(source)               → clears hold
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("nsp.intervention_lock")

MESSAGE_BUS_URL: str = os.environ.get("MESSAGE_BUS_URL", "http://192.168.1.141:9999")
LOCK_TTL_SECONDS: int = 300  # 5 minutes per spec

# Agent identities for broadcast
ESCALATION_AGENTS: List[str] = ["vector", "cipher", "axiom", "genesis", "sovereign"]

# In-memory cache of active locks (keyed by service name)
# Source of truth is the bus, but this avoids redundant bus queries
_active_locks: Dict[str, Dict] = {}
_maintenance_holds: Dict[str, Dict] = {}


def _post_bus(payload: Dict[str, Any], timeout: int = 5) -> bool:
    """
    POST a message to the Nexus message bus.

    Returns True on success, False on failure (never raises).
    """
    try:
        resp = requests.post(
            f"{MESSAGE_BUS_URL}/send",
            json=payload,
            timeout=timeout,
        )
        if resp.status_code not in (200, 201, 202):
            logger.warning("Bus POST returned HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        logger.warning("Bus POST failed: %s", exc)
        return False


def claim_lock(service: str, incident_id: str) -> bool:
    """
    Post an intervention lock claim to the message bus.

    Other agents must check for an active claim before acting on the same service.
    Lock expires after LOCK_TTL_SECONDS if not explicitly released.

    Parameters:
        service:     The service being intervened on.
        incident_id: Unique identifier for this incident.

    Returns:
        True if claim was posted successfully, False otherwise.
    """
    expires_at = time.time() + LOCK_TTL_SECONDS
    claim = {
        "service": service,
        "incident_id": incident_id,
        "lock_holder": "nsp",
        "expires_at": expires_at,
        "claimed_at": time.time(),
    }

    success = _post_bus({
        "from": "nsp",
        "to": "all",
        "type": "intervention_lock_claim",
        "message": f"NSP claiming intervention lock on {service} (incident={incident_id})",
        "payload": claim,
    })

    if success:
        _active_locks[service] = claim
        logger.info("Lock claimed: service=%s incident=%s expires_at=T+%ds", service, incident_id, LOCK_TTL_SECONDS)
    else:
        logger.warning("Failed to post lock claim to bus — proceeding without distributed lock")

    return success


def release_lock(incident_id: str, service: str, outcome: str) -> bool:
    """
    Release an intervention lock by posting a resolution to the message bus.

    Parameters:
        incident_id: The incident ID from claim_lock().
        service:     The service that was being intervened on.
        outcome:     Brief outcome description ("resolved", "escalated", etc.)

    Returns:
        True if release was posted successfully.
    """
    success = _post_bus({
        "from": "nsp",
        "to": "all",
        "type": "intervention_lock_release",
        "message": f"NSP releasing lock on {service} (incident={incident_id}, outcome={outcome})",
        "payload": {
            "service": service,
            "incident_id": incident_id,
            "outcome": outcome,
            "released_at": time.time(),
        },
    })

    _active_locks.pop(service, None)
    logger.info("Lock released: service=%s incident=%s outcome=%s", service, incident_id, outcome)
    return success


def check_active_claim(service: str) -> Optional[Dict]:
    """
    Check if there is a non-expired active lock for a service.

    Returns the claim dict if active, None if no active claim.
    Note: Checks in-memory cache only — does not query the bus.
    """
    claim = _active_locks.get(service)
    if claim and time.time() < claim.get("expires_at", 0):
        return claim
    if service in _active_locks:
        _active_locks.pop(service, None)
    return None


def broadcast_escalation(
    incident_id: str,
    service: str,
    failure_class: str,
    signal_chain: List[str],
    attempt_log: List[str],
    agent_packages: Dict[str, Dict],
) -> None:
    """
    Broadcast escalation packages to all 5 agents via the message bus.

    Each agent receives a tailored package per the spec's differentiated
    agent packages section.

    Parameters:
        incident_id:    Unique incident ID.
        service:        Failing service name.
        failure_class:  Root cause class string.
        signal_chain:   List of signals that led to escalation.
        attempt_log:    List of fix attempts made before escalation.
        agent_packages: Dict of {agent_name: package_dict}.
    """
    base_payload = {
        "incident_id": incident_id,
        "service": service,
        "failure_class": failure_class,
        "signal_chain": signal_chain,
        "attempt_log": attempt_log,
        "escalated_at": time.time(),
    }

    for agent in ESCALATION_AGENTS:
        package = agent_packages.get(agent, {})
        payload = {**base_payload, "agent_package": package}
        _post_bus({
            "from": "nsp",
            "to": agent,
            "type": "escalation",
            "message": (
                f"🚨 NSP ESCALATION: {service} ({failure_class}) "
                f"— {len(attempt_log)} fix attempts failed. Action required."
            ),
            "payload": payload,
        })

    logger.warning(
        "Escalation broadcast: incident=%s service=%s class=%s attempts=%d",
        incident_id, service, failure_class, len(attempt_log),
    )


def post_maintenance_hold(source: str, affected_services: List[str]) -> None:
    """
    Post an EXTERNAL_SHADOW maintenance hold when an external API is down.

    Downstream services are suppressed from anomaly detection until lifted.

    Parameters:
        source:            The external source that is down ("polygon", "alpaca").
        affected_services: Services to suppress during the hold.
    """
    hold = {
        "source": source,
        "affected_services": affected_services,
        "posted_at": time.time(),
    }
    _maintenance_holds[source] = hold

    _post_bus({
        "from": "nsp",
        "to": "all",
        "type": "maintenance_hold",
        "message": (
            f"⚠️ MAINTENANCE_HOLD: {source} external API down. "
            f"Suppressing anomaly detection on: {', '.join(affected_services)}"
        ),
        "payload": hold,
    })

    logger.warning("MAINTENANCE_HOLD posted: source=%s suppressed=%s", source, affected_services)


def lift_maintenance_hold(source: str) -> None:
    """
    Lift a maintenance hold when the external source recovers.

    Parameters:
        source: The external source that has recovered.
    """
    hold = _maintenance_holds.pop(source, None)
    if not hold:
        logger.debug("lift_maintenance_hold: no active hold for %s", source)
        return

    _post_bus({
        "from": "nsp",
        "to": "all",
        "type": "maintenance_hold_lifted",
        "message": f"✅ MAINTENANCE_HOLD lifted: {source} external API recovered",
        "payload": {"source": source, "lifted_at": time.time()},
    })

    logger.info("MAINTENANCE_HOLD lifted: source=%s", source)


def is_under_maintenance_hold(service: str) -> bool:
    """Return True if service is currently suppressed by a maintenance hold."""
    now = time.time()
    for hold in _maintenance_holds.values():
        if service in hold.get("affected_services", []):
            return True
    return False
