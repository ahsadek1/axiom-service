"""
ORACLE Intelligence Hub — Pydantic Models
All request/response types for the ORACLE service.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel


# ── Cache / Freshness ─────────────────────────────────────────────────────────

class DataSection(BaseModel):
    """A single engine's data section with freshness metadata."""
    data: Optional[Dict[str, Any]] = None
    data_freshness: str = "LIVE"  # LIVE | STALE | UNAVAILABLE
    engine: str = ""


# ── Coherence ─────────────────────────────────────────────────────────────────

class CoherenceFlag(BaseModel):
    type: str
    description: str
    severity: str  # WARNING | CAUTION | INFO


class CoherenceResult(BaseModel):
    coherence_score: int
    coherence_level: str  # HIGH | MEDIUM | LOW | CONFLICTED
    flags: List[CoherenceFlag] = []
    aligned_signals: List[str] = []
    conflicting_signals: List[str] = []


# ── Context Packets ───────────────────────────────────────────────────────────

class PriceData(BaseModel):
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    volume: Optional[float] = None
    avg_volume_30d: Optional[float] = None
    volume_ratio: Optional[float] = None


class VolData(BaseModel):
    iv_rank: Optional[float] = None
    iv_percentile: Optional[float] = None
    hv30: Optional[float] = None
    hv60: Optional[float] = None
    iv_hv_spread: Optional[float] = None
    expected_move_weekly: Optional[float] = None
    put_call_skew: Optional[str] = None


class FlowData(BaseModel):
    sweeps_today: List[Dict[str, Any]] = []
    dark_pool_prints: List[Dict[str, Any]] = []
    net_flow_bias: str = "NEUTRAL"
    unusual_activity: bool = False
    put_call_ratio: Optional[float] = None
    options_volume_vs_avg: Optional[float] = None


class GammaData(BaseModel):
    gex_level: Optional[float] = None
    gamma_flip: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    hiro_signal: Optional[float] = None
    net_gex: Optional[str] = None  # POSITIVE | NEGATIVE


class MacroData(BaseModel):
    regime: str = "UNKNOWN"
    composite_score: int = 0
    vix: Optional[float] = None
    vix_trend_5d: Optional[str] = None
    fed_funds_rate: Optional[float] = None
    yield_2y: Optional[float] = None
    yield_10y: Optional[float] = None
    yield_spread: Optional[float] = None
    hy_spread: Optional[float] = None
    upcoming_events: List[Dict[str, Any]] = []
    next_high_impact_days: Optional[int] = None
    strategy_bias: str = "NEUTRAL"


class InsiderTransaction(BaseModel):
    date: str
    insider_name: str
    role: str
    transaction_type: str
    shares: Optional[int] = None
    price: Optional[float] = None
    total_value: Optional[float] = None


class FundamentalData(BaseModel):
    earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    earnings_clear_25d: bool = True
    earnings_clear_45d: bool = True
    last_8_surprises: List[Dict[str, Any]] = []
    avg_actual_move_pct: Optional[float] = None
    implied_move_pct: Optional[float] = None
    insider_transactions_90d: List[InsiderTransaction] = []
    insider_net_bias: str = "NEUTRAL"
    insider_cluster_flag: bool = False
    institutional_ownership_change: Optional[float] = None


class HistoricalData(BaseModel):
    instances: int = 0
    win_rate: Optional[float] = None
    avg_win_pct: Optional[float] = None
    avg_loss_pct: Optional[float] = None
    expected_value: Optional[float] = None
    most_common_failure: Optional[str] = None
    regime_match: str = "UNKNOWN"
    data_confidence: str = "LOW"  # LOW | MEDIUM | HIGH


class NewsData(BaseModel):
    article_count: int = 0
    articles: List[Dict[str, Any]] = []
    net_sentiment: str = "NEUTRAL"  # BULLISH | BEARISH | NEUTRAL
    bullish_count: int = 0
    bearish_count: int = 0
    most_recent_headline: Optional[str] = None


class ContextPacket(BaseModel):
    """Full ticker context packet returned by GET /oracle/context/{ticker}."""
    ticker: str
    card_type: str = "full"  # preliminary | full
    timestamp: str
    on_demand: bool = False

    price: Optional[PriceData] = None
    price_freshness: str = "UNAVAILABLE"

    vol: Optional[VolData] = None
    vol_freshness: str = "UNAVAILABLE"

    flow: Optional[FlowData] = None
    flow_freshness: str = "UNAVAILABLE"

    gamma: Optional[GammaData] = None
    gamma_freshness: str = "UNAVAILABLE"

    macro: Optional[MacroData] = None
    macro_freshness: str = "UNAVAILABLE"

    fundamental: Optional[FundamentalData] = None
    fundamental_freshness: str = "UNAVAILABLE"

    historical: Optional[HistoricalData] = None
    historical_freshness: str = "UNAVAILABLE"

    news: Optional[NewsData] = None
    news_freshness: str = "UNAVAILABLE"

    coherence: Optional[CoherenceResult] = None


class PreliminaryCard(BaseModel):
    """Lightweight card for Tier 1 pool (100 stocks)."""
    ticker: str
    card_type: str = "preliminary"
    timestamp: str
    price: Optional[float] = None
    iv_rank: Optional[float] = None
    iv_percentile: Optional[float] = None
    hv30: Optional[float] = None
    iv_hv_spread: Optional[float] = None
    earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    earnings_clear: bool = True
    macro_regime: str = "UNKNOWN"
    macro_vix: Optional[float] = None
    macro_event_risk_days: Optional[int] = None
    tier2_signal: str = "BORDERLINE"  # ADVANCE | BORDERLINE | HOLD


# ── API Requests / Responses ──────────────────────────────────────────────────

class PrefetchRequest(BaseModel):
    tickers: List[str]
    tier: str = "full"  # preliminary | full


class PrefetchResponse(BaseModel):
    accepted: bool = True
    ticker_count: int
    tier: str
    estimated_ready_ms: int


class HealthCheck(BaseModel):
    status: str  # healthy | degraded | critical
    engines: Dict[str, Any] = {}
    cache: Dict[str, Any] = {}
    intelligence_layer: Dict[str, Any] = {}


class CrossTickerPattern(BaseModel):
    type: str
    tickers_involved: List[str]
    description: str
    implication: str


class CycleIntelligence(BaseModel):
    cycle_id: str
    cycle_time: str
    tickers_in_pool: List[str]
    patterns_detected: List[CrossTickerPattern] = []
    echo_chamber_risk_tickers: List[str] = []
