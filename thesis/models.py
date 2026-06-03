"""
THESIS — Shared Pydantic models and enumerations.

All cross-module data structures live here to prevent circular imports
and guarantee a single source of truth for serialization contracts.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class GateResult(str, Enum):
    """Binary outcome for hard-gate frameworks."""

    PASS = "PASS"
    FAIL = "FAIL"


class TradingPosture(str, Enum):
    """Top-level stance emitted by the weekly thesis."""

    AGGRESSIVE = "AGGRESSIVE"
    NEUTRAL = "NEUTRAL"
    DEFENSIVE = "DEFENSIVE"
    SELECTIVE = "SELECTIVE"


class EventType(str, Enum):
    """Real-time events that can trigger thesis review."""

    UNEXPECTED_FED_SPEAK = "unexpected_fed_speak"
    MAJOR_GEOPOLITICAL_SHOCK = "major_geopolitical_shock"
    BELLWETHER_EARNINGS_SHOCK = "bellwether_earnings_shock"
    VIX_SPIKE = "vix_spike"
    KEY_TECHNICAL_BREAK = "key_technical_break"
    CREDIT_SPREAD_BLOW_OUT = "credit_spread_blow_out"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class OracleData(BaseModel):
    """Snapshot of market indicators fetched from ORACLE."""

    spy_price: Optional[float] = None
    spy_change_pct: Optional[float] = None
    vix_level: Optional[float] = None
    vix_change: Optional[float] = None
    ten_year_yield: Optional[float] = None
    two_year_yield: Optional[float] = None
    yield_curve_spread: Optional[float] = None  # 10Y - 2Y, bp
    hy_spread: Optional[float] = None           # HY credit spread, bp
    dxy: Optional[float] = None                 # US Dollar Index
    fed_funds_rate: Optional[float] = None
    sp500_pe: Optional[float] = None
    put_call_ratio: Optional[float] = None
    aaii_bull_pct: Optional[float] = None       # AAII sentiment
    timestamp: str = ""

    @property
    def yield_curve_inverted(self) -> bool:
        """True when 2Y > 10Y (inversion signal)."""
        if self.ten_year_yield is None or self.two_year_yield is None:
            return False
        return self.two_year_yield > self.ten_year_yield


class MarketSnapshot(BaseModel):
    """Inbound payload for Layer 3 real-time monitoring."""

    spy_price: Optional[float] = None
    vix_level: Optional[float] = None
    vix_change_1h: Optional[float] = None       # VIX point change in last hour
    spy_change_30m: Optional[float] = None      # SPY % change in last 30 min
    hy_spread_change: Optional[float] = None    # HY spread change, bp
    news_headlines: List[str] = Field(default_factory=list)
    timestamp: str = ""


# ---------------------------------------------------------------------------
# Framework analysis
# ---------------------------------------------------------------------------


class FrameworkAnalysis(BaseModel):
    """Structured result returned by each of the five trading frameworks."""

    name: str
    gate_type: str                          # "HARD" | "SOFT"
    gate_result: Optional[GateResult] = None   # Hard gates only
    confidence_adjustment: int = 0          # Soft gates: ±int, hard gates: 0
    size_veto: bool = False                 # True when framework vetoes size
    is_veto: bool = False                   # True when framework is a soft veto
    analysis: str = ""
    key_signal: str = ""
    signals: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------


class ConflictResolution(BaseModel):
    """Output of the multi-framework gate / soft-gate reconciliation."""

    macro_gate: GateResult
    risk_reward_gate: GateResult
    soft_veto_count: int
    confidence_adjustment: int              # Net of all soft-gate adjustments
    final_confidence: int                   # Base 50 + adjustments, clamped 0-100
    size_multiplier: float                  # 0.25 / 0.50 / 0.75 / 1.0
    is_go: bool
    no_go_reason: Optional[str] = None
    verdict: str = ""                       # Human-readable summary
    run_prosecutor_defender: bool = False
    prosecutor_framework: Optional[str] = None
    defender_framework: Optional[str] = None


# ---------------------------------------------------------------------------
# CHRONICLE records
# ---------------------------------------------------------------------------


class ThesisContext(BaseModel):
    """Maps 1-to-1 with the thesis_context table in CHRONICLE."""

    id: Optional[int] = None
    layer: str                              # "weekly" | "daily"
    valid_from: str
    valid_until: str
    trading_posture: str                    # TradingPosture value
    sizing_multiplier: float
    favored_sectors: List[str] = Field(default_factory=list)
    avoid_sectors: List[str] = Field(default_factory=list)
    favored_strategies: List[str] = Field(default_factory=list)
    confidence_adjustment: int
    macro_gate: str                         # GateResult value
    risk_reward_gate: str                   # GateResult value
    thesis_sentence: str
    primary_authority: str = ""
    full_thesis: str = ""
    created_at: Optional[str] = None


class ThesisEvent(BaseModel):
    """Maps 1-to-1 with the thesis_events table in CHRONICLE."""

    id: Optional[int] = None
    event_type: str                         # EventType value
    detected_at: str
    description: str
    affected_sectors: List[str] = Field(default_factory=list)
    thesis_paused: bool = False
    thesis_updated_at: Optional[str] = None
    resolved_at: Optional[str] = None


# ---------------------------------------------------------------------------
# API responses
# ---------------------------------------------------------------------------


class CurrentContextResponse(BaseModel):
    """Payload returned by GET /thesis/current-context for OMNI."""

    macro_gate: str = "PASS"
    risk_reward_gate: str = "PASS"
    trading_posture: str = "NEUTRAL"
    sizing_multiplier: float = 1.0
    favored_sectors: List[str] = Field(default_factory=list)
    avoid_sectors: List[str] = Field(default_factory=list)
    favored_strategies: List[str] = Field(default_factory=list)
    confidence_adjustment: int = 0
    thesis_sentence: str = "No thesis available — neutral posture"
    primary_authority: str = ""
    valid_until: str = ""
    thesis_age_hours: float = 0.0


class HealthResponse(BaseModel):
    """Service health payload."""

    service: str = "THESIS"
    status: str = "ok"
    version: str = "1.0.0"
    port: int = 8060
    chronicle_reachable: bool = False
    oracle_reachable: bool = False
    has_weekly_thesis: bool = False
    has_daily_brief: bool = False


class GenerateResponse(BaseModel):
    """Response after triggering thesis generation."""

    status: str
    message: str
    thesis_id: Optional[int] = None
    thesis_sentence: Optional[str] = None
    macro_gate: Optional[str] = None
    risk_reward_gate: Optional[str] = None
    trading_posture: Optional[str] = None
    sizing_multiplier: Optional[float] = None


class PerformanceMetrics(BaseModel):
    """Weekly accuracy metrics for GET /thesis/performance."""

    weeks_analyzed: int = 0
    macro_gate_accuracy: float = 0.0
    posture_accuracy: float = 0.0
    avg_confidence_adjustment: float = 0.0
    note: str = "Performance tracking requires historical comparison data"
