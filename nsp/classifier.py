"""
classifier.py — NSP Root Cause Classifier.

Implements the 8 failure classes from the spec using explicit decision trees,
not heuristics. Each class has a defined action and escalation path.

Classes (in priority order):
  DEAD          — process gone from ps, 3+ consecutive poll failures
  UNRESPONSIVE  — process alive but not responding
  CASCADING     — same service restarted 3+ times in 10 min OR 3+ services failed in 60s
  EXTERNAL      — Polygon or Alpaca returning 503
  STARVED       — service degraded because its dependency is down
  DEGRADED      — process alive, specific signal degradation
  CONFIG_DRIFT  — running config differs from known-good snapshot
  CODE          — SQLite OperationalError / TypeError / logic exception in logs

Usage:
    from classifier import classify, FailureClass, ClassificationResult
    result = classify(service_name, telemetry_snapshot, state_context)
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import psutil

logger = logging.getLogger("nsp.classifier")


class FailureClass(str, Enum):
    """All possible root-cause classifications."""
    DEAD = "DEAD"
    UNRESPONSIVE = "UNRESPONSIVE"
    CASCADING = "CASCADING"
    EXTERNAL = "EXTERNAL"
    STARVED = "STARVED"
    DEGRADED = "DEGRADED"
    CONFIG_DRIFT = "CONFIG_DRIFT"
    CODE = "CODE"
    HEALTHY = "HEALTHY"


# Observation windows before next attempt (seconds)
OBSERVATION_WINDOWS: Dict[FailureClass, Tuple[int, int]] = {
    FailureClass.DEAD:         (20, 45),
    FailureClass.UNRESPONSIVE: (60, 120),
    FailureClass.DEGRADED:     (120, 300),
    # CASCADING / EXTERNAL / CODE / CONFIG_DRIFT → skip to escalation
}

# Dependency map: if service X is down, these services may appear STARVED
SERVICE_DEPENDENCIES: Dict[str, List[str]] = {
    "alpha-buffer":  ["omni"],
    "prime-buffer":  ["omni"],
    "alpha-exec":    ["alpha-buffer", "axiom"],
    "prime-exec":    ["prime-buffer", "axiom"],
    "omni":          ["oracle"],
    "oracle":        ["axiom"],
    "cipher":        ["axiom"],
    "atlas":         ["axiom"],
    "sage":          ["axiom"],
}


@dataclass
class ClassificationResult:
    """Result of a single classification decision."""
    failure_class: FailureClass
    service: str
    confidence: str                       # "HIGH" | "MEDIUM" | "LOW"
    signal_chain: List[str]               # ordered list of signals that led here
    recommended_action: str               # human-readable action
    auto_fixable: bool                    # can NSP attempt a fix autonomously?
    escalate_immediately: bool            # skip attempt loop, go straight to agents
    suppress_dependents: List[str] = field(default_factory=list)  # services to mute
    timestamp: float = field(default_factory=time.time)


def _process_alive(pid: Optional[int]) -> bool:
    """Return True if the given PID exists in the process table."""
    if pid is None:
        return False
    return psutil.pid_exists(pid)


def classify(
    service: str,
    consecutive_failures: int,
    process_pid: Optional[int],
    telemetry: Dict,
    service_states: Dict[str, str],   # {service_name: "UP"|"DOWN"|"DEGRADED"}
    recent_restart_count_10min: int,
    cascade_window_failures: int,     # how many services failed in last 60s
    external_polygon_503: bool,
    external_alpaca_503: bool,
    log_has_code_error: bool,
    running_config_hash: Optional[str],
    known_good_config_hash: Optional[str],
) -> ClassificationResult:
    """
    Classify the root cause of a service failure using explicit decision trees.

    Parameters mirror the spec's decision tree conditions exactly.
    Returns a ClassificationResult with recommended action.
    """

    # ── 1. EXTERNAL check (global, suppresses everything downstream) ───────
    if external_polygon_503 or external_alpaca_503:
        source = "Polygon" if external_polygon_503 else "Alpaca"
        return ClassificationResult(
            failure_class=FailureClass.EXTERNAL,
            service=service,
            confidence="HIGH",
            signal_chain=[f"{source} API returning 503"],
            recommended_action=f"MAINTENANCE_HOLD — suppress anomaly detection until {source} recovers",
            auto_fixable=False,
            escalate_immediately=False,
            suppress_dependents=list(SERVICE_DEPENDENCIES.get(service, [])),
        )

    # ── 2. CODE check (explicit errors in logs — no infra fix) ────────────
    if log_has_code_error:
        return ClassificationResult(
            failure_class=FailureClass.CODE,
            service=service,
            confidence="HIGH",
            signal_chain=["SQLite OperationalError / TypeError / logic exception in logs"],
            recommended_action="Escalate GENESIS immediately — no infra fix attempted",
            auto_fixable=False,
            escalate_immediately=True,
        )

    # ── 3. CASCADING check ─────────────────────────────────────────────────
    if recent_restart_count_10min >= 3:
        return ClassificationResult(
            failure_class=FailureClass.CASCADING,
            service=service,
            confidence="HIGH",
            signal_chain=[f"Service restarted {recent_restart_count_10min}x in last 10 minutes"],
            recommended_action="FREEZE this service — escalate immediately",
            auto_fixable=False,
            escalate_immediately=True,
        )

    if cascade_window_failures >= 3:
        return ClassificationResult(
            failure_class=FailureClass.CASCADING,
            service=service,
            confidence="HIGH",
            signal_chain=[f"{cascade_window_failures} services failed within 60 seconds"],
            recommended_action="FREEZE all restarts — escalate immediately",
            auto_fixable=False,
            escalate_immediately=True,
        )

    # ── 4. CONFIG_DRIFT ────────────────────────────────────────────────────
    if (
        running_config_hash is not None
        and known_good_config_hash is not None
        and running_config_hash != known_good_config_hash
    ):
        return ClassificationResult(
            failure_class=FailureClass.CONFIG_DRIFT,
            service=service,
            confidence="HIGH",
            signal_chain=[
                f"running_config_hash={running_config_hash}",
                f"known_good_hash={known_good_config_hash}",
            ],
            recommended_action="Diff config + alert Ahmed — no auto-fix without approval",
            auto_fixable=False,
            escalate_immediately=True,
        )

    # ── 5. DEAD ────────────────────────────────────────────────────────────
    if consecutive_failures >= 3 and not _process_alive(process_pid):
        return ClassificationResult(
            failure_class=FailureClass.DEAD,
            service=service,
            confidence="HIGH",
            signal_chain=[
                f"consecutive_failed_polls={consecutive_failures}",
                "process not found in ps",
            ],
            recommended_action="Restart service",
            auto_fixable=True,
            escalate_immediately=False,
        )

    # ── 6. UNRESPONSIVE ────────────────────────────────────────────────────
    if consecutive_failures >= 3 and _process_alive(process_pid):
        return ClassificationResult(
            failure_class=FailureClass.UNRESPONSIVE,
            service=service,
            confidence="HIGH",
            signal_chain=[
                f"consecutive_failed_polls={consecutive_failures}",
                f"process alive (pid={process_pid})",
            ],
            recommended_action="Wait 30s, recheck before restarting",
            auto_fixable=True,
            escalate_immediately=False,
        )

    # ── 7. STARVED ─────────────────────────────────────────────────────────
    dependencies = SERVICE_DEPENDENCIES.get(service, [])
    down_deps = [d for d in dependencies if service_states.get(d) == "DOWN"]
    if down_deps and telemetry.get("status") == "degraded":
        return ClassificationResult(
            failure_class=FailureClass.STARVED,
            service=service,
            confidence="MEDIUM",
            signal_chain=[f"dependency {', '.join(down_deps)} is DOWN", "service shows degraded"],
            recommended_action=f"Fix dependency ({', '.join(down_deps)}) first — suppress {service} alerts",
            auto_fixable=True,  # fix is on the dependency, not this service
            escalate_immediately=False,
            suppress_dependents=[service],
        )

    # ── 8. DEGRADED ────────────────────────────────────────────────────────
    if telemetry.get("status") == "degraded":
        signals: List[str] = []
        action = "Investigate specific degradation signals"

        cache_warm = telemetry.get("cache_warm_count", 1)
        brain_error_rate = telemetry.get("brain_error_rate", 0.0)
        cb_status = telemetry.get("cb_status", "NORMAL")

        if cache_warm == 0:
            signals.append("cache_warm_count=0")
            action = "Trigger prefetch — NOT restart"
        elif isinstance(brain_error_rate, (int, float)) and brain_error_rate > 0.4:
            signals.append(f"brain_error_rate={brain_error_rate:.2%}")
            action = "Probe individual brain APIs"
        elif cb_status != "NORMAL":
            signals.append(f"cb_status={cb_status}")
            action = f"Alert (cb_status={cb_status}) — do NOT restart"

        if not signals:
            signals = ["status=degraded (no specific signal matched)"]

        return ClassificationResult(
            failure_class=FailureClass.DEGRADED,
            service=service,
            confidence="MEDIUM",
            signal_chain=signals,
            recommended_action=action,
            auto_fixable=True,
            escalate_immediately=False,
        )

    # ── 9. HEALTHY ─────────────────────────────────────────────────────────
    return ClassificationResult(
        failure_class=FailureClass.HEALTHY,
        service=service,
        confidence="HIGH",
        signal_chain=["No failure signals detected"],
        recommended_action="No action required",
        auto_fixable=False,
        escalate_immediately=False,
    )
