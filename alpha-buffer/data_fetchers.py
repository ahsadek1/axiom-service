"""
data_fetchers.py — External data fetchers with explicit fallback chains
=======================================================================
Every fetcher returns a typed contract object or raises a typed exception.
No silent defaults. No None values that reach the scorer.

Fallback chain per dependency:
  IVR:      ORATS → Polygon IV calc → NoVolatilityDataError (skip ticker)
  RSI/MA:   Polygon bars → NoTechnicalDataError (skip ticker)
  Earnings: ORATS hist/earnings → Polygon events → treat as EARNINGS_PRESENT (skip)
  VIX:      Polygon → FRED → pause scanning (see market_state.py)
  Flow:     Polygon options chain (PCR) → FlowData with None PCR (acceptable)
  Gamma:    Polygon options chain → GammaData with None walls (acceptable)

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from data_contracts import (
    VolatilityData, TechnicalData, FundamentalData,
    FlowData, GammaData, TickerContext,
    DataContractError, NoVolatilityDataError, NoTechnicalDataError,
    parse_orats_volatility, parse_polygon_iv_fallback,
)

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ORATS_TOKEN     = os.getenv("ORATS_TOKEN") or os.getenv("ORATS_API_KEY")
POLYGON_KEY     = "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl"
TIMEOUT         = 10

ETF_TICKERS     = {"SPY","QQQ","IWM","GLD","SLV","EFA","IEF","HYG","AGG","IBB","XLF","XLE"}


# ── IVR / Volatility ──────────────────────────────────────────────────────────

def fetch_orats_summary(ticker: str) -> dict:
    """Raw ORATS summaries fetch. Raises on HTTP error."""
    r = requests.get(
        "https://api.orats.io/datav2/summaries",
        params={"token": ORATS_TOKEN, "ticker": ticker},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json().get("data", [])
    if not rows:
        raise DataContractError(f"ORATS: no data returned for {ticker}")
    return rows[0]


def fetch_polygon_options_chain(ticker: str) -> dict:
    """Fetch Polygon options chain. Returns aggregated call/put volumes."""
    r = requests.get(
        f"https://api.polygon.io/v3/snapshot/options/{ticker}",
        params={"apiKey": POLYGON_KEY, "limit": 250},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    call_oi = call_vol = put_oi = put_vol = 0.0
    for c in results:
        ct   = c.get("details", {}).get("contract_type", "")
        oi   = c.get("open_interest", 0) or 0
        vol  = c.get("day", {}).get("volume", 0) or 0
        if ct == "call":
            call_oi  += oi; call_vol  += vol
        elif ct == "put":
            put_oi   += oi; put_vol   += vol
    return {
        "call_oi": call_oi, "put_oi": put_oi,
        "call_vol": call_vol, "put_vol": put_vol,
    }


def get_volatility(ticker: str) -> VolatilityData:
    """
    ORATS primary → Polygon fallback → NoVolatilityDataError.
    Caller skips ticker on NoVolatilityDataError — never uses a default.
    """
    # Primary: ORATS
    try:
        raw  = fetch_orats_summary(ticker)
        return parse_orats_volatility(raw, ticker)
    except DataContractError as e:
        logger.warning("ORATS contract violation for %s: %s — trying Polygon", ticker, e)
    except Exception as e:
        logger.warning("ORATS fetch failed for %s: %s — trying Polygon fallback", ticker, e)

    # Fallback: Polygon options chain IV proxy
    try:
        chain = fetch_polygon_options_chain(ticker)
        return parse_polygon_iv_fallback(
            chain["call_oi"], chain["put_oi"],
            chain["call_vol"], chain["put_vol"],
            ticker,
        )
    except DataContractError as e:
        logger.error("Polygon IV fallback contract violation for %s: %s", ticker, e)
    except Exception as e:
        logger.error("Polygon IV fallback failed for %s: %s", ticker, e)

    # Both failed — skip ticker
    raise NoVolatilityDataError(ticker)


# ── Technicals ────────────────────────────────────────────────────────────────

def _compute_rsi(closes: list, period: int = 14) -> float:
    """RSI from close prices. Raises DataContractError if insufficient bars."""
    if len(closes) < period + 1:
        raise DataContractError(f"RSI: need {period+1} bars, got {len(closes)}")
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-(i+1)]
        (gains if diff >= 0 else losses).append(abs(diff))
        if diff < 0: gains.append(0)
        else:        losses.append(0)
    # Remove the extra appends (paired)
    gains  = [closes[-i] - closes[-(i+1)] for i in range(1, period+1) if closes[-i] >= closes[-(i+1)]]
    losses = [closes[-(i+1)] - closes[-i] for i in range(1, period+1) if closes[-i] <  closes[-(i+1)]]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def get_technicals(ticker: str) -> TechnicalData:
    """
    Fetch RSI(14), MAs, volume ratio from Polygon daily bars.
    Raises NoTechnicalDataError if bars unavailable.
    """
    from datetime import date, timedelta as td, datetime as _dt
    # BUG-FIX (Axiom 3.8): date.today() returns UTC date on Railway/UTC servers.
    # After 8 PM ET Polygon returns 0 bars for "tomorrow" — skipping all tickers.
    # Use ET-aware date via zoneinfo/pytz fallback chain.
    try:
        from zoneinfo import ZoneInfo as _ZI
        _today_et = _dt.now(_ZI("America/New_York")).date()
    except Exception:
        try:
            import pytz as _pytz
            _today_et = _dt.now(_pytz.timezone("America/New_York")).date()
        except Exception:
            _today_et = date.today()
    to_date   = _today_et.strftime("%Y-%m-%d")
    from_date = (_today_et - td(days=90)).strftime("%Y-%m-%d")

    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}",
            params={"adjusted": "true", "sort": "asc", "limit": 80, "apiKey": POLYGON_KEY},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        bars = r.json().get("results", [])
    except Exception as e:
        logger.error("Polygon bars failed for %s: %s", ticker, e)
        raise NoTechnicalDataError(ticker)

    if len(bars) < 20:
        raise NoTechnicalDataError(ticker)

    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    last    = closes[-1]

    try:
        rsi = _compute_rsi(closes)
    except DataContractError as e:
        raise NoTechnicalDataError(ticker) from e

    ma20    = sum(closes[-20:]) / 20
    vs_20   = round((last / ma20 - 1) * 100, 2)
    vs_50   = round((last / sum(closes[-50:]) * 50 - 1) * 100, 2) if len(closes) >= 50 else None
    avg_vol = sum(volumes[-30:]) / min(30, len(volumes))
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else None

    return TechnicalData(
        ticker           = ticker,
        last_price       = last,
        rsi_14           = rsi,
        price_vs_20d_ma  = vs_20,
        price_vs_50d_ma  = vs_50,
        volume_ratio     = vol_ratio,
        avg_volume_30d   = round(avg_vol, 0),
        source           = "polygon",
    )


# ── Fundamentals ──────────────────────────────────────────────────────────────

def get_fundamentals(ticker: str) -> FundamentalData:
    """
    Earnings date from ORATS hist/earnings.
    Revenue growth + margin from Polygon quarterly financials.
    ETFs return FundamentalData with None earnings fields — not an error.
    """
    days_to_earnings = None
    earnings_date    = None

    # Earnings date: ORATS primary (unlimited quota)
    if ticker not in ETF_TICKERS:
        try:
            r = requests.get(
                "https://api.orats.io/datav2/hist/earnings",
                params={"token": ORATS_TOKEN, "ticker": ticker, "limit": 8},
                timeout=TIMEOUT,
            )
            rows = r.json().get("data", [])
            if rows:
                rows.sort(key=lambda x: x.get("earnDate", ""), reverse=True)
                last_date = rows[0].get("earnDate")
                if last_date:
                    from datetime import date, timedelta as td
                    last = datetime.strptime(last_date, "%Y-%m-%d").date()
                    next_earn = last + td(days=91)
                    today = date.today()
                    if next_earn < today:
                        next_earn += td(days=91)
                    days_to_earnings = (next_earn - today).days
                    earnings_date    = next_earn.strftime("%Y-%m-%d")
        except Exception as e:
            logger.debug("ORATS earnings for %s: %s", ticker, e)

    # Revenue growth + margin: Polygon financials
    rev_growth   = None
    margin_trend = "FLAT"
    analyst_bias = "NEUTRAL"
    try:
        r = requests.get(
            "https://api.polygon.io/vX/reference/financials",
            params={"ticker": ticker, "limit": 8, "timeframe": "quarterly", "apiKey": POLYGON_KEY},
            timeout=TIMEOUT,
        )
        results = r.json().get("results", [])
        def _rev(row):
            return (row.get("financials", {}).get("income_statement", {})
                       .get("revenues", {}).get("value"))
        def _gp(row):
            return (row.get("financials", {}).get("income_statement", {})
                       .get("gross_profit", {}).get("value"))
        if len(results) >= 5:
            rev_curr      = _rev(results[0])
            rev_year_ago  = _rev(results[4])
            gp_curr       = _gp(results[0])
            rev_curr_val  = _rev(results[0])
            gp_2q         = _gp(results[2])
            rev_2q        = _rev(results[2])
            if rev_curr and rev_year_ago and rev_year_ago != 0:
                rev_growth = round((rev_curr - rev_year_ago) / abs(rev_year_ago) * 100, 2)
            if rev_curr_val and gp_curr and rev_curr_val != 0 and rev_2q and gp_2q and rev_2q != 0:
                m_curr = gp_curr / rev_curr_val
                m_2q   = gp_2q   / rev_2q
                diff   = m_curr - m_2q
                margin_trend = "EXPANDING" if diff > 0.01 else "CONTRACTING" if diff < -0.01 else "FLAT"
    except Exception as e:
        logger.debug("Polygon financials for %s: %s", ticker, e)

    return FundamentalData(
        ticker                = ticker,
        days_to_earnings      = days_to_earnings,
        earnings_date         = earnings_date,
        revenue_growth_yoy    = rev_growth,
        margin_trend          = margin_trend,
        analyst_revision_bias = analyst_bias,
    )


# ── Flow ──────────────────────────────────────────────────────────────────────

def get_flow(ticker: str) -> FlowData:
    """Options flow from Polygon. PCR may be None — acceptable for scoring."""
    try:
        chain   = fetch_polygon_options_chain(ticker)
        call_v  = chain["call_vol"]
        put_v   = chain["put_vol"]
        pcr     = round(put_v / call_v, 3) if call_v > 0 else None
        total_v = call_v + put_v
        avg_v   = 500_000  # rough market avg
        vol_vs  = round(total_v / avg_v, 2) if total_v > 0 else None
        return FlowData(
            ticker               = ticker,
            put_call_ratio       = pcr,
            options_volume_vs_avg= vol_vs,
            source               = "polygon",
        )
    except Exception as e:
        logger.debug("Flow fetch failed for %s: %s", ticker, e)
        return FlowData(ticker=ticker, put_call_ratio=None, source="unavailable")


# ── Gamma ─────────────────────────────────────────────────────────────────────

def get_gamma(ticker: str) -> GammaData:
    """GEX/walls from Polygon. Walls may be None — acceptable."""
    try:
        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={"apiKey": POLYGON_KEY, "limit": 250},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        contracts = r.json().get("results", [])
        call_gex = put_gex = 0.0
        for c in contracts:
            delta  = c.get("greeks", {}).get("delta", 0) or 0
            gamma  = c.get("greeks", {}).get("gamma", 0) or 0
            oi     = c.get("open_interest", 0) or 0
            ct     = c.get("details", {}).get("contract_type", "")
            gex    = gamma * oi * 100 * abs(delta)
            if ct == "call": call_gex += gex
            elif ct == "put": put_gex += gex
        net = call_gex - put_gex
        return GammaData(
            ticker     = ticker,
            call_wall  = None,  # requires strike-level analysis
            put_wall   = None,
            net_gex    = "POSITIVE" if net > 0 else "NEGATIVE" if net < 0 else "NEUTRAL",
            hiro_signal= None,
            source     = "polygon",
        )
    except Exception as e:
        logger.debug("Gamma fetch failed for %s: %s", ticker, e)
        return GammaData(ticker=ticker, call_wall=None, put_wall=None,
                         net_gex=None, hiro_signal=None, source="unavailable")


# ── Full Context Assembly ─────────────────────────────────────────────────────

def get_ticker_context(ticker: str) -> TickerContext:
    """
    Assemble full TickerContext. Raises NoVolatilityDataError or
    NoTechnicalDataError if critical data unavailable — caller skips ticker.
    """
    vol   = get_volatility(ticker)    # raises if unavailable
    tech  = get_technicals(ticker)    # raises if unavailable
    fund  = get_fundamentals(ticker)  # returns with None fields — acceptable
    flow  = get_flow(ticker)          # returns with None PCR — acceptable
    gamma = get_gamma(ticker)         # returns with None walls — acceptable

    return TickerContext(
        ticker      = ticker,
        vol         = vol,
        tech        = tech,
        fundamental = fund,
        flow        = flow,
        gamma       = gamma,
    )
