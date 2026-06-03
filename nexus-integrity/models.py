"""
models.py — Shared data models for nexus-integrity service.

All dataclasses and enums used across modules are defined here.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class CompositeColor(str, Enum):
    """Trading readiness color band."""
    GREEN = "GREEN"   # 90-100 — full capacity
    AMBER = "AMBER"   # 60-89  — cap size 50%
    RED = "RED"       # 30-59  — halt new entries
    BLACK = "BLACK"   # 0-29   — halt everything + emergency alert


class AlertTier(str, Enum):
    """Alert severity tier (V9 amendment: derived from TRS sub-component weights)."""
    P0 = "P0"   # TRS=0 or store dead — immediate page Ahmed
    P1 = "P1"   # TRS < GREEN threshold — Health Group alert, ACK within 15 min
    P2 = "P2"   # Single probe failure, TRS >= GREEN — Health Group alert, suppressed 30 min
    P3 = "P3"   # Anomaly detected, system healthy — CHRONICLE only
    P4 = "P4"   # Informational — daily digest only


class ProbeResult(str, Enum):
    """Result of an execution probe."""
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"   # probe too recent (within min interval)
    MUTEX_BUSY = "mutex_busy"


class StageResult(str, Enum):
    """Result of a pipeline flow verification stage."""
    PASS = "pass"
    FAIL = "fail"
    FIXED = "fixed"       # failed then auto-remediated successfully
    ESCALATED = "escalated"  # failed, fix attempted, recovery assert failed


@dataclass
class TRSResult:
    """Result returned by TRS lookup. Fail-closed: score=0 on any error (V11 amendment)."""
    score: float                      # 0–100
    block: bool                       # True = trading blocked
    reason: str = ""                  # Human-readable reason
    color: CompositeColor = CompositeColor.BLACK
    component_scores: Dict[str, float] = field(default_factory=dict)
    age_seconds: float = 0.0          # How old the TRS entry is
    alert_tier: AlertTier = AlertTier.P0

    def __post_init__(self) -> None:
        """Derive color and alert_tier from score."""
        if self.score >= 90:
            self.color = CompositeColor.GREEN
        elif self.score >= 60:
            self.color = CompositeColor.AMBER
        elif self.score >= 30:
            self.color = CompositeColor.RED
        else:
            self.color = CompositeColor.BLACK

        # Derive alert tier from score + block flag (V9 amendment)
        if self.block or self.score == 0:
            self.alert_tier = AlertTier.P0
        elif self.score < 90:
            self.alert_tier = AlertTier.P1
        else:
            self.alert_tier = AlertTier.P3  # healthy but something sub-optimal


@dataclass
class ComponentScore:
    """Score for a single TRS component."""
    name: str
    score: float          # 0–100 for this component
    weight: float         # weight in composite (e.g. 25.0 = 25%)
    weighted_score: float # score * weight / 100
    healthy: bool
    detail: str = ""
    latency_ms: float = 0.0


@dataclass
class FlowStageResult:
    """Result of a single pipeline flow verification stage."""
    stage: int
    name: str
    result: StageResult
    detail: str = ""
    fix_attempted: bool = False
    fix_succeeded: bool = False
    latency_ms: float = 0.0


@dataclass
class CanaryResult:
    """Result of a canary run."""
    canary_type: str         # "pre_market" | "intraday"
    passed: bool
    stage_reached: int       # 1-4
    sla_breach: bool         # took longer than expected
    duration_ms: float
    detail: str = ""
    cleanup_confirmed: bool = True


@dataclass
class ProbeRunResult:
    """Result of an options execution probe (V12 amendment)."""
    result: ProbeResult
    step_failed: Optional[int] = None   # 1-6 step that failed, if any
    order_id: Optional[str] = None
    occ_symbol: Optional[str] = None
    duration_ms: float = 0.0
    detail: str = ""


@dataclass
class HealthEvent:
    """A single health event written to CHRONICLE health_events table (V14 amendment)."""
    source: str           # authoritative writer only
    service: str
    check_type: str
    result: str           # "pass" | "fail" | "warn"
    latency_ms: float = 0.0
    detail: str = ""
    trs_weight: float = 0.0
    schema_version: str = "2.1"
