"""
ORACLE — Engine 5: Macro Engine
Sources: Trading Economics (calendar) + FRED (regime data)
Query pattern: Per-cycle (system-wide) — one call per Axiom refresh, not per-ticker.
Cache TTL: 6 hours standard, 1 hour on event days.
"""

import logging
import time
from typing import Any, Dict, Optional

import cache
import config
from clients import fred_client, trading_economics_client
from models import MacroData

logger = logging.getLogger(__name__)

ENGINE = "macro"
CYCLE_KEY = "__SYSTEM__"  # Macro is system-wide, not per-ticker


def fetch() -> tuple[Optional[MacroData], str]:
    """
    Fetch system-wide macro data. Cached per cycle — all tickers share one packet.

    Returns:
        Tuple of (MacroData or None, freshness string).
    """
    start = time.monotonic()

    # Cache check (system-wide key)
    cached = cache.get(CYCLE_KEY, ENGINE, "full")
    if cached is not None:
        cache.log_api_call(ENGINE, "fred", None,
                           int((time.monotonic() - start) * 1000),
                           True, cache_hit=True)
        return MacroData(**cached), config.FRESHNESS_LIVE

    # Live fetch
    fred_data = fred_client.get_macro_data()
    te_calendar = trading_economics_client.get_calendar(days_ahead=14)
    te_next = trading_economics_client.get_next_high_impact_event()

    vix = fred_data.get("vix")
    hy_spread = fred_data.get("hy_spread_bps")
    yield_spread = fred_data.get("yield_spread_bps")

    regime, score = _classify_regime(vix, hy_spread, yield_spread)
    strategy_bias = _strategy_bias(regime, vix)
    next_event_days = te_next.get("days_until") if te_next else None

    macro = MacroData(
        regime=regime,
        composite_score=score,
        vix=vix,
        fed_funds_rate=fred_data.get("fed_funds_rate"),
        yield_2y=fred_data.get("yield_2y"),
        yield_10y=fred_data.get("yield_10y"),
        yield_spread=fred_data.get("yield_spread_bps"),
        hy_spread=hy_spread,
        upcoming_events=te_calendar[:10],
        next_high_impact_days=next_event_days,
        strategy_bias=strategy_bias,
    )

    ttl = config.MACRO_TTL_EVENT_DAY if (next_event_days is not None and next_event_days <= 1) \
          else config.MACRO_TTL_STANDARD

    cache.set(CYCLE_KEY, ENGINE, macro.model_dump(), ttl, "full")
    cache.log_api_call(ENGINE, "fred", None,
                       int((time.monotonic() - start) * 1000), True)
    return macro, config.FRESHNESS_LIVE


def _classify_regime(vix: Optional[float], hy_spread: Optional[float],
                     yield_spread: Optional[float]) -> tuple[str, int]:
    """
    Multi-factor regime classification.
    VIX (0-25) + HY Spread (0-25) + Yield Curve (0-25) = composite 0-75
    Scaled to 0-100 when put/call ratio data available (stub for now).

    Args:
        vix:          Current VIX level.
        hy_spread:    HY credit spread in basis points.
        yield_spread: 2Y/10Y yield spread in basis points.

    Returns:
        Tuple of (regime string, composite score 0-100).
    """
    score = 0

    # Factor 1: VIX
    if vix is not None:
        if vix < 13:
            score += 0
        elif vix < 17:
            score += 5
        elif vix < 21:
            score += 10
        elif vix < 26:
            score += 15
        elif vix < 31:
            score += 20
        else:
            score += 25

    # Factor 2: HY Credit Spread (bps)
    if hy_spread is not None:
        if hy_spread < 150:
            score += 0
        elif hy_spread < 250:
            score += 5
        elif hy_spread < 350:
            score += 10
        elif hy_spread < 450:
            score += 15
        elif hy_spread < 600:
            score += 20
        else:
            score += 25

    # Factor 3: Yield Curve (bps — positive=normal, negative=inverted)
    if yield_spread is not None:
        if yield_spread > 100:
            score += 0
        elif yield_spread > 50:
            score += 5
        elif yield_spread > 0:
            score += 10
        elif yield_spread > -50:
            score += 15
        elif yield_spread > -100:
            score += 20
        else:
            score += 25

    # Scale to 0-100 (3 factors max 75 → scale by 4/3)
    scaled = min(100, int(score * 100 / 75)) if score > 0 else 0

    if scaled <= 15:
        return "LOW_VOL", scaled
    elif scaled <= 30:
        return "NORMAL", scaled
    elif scaled <= 45:
        return "ELEVATED", scaled
    elif scaled <= 60:
        return "STRESS", scaled
    elif scaled <= 75:
        return "HIGH_STRESS", scaled
    else:
        return "CRISIS", scaled


def _strategy_bias(regime: str, vix: Optional[float]) -> str:
    """Determine strategy bias based on regime."""
    if regime in ("CRISIS", "HIGH_STRESS"):
        return "CASH_OR_HEDGE_ONLY"
    elif regime == "STRESS":
        return "CREDIT_SPREADS_ONLY_REDUCED_SIZE"
    elif regime == "ELEVATED":
        return "CREDIT_SPREADS_PREFERRED"
    elif regime == "LOW_VOL":
        return "DEBIT_SPREADS_OR_REDUCE"
    else:
        return "NEUTRAL"
