"""
models.py — Pydantic models for Pipeline Sentinel.

All models are Pydantic v1 compatible (Python 3.9).
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HopEnum(str, Enum):
    """Valid pipeline hops in order of execution.
    
    Note: buffer_accepted is deprecated (pipeline evolved to omni_started → omni_completed).
    Legacy entries in the database with buffer_accepted are treated as terminal hops
    for stall detection purposes.
    """
    axiom_push           = "axiom_push"
    agent_received       = "agent_received"
    omni_started         = "omni_started"
    omni_completed       = "omni_completed"
    execution_received   = "execution_received"
    alpaca_submitted     = "alpaca_submitted"
    alpaca_confirmed     = "alpaca_confirmed"


class ServiceEnum(str, Enum):
    """Valid service names in the Nexus pipeline."""
    axiom            = "axiom"
    cipher           = "cipher"
    atlas            = "atlas"
    sage             = "sage"
    alpha_buffer     = "alpha-buffer"
    prime_buffer     = "prime-buffer"
    omni             = "omni"
    alpha_execution  = "alpha-execution"
    prime_execution  = "prime-execution"


class PathwayEnum(str, Enum):
    """Trading pathway — Alpha (options) or Prime (swing equity)."""
    alpha = "alpha"
    prime = "prime"


class StatusEnum(str, Enum):
    """Hop status."""
    ok      = "ok"
    error   = "error"
    timeout = "timeout"


class TraceRequest(BaseModel):
    """Inbound trace hop from any Nexus service."""

    trace_id: str = Field(..., description="UUID4 identifying the pick across its full journey")
    hop:      HopEnum = Field(..., description="Which stage of the pipeline this hop represents")
    service:  ServiceEnum = Field(..., description="Service emitting this hop")
    ticker:   str = Field(..., description="Ticker symbol being processed")
    pathway:  PathwayEnum = Field(..., description="alpha or prime pathway")
    status:   StatusEnum = Field(StatusEnum.ok, description="Outcome of this hop")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Arbitrary context (e.g. latency_ms)")


class HealthScoreComponents(BaseModel):
    """Breakdown of what contributed to the current health score."""

    pipeline_completion_rate:    float = Field(..., description="0.0–1.0 fraction of recent picks that completed")
    inter_service_latency_p95_ms: float = Field(..., description="P95 inter-hop latency in milliseconds")
    omni_brain_latency_p95_ms:   float = Field(..., description="P95 OMNI synthesis latency in milliseconds")
    oracle_cache_freshness:      float = Field(1.0, description="1.0 = fresh, 0.0 = stale beyond threshold")
    active_anomaly_count:        int   = Field(0, description="Number of active GA anomaly reports")
    stalled_picks_count:         int   = Field(0, description="Number of currently stalled picks")


class SystemHealthResponse(BaseModel):
    """Full system health snapshot returned by /system-health."""

    health_score:               float = Field(..., description="0–100 composite health score")
    score_components:           HealthScoreComponents
    status:                     str   = Field(..., description="NOMINAL | DEGRADED | IMPAIRED | CRITICAL | EMERGENCY")
    recommended_size_multiplier: float = Field(..., description="1.0 at NOMINAL, 0.0 at CRITICAL/EMERGENCY")
    active_failures:            List[str] = Field(default_factory=list, description="Active failure class names")
    context_block:              Dict[str, Any] = Field(..., description="Compact block injected into every agent pool push")
    submissions_open:           bool  = Field(..., description="False when score < HEALTH_HALT_THRESHOLD")
    computed_at:                str   = Field(..., description="ISO8601 UTC timestamp of computation")
    stale:                      bool  = Field(False, description="True if score could not be freshly computed")


class FailureEventModel(BaseModel):
    """A recorded failure event from the classifier."""

    id:             int
    failure_class:  str
    trace_id:       Optional[str]
    detail:         str
    auto_recovered: bool
    resolved:       bool
    resolved_at:    Optional[str]
    ts:             float
    created_at:     str


class TraceHop(BaseModel):
    """A single recorded hop in a pick's pipeline journey."""

    id:         int
    trace_id:   str
    ticker:     str
    pathway:    str
    hop:        str
    service:    str
    status:     str
    metadata:   Optional[Dict[str, Any]]
    ts:         float
    created_at: str


class AnomalyReport(BaseModel):
    """Anomaly report posted by Guardian Angel v3."""

    source:       str   = Field(..., description="Reporting source, e.g. guardian-angel-v3")
    anomaly_type: str   = Field(..., description="Type of anomaly detected")
    service:      str   = Field(..., description="Affected service name")
    detail:       str   = Field(..., description="Human-readable description")
    ts:           float = Field(..., description="Unix timestamp of anomaly detection")
