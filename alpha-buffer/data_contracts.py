"""
data_contracts.py — Typed data boundaries for Nexus V2
=======================================================
Every external data source enters through a typed contract.
Null data raises DataContractError — never reaches the scorer as a default.

Philosophy: Make the D5-locked-at-7 / IVR=0.76-fake-rank failure class
            impossible by construction, not by monitoring.

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import logging

logger = logging.getLogger(__name__)


# ── Custom Exceptions ─────────────────────────────────────────────────────────

class DataContractError(Exception):
    """Raised when external data violates its contract — missing, wrong type, out of range."""
    pass

class NoVolatilityDataError(Exception):
    """Raised when both ORATS and Polygon fallback fail for a ticker. Caller skips ticker."""
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"No volatility data available for {ticker} — both sources failed")

class NoTechnicalDataError(Exception):
    """Raised when Polygon bars fail for a ticker. Caller skips ticker."""
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"No technical data available for {ticker}")

class PositionCapError(Exception):
    """Raised when Alpaca live position count >= 3. Hard block."""
    pass

class CapitalExceededError(Exception):
    """Raised when capital allocation would exceed pool total."""
    pass

class MarketStateStaleError(Exception):
    """Raised when MarketState VIX data is older than MAX_MARKET_STATE_AGE."""
    pass


# ── Typed Data Objects ────────────────────────────────────────────────────────

@dataclass
class VolatilityData:
    """
    IV data for a ticker. iv_rank is NEVER None — callers must handle
    NoVolatilityDataError and skip the ticker if data is unavailable.
    """
    ticker:         str
    iv_rank:        float           # ORATS rip (0-100). Non-optional by design.
    iv_percentile:  float           # same as rip
    hv_30:          Optional[float] # realized vol 30d (decimal → pct)
    hv_60:          Optional[float]
    iv_hv_spread:   Optional[float]
    contango:       Optional[float]
    implied_move:   Optional[float]
    source:         str             # "orats" | "polygon_fallback"
    fetched_at:     datetime        = field(default_factory=lambda: datetime.now(timezone.utc))
    is_fallback:    bool            = False

    def __post_init__(self):
        if not (0.0 <= self.iv_rank <= 100.0):
            raise DataContractError(
                f"VolatilityData: iv_rank={self.iv_rank} out of range [0,100] for {self.ticker}"
            )


@dataclass
class TechnicalData:
    """
    Price/technical data. rsi_14 is non-optional — callers handle NoTechnicalDataError.
    """
    ticker:             str
    last_price:         float
    rsi_14:             float           # Non-optional. Computed from 60 daily bars.
    price_vs_20d_ma:    Optional[float] # % deviation
    price_vs_50d_ma:    Optional[float]
    volume_ratio:       Optional[float] # today / 30d avg
    avg_volume_30d:     Optional[float]
    source:             str             # "polygon"
    fetched_at:         datetime        = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not (0.0 <= self.rsi_14 <= 100.0):
            raise DataContractError(
                f"TechnicalData: rsi_14={self.rsi_14} out of range [0,100] for {self.ticker}"
            )


@dataclass
class FundamentalData:
    """Earnings + financial quality data."""
    ticker:                 str
    days_to_earnings:       Optional[int]   # None = unknown (ETF or no coverage)
    earnings_date:          Optional[str]
    revenue_growth_yoy:     Optional[float] # % — None acceptable (ETFs, sparse coverage)
    margin_trend:           str             = "FLAT"   # EXPANDING | FLAT | CONTRACTING
    analyst_revision_bias:  str             = "NEUTRAL"
    insider_net_bias:       str             = "NEUTRAL"
    source:                 str             = "polygon+orats"
    fetched_at:             datetime        = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FlowData:
    """Options flow data."""
    ticker:                 str
    put_call_ratio:         Optional[float]
    options_volume_vs_avg:  Optional[float]
    net_flow_bias:          str             = "NEUTRAL"
    unusual_activity:       bool            = False
    source:                 str             = "polygon"
    fetched_at:             datetime        = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GammaData:
    """Gamma/GEX data."""
    ticker:         str
    call_wall:      Optional[float]
    put_wall:       Optional[float]
    net_gex:        Optional[str]   # "POSITIVE" | "NEGATIVE" | "NEUTRAL"
    hiro_signal:    Optional[float]
    source:         str             = "polygon"
    fetched_at:     datetime        = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TickerContext:
    """
    Complete context for one ticker. All fields populated before scoring.
    Scorer receives this — never receives partial or null-field dicts.
    """
    ticker:         str
    vol:            VolatilityData
    tech:           TechnicalData
    fundamental:    FundamentalData
    flow:           FlowData
    gamma:          GammaData
    assembled_at:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ScanResult:
    """
    Result of one scan cycle. skips exposes data failures — no silent dry cycles.
    C2 fix: if skips > 50% of pool, caller alerts Ahmed.
    """
    window_id:      str
    pool_size:      int
    verdicts:       list        = field(default_factory=list)
    skips:          dict        = field(default_factory=dict)  # {"no_volatility_data": 3}
    degraded:       bool        = False
    degraded_sources: list      = field(default_factory=list)  # ["orats"]
    uniform_score_flag: bool    = False

    @property
    def skip_count(self) -> int:
        return sum(self.skips.values())

    @property
    def skip_rate(self) -> float:
        if self.pool_size == 0:
            return 0.0
        return self.skip_count / self.pool_size

    def is_data_failure(self) -> bool:
        """
        True if skip rate exceeds 50% due to a real data or pipeline failure.

        Excludes expected non-failure skip reasons:
          - axiom_standby: Axiom market-closed gate (submissions_open=False after 3:30 PM ET)
          - stale_ivr / stale_ivr_axiom: after-hours IVR data known to be stale
          - already_processed: normal dedup, not a failure

        FIX-AXIOM-STANDBY (2026-05-05): Post-market scans all hitting AXIOM_STANDBY
        is expected behaviour, not a data failure. Without this exclusion, every
        post-market window fires a false HIGH SKIP RATE alert.
        """
        _EXPECTED_SKIP_REASONS = {"axiom_standby", "stale_ivr", "stale_ivr_axiom", "already_processed"}
        expected_skip_count = sum(
            count for reason, count in self.skips.items()
            if reason in _EXPECTED_SKIP_REASONS
        )
        real_skip_count = self.skip_count - expected_skip_count
        # Effective pool = total pool minus tickers that were correctly expected to skip
        effective_pool = self.pool_size - expected_skip_count
        if effective_pool < 5:
            return False  # too few real candidates to judge
        real_skip_rate = real_skip_count / effective_pool
        return real_skip_rate > 0.50

    def log_skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_orats_volatility(raw: dict, ticker: str) -> VolatilityData:
    """
    Parse ORATS summaries response into VolatilityData.
    Raises DataContractError if iv_rank (rip) is missing or invalid.
    """
    rip = raw.get("rip")
    if rip is None:
        raise DataContractError(f"ORATS: iv_rank (rip) missing for {ticker}")
    try:
        rip = float(rip)
    except (TypeError, ValueError):
        raise DataContractError(f"ORATS: iv_rank not numeric for {ticker}: {rip!r}")

    iv30d  = raw.get("iv30d")
    rVol30 = raw.get("rVol30")

    hv30 = round(float(rVol30) * 100, 2) if rVol30 is not None else None
    iv_hv = round(float(iv30d) - float(rVol30), 4) if iv30d and rVol30 else None

    return VolatilityData(
        ticker        = ticker,
        iv_rank       = round(rip, 2),
        iv_percentile = round(rip, 2),
        hv_30         = hv30,
        hv_60         = None,
        iv_hv_spread  = iv_hv,
        contango      = raw.get("contango"),
        implied_move  = raw.get("impliedMove"),
        source        = "orats",
    )


def parse_polygon_iv_fallback(call_oi: float, put_oi: float,
                               call_vol: float, put_vol: float,
                               ticker: str) -> VolatilityData:
    """
    Fallback IV rank from Polygon options chain.
    Uses volume PCR as a proxy — not a real IVR percentile.
    Marked is_fallback=True so scorers can apply appropriate weight.
    """
    # Volume PCR → rough IVR proxy (inverted: low PCR = bullish = higher IVR proxy)
    total_vol = call_vol + put_vol
    if total_vol == 0:
        raise DataContractError(f"Polygon IV fallback: zero options volume for {ticker}")

    vol_pcr = put_vol / call_vol if call_vol > 0 else 1.0
    # Scale PCR (0.3-2.0) to IVR-like range (20-80)
    # PCR=0.5 → IVR~60 (low put pressure = relatively low IV rank)
    # PCR=1.5 → IVR~40 (high put pressure = moderate IV rank)
    iv_rank_proxy = max(5.0, min(95.0, 50.0 - (vol_pcr - 1.0) * 20.0))

    logger.warning("Using Polygon IV fallback for %s — iv_rank_proxy=%.1f (not real IVR)",
                   ticker, iv_rank_proxy)

    return VolatilityData(
        ticker        = ticker,
        iv_rank       = round(iv_rank_proxy, 2),
        iv_percentile = round(iv_rank_proxy, 2),
        hv_30         = None,
        hv_60         = None,
        iv_hv_spread  = None,
        contango      = None,
        implied_move  = None,
        source        = "polygon_fallback",
        is_fallback   = True,
    )
