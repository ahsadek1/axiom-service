"""
restart_cluster_detector.py — SOVEREIGN Amendment S2

Detects service restart clusters — multiple services restarting in a short
window — which is qualitatively different from any single restart.

ROOT CAUSE CAUGHT: 2026-04-29, multiple services (Axiom, Prime, Alpha, OMNI,
Sage) restarted within 20-minute windows throughout the day. No existing monitor
raised a cluster flag. Each restart was evaluated in isolation. The pattern was
completely invisible.

A multi-service cluster is a P0: it indicates a systemic issue (OS memory
pressure, shared dependency failure, process manager thrashing) rather than
an isolated service failure.

A single-service thrash (same service restarting 3+ times in 20 min) is P1.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

import config

logger = logging.getLogger("integrity.restart_cluster")

CHRONICLE_DB: str = "/Users/ahmedsadek/nexus/data/chronicle.db"

# Cluster detection parameters
CLUSTER_WINDOW_MINUTES: int = 20
MULTI_SERVICE_CLUSTER_THRESHOLD: int = 3   # ≥3 distinct services = P0 cluster
SINGLE_SERVICE_THRASH_THRESHOLD: int = 3   # same service ≥3 restarts = P1 thrash

# Services to monitor (all local Nexus services)
MONITORED_SERVICES: Dict[str, str] = {
    "alpha-execution":  "http://localhost:8005/health",
    "prime-execution":  "http://localhost:8006/health",
    "omni":             "http://localhost:8004/health",
    "axiom":            "http://localhost:8001/health",
    "alpha-buffer":     "http://localhost:8002/health",
    "prime-buffer":     "http://localhost:8003/health",
    "oracle":           "http://localhost:8007/health",
    "ails":             "http://localhost:8008/health",
    "pipeline-sentinel":"http://localhost:8010/health",
    "sage":             "http://localhost:9003/health",
    "atlas":            "http://localhost:9002/health",
    "cipher":           "http://localhost:9001/health",
    "vector":           "http://localhost:8030/vector/health",
}


@dataclass
class ServiceUptimeSnapshot:
    service: str
    uptime_since: Optional[str]    # ISO string from /health
    uptime_minutes: Optional[float]
    reachable: bool
    checked_at: float = field(default_factory=time.time)


@dataclass
class ClusterAlert:
    kind: str           # "MULTI_SERVICE_CLUSTER" | "SINGLE_SERVICE_THRASH" | "NONE"
    severity: str       # "P0" | "P1" | "NONE"
    services: List[str] = field(default_factory=list)
    restart_count: int = 0
    message: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def is_alert(self) -> bool:
        return self.kind != "NONE"


# ---------------------------------------------------------------------------
# Uptime tracking — detect restarts by comparing uptime_since to previous check
# ---------------------------------------------------------------------------

_previous_uptime_since: Dict[str, Optional[str]] = {}


def snapshot_service_uptimes() -> Dict[str, ServiceUptimeSnapshot]:
    """
    Fetch /health for all monitored services and extract uptime_since.

    Returns dict of service_name → ServiceUptimeSnapshot.
    """
    snapshots: Dict[str, ServiceUptimeSnapshot] = {}

    for service, url in MONITORED_SERVICES.items():
        try:
            resp = requests.get(url, timeout=4.0)
            if resp.status_code == 200:
                data = resp.json()
                uptime_since = (
                    data.get("uptime_since")
                    or data.get("started_at")
                    or data.get("start_time")
                )
                uptime_minutes = _parse_uptime_minutes(uptime_since)
                snapshots[service] = ServiceUptimeSnapshot(
                    service=service,
                    uptime_since=uptime_since,
                    uptime_minutes=uptime_minutes,
                    reachable=True,
                )
            else:
                snapshots[service] = ServiceUptimeSnapshot(
                    service=service, uptime_since=None,
                    uptime_minutes=None, reachable=False,
                )
        except requests.RequestException:
            snapshots[service] = ServiceUptimeSnapshot(
                service=service, uptime_since=None,
                uptime_minutes=None, reachable=False,
            )

    return snapshots


def detect_restarts(
    current: Dict[str, ServiceUptimeSnapshot],
    previous: Dict[str, Optional[str]],
) -> List[str]:
    """
    Compare current uptime_since to previous to detect restarts.

    A restart is detected when:
    - The service was reachable before (uptime_since existed)
    - The uptime_since has changed (new startup time)

    Returns list of service names that restarted.
    """
    restarted: List[str] = []

    for service, snapshot in current.items():
        if not snapshot.reachable:
            continue
        prev_since = previous.get(service)
        curr_since = snapshot.uptime_since

        if prev_since is None:
            # First observation — no comparison possible
            continue

        # Truncate to second precision before comparing — sub-second differences
        # (e.g. from multi-worker services where uptime_since is derived from
        # process create_time with floating-point microseconds) are not real restarts.
        curr_trunc = curr_since[:19] if curr_since else curr_since
        prev_trunc = prev_since[:19] if prev_since else prev_since

        if curr_trunc and prev_trunc and curr_trunc != prev_trunc:
            restarted.append(service)
            logger.info(
                "RESTART DETECTED: %s | prev_since=%s new_since=%s",
                service, prev_since, curr_since,
            )

    return restarted


def check_cluster(recent_restarts: List[Dict]) -> ClusterAlert:
    """
    Analyze recent restart events for cluster patterns.

    Args:
        recent_restarts: List of {service, ts} dicts from restart event log.

    Returns:
        ClusterAlert — kind=NONE if no cluster, P0 or P1 if cluster detected.
    """
    if not recent_restarts:
        return ClusterAlert(kind="NONE", severity="NONE")

    # Count by service
    counts: Dict[str, int] = {}
    for event in recent_restarts:
        svc = event.get("service", "unknown")
        counts[svc] = counts.get(svc, 0) + 1

    distinct_services = len(counts)
    total_events = sum(counts.values())

    # Single-service thrash (P1)
    for service, count in counts.items():
        if count >= SINGLE_SERVICE_THRASH_THRESHOLD:
            msg = (
                f"SINGLE-SERVICE THRASH: {service} restarted {count}x in "
                f"{CLUSTER_WINDOW_MINUTES} min. Root cause unknown. "
                f"Likely: launchd restart loop, OOM kill, or dependency failure."
            )
            logger.warning(msg)
            return ClusterAlert(
                kind="SINGLE_SERVICE_THRASH",
                severity="P1",
                services=[service],
                restart_count=count,
                message=msg,
            )

    # Multi-service cluster (P0)
    if distinct_services >= MULTI_SERVICE_CLUSTER_THRESHOLD:
        names = list(counts.keys())
        msg = (
            f"CLUSTER P0: {distinct_services} services restarted "
            f"({total_events} total events) in {CLUSTER_WINDOW_MINUTES} min. "
            f"Services: {', '.join(names)}. "
            f"Check: OS memory pressure, shared dependency (Chronicle/Bus/network), "
            f"launchd misconfiguration."
        )
        logger.error(msg)
        return ClusterAlert(
            kind="MULTI_SERVICE_CLUSTER",
            severity="P0",
            services=names,
            restart_count=total_events,
            message=msg,
        )

    return ClusterAlert(kind="NONE", severity="NONE")


def _parse_uptime_minutes(uptime_since: Optional[str]) -> Optional[float]:
    """
    Parse an ISO 8601 uptime_since string and return minutes since that time.

    Returns None if parsing fails.
    """
    if not uptime_since:
        return None
    try:
        # Handle both offset-aware and offset-naive strings
        dt_str = uptime_since
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        return delta.total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Persistent restart event log (in-process, written to CHRONICLE on events)
# ---------------------------------------------------------------------------

_restart_event_log: List[Dict] = []  # [{service, ts}]


def record_restart(service: str) -> None:
    """Record a restart event in the in-process log."""
    now = time.time()
    _restart_event_log.append({"service": service, "ts": now})
    # Prune events older than the cluster window
    cutoff = now - (CLUSTER_WINDOW_MINUTES * 60)
    _restart_event_log[:] = [e for e in _restart_event_log if e["ts"] >= cutoff]


def get_recent_restart_events() -> List[Dict]:
    """Return restart events within the cluster window."""
    cutoff = time.time() - (CLUSTER_WINDOW_MINUTES * 60)
    return [e for e in _restart_event_log if e["ts"] >= cutoff]


def update_uptime_baselines(
    current: Dict[str, ServiceUptimeSnapshot]
) -> Dict[str, Optional[str]]:
    """
    Update the previous uptime_since baseline from current snapshot.
    Returns new baseline dict.
    """
    return {
        svc: snap.uptime_since
        for svc, snap in current.items()
        if snap.reachable
    }
