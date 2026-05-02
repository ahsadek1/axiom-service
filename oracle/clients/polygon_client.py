"""
ORACLE — Polygon.io Client
Primary source for real-time price snapshots and historical OHLCV.
Fallback: yfinance (see price_engine.py).
"""

import logging
import time
from typing import Any, Dict, Optional

from datetime import datetime, timedelta

import requests

import config
import rate_limiter

logger = logging.getLogger(__name__)

SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
AGGS_URL    = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
TIMEOUT = (5, 15)  # (connect, read) seconds


def get_snapshot(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetch real-time snapshot for a single ticker from Polygon.

    Args:
        ticker: Stock ticker symbol (e.g. "NVDA").

    Returns:
        Dict with price, volume, day range data, or None on failure.
    """
    if not rate_limiter.acquire("polygon"):
        logger.warning("Polygon rate limit timeout for %s", ticker)
        return None

    url = SNAPSHOT_URL.format(ticker=ticker.upper())
    params = {"apiKey": config.POLYGON_API_KEY}
    start = time.monotonic()

    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 200:
            body = resp.json()
            ticker_data = body.get("ticker", {})
            day = ticker_data.get("day", {})
            prev_day = ticker_data.get("prevDay", {})
            last_quote = ticker_data.get("lastQuote", {})
            last_trade = ticker_data.get("lastTrade", {})

            return {
                "last": last_trade.get("p") or ticker_data.get("lastTrade", {}).get("p"),
                "bid": last_quote.get("P"),
                "ask": last_quote.get("p"),
                "day_high": day.get("h"),
                "day_low": day.get("l"),
                "day_open": day.get("o"),
                "day_close": day.get("c"),
                "volume": day.get("v"),
                "vwap": day.get("vw"),
                "prev_close": prev_day.get("c"),
                "latency_ms": latency_ms,
            }
        else:
            logger.error("Polygon snapshot error %s for %s: %s",
                         resp.status_code, ticker, resp.text[:200])
            return None

    except requests.Timeout:
        logger.error("Polygon snapshot timeout for %s", ticker)
        return None
    except requests.ConnectionError as e:
        logger.error("Polygon connection error for %s: %s", ticker, e)
        return None
    except (ValueError, KeyError) as e:
        logger.error("Polygon response parse error for %s: %s", ticker, e)
        return None


def get_financials(ticker: str) -> Optional[dict]:
    """
    Fetch last 4 quarters of financials from Polygon vX/reference/financials.
    Returns revenue_growth_yoy (% float) and margin_trend string, or None.
    """
    params = {"ticker": ticker.upper(), "limit": 8, "timeframe": "quarterly", "apiKey": config.POLYGON_API_KEY}
    try:
        resp = requests.get(
            "https://api.polygon.io/vX/reference/financials",
            params=params, timeout=TIMEOUT
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if len(results) < 2:
            return None

        def _rev(r):
            return (r.get("financials", {}).get("income_statement", {})
                     .get("revenues", {}).get("value"))
        def _gp(r):
            return (r.get("financials", {}).get("income_statement", {})
                     .get("gross_profit", {}).get("value"))

        rev_curr = _rev(results[0])
        rev_prev = _rev(results[1]) if len(results) > 1 else None
        gp_curr  = _gp(results[0])
        gp_prev  = _gp(results[1]) if len(results) > 1 else None
        gp_old   = _gp(results[3]) if len(results) > 3 else None

        # Revenue growth YoY (compare most recent Q vs same Q a year ago)
        rev_yoy = None
        rev_year_ago = _rev(results[3]) if len(results) > 3 else None
        if rev_curr and rev_year_ago and rev_year_ago != 0:
            rev_yoy = round((rev_curr - rev_year_ago) / abs(rev_year_ago) * 100, 2)

        # Margin trend: compare gross margin current vs 2 quarters ago
        margin_trend = "FLAT"
        if rev_curr and gp_curr and rev_curr != 0:
            margin_curr = gp_curr / rev_curr
            rev_2q = _rev(results[2]) if len(results) > 2 else None
            gp_2q  = _gp(results[2]) if len(results) > 2 else None
            if rev_2q and gp_2q and rev_2q != 0:
                margin_2q = gp_2q / rev_2q
                diff = margin_curr - margin_2q
                if diff > 0.01:    margin_trend = "EXPANDING"
                elif diff < -0.01: margin_trend = "CONTRACTING"

        return {"revenue_growth_yoy": rev_yoy, "margin_trend": margin_trend}
    except Exception as e:
        logger.error("Polygon financials error for %s: %s", ticker, e)
        return None


def get_daily_bars(ticker: str, days: int = 60) -> Optional[list]:
    """
    Fetch last `days` daily OHLCV bars for a ticker.
    Returns list of dicts with o, h, l, c, v keys, or None on failure.
    """
    if not rate_limiter.acquire("polygon"):
        logger.warning("Polygon rate limit timeout for %s (bars)", ticker)
        return None

    to_date   = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days + 20)).strftime("%Y-%m-%d")  # buffer for weekends
    url = AGGS_URL.format(ticker=ticker.upper(), from_date=from_date, to_date=to_date)
    params = {"adjusted": "true", "sort": "asc", "limit": days + 20, "apiKey": config.POLYGON_API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            return [{"o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]} for r in results]
        else:
            logger.error("Polygon bars error %s for %s: %s",
                         resp.status_code, ticker, resp.text[:200])
            return None
    except Exception as e:
        logger.error("Polygon bars error for %s: %s", ticker, e)
        return None


def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
    """Compute RSI from close prices."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-(i+1)]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def get_technicals(ticker: str) -> Optional[dict]:
    """
    Compute RSI(14), 20d MA, 50d MA, volume ratio from daily bars.
    Returns dict with rsi_14, price_vs_20d_ma, price_vs_50d_ma, volume_ratio,
    avg_volume_30d, or None on failure.
    """
    bars = get_daily_bars(ticker, days=60)
    if not bars or len(bars) < 20:
        return None

    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    last    = closes[-1]

    # RSI(14)
    rsi = _compute_rsi(closes)

    # 20d MA
    ma20 = sum(closes[-20:]) / 20
    vs_20 = round((last / ma20 - 1) * 100, 2) if ma20 else None

    # 50d MA (if enough bars)
    vs_50 = None
    if len(closes) >= 50:
        ma50  = sum(closes[-50:]) / 50
        vs_50 = round((last / ma50 - 1) * 100, 2) if ma50 else None

    # Volume ratio: today vs 30d avg
    vol_today  = volumes[-1] if volumes else None
    avg_vol_30 = sum(volumes[-30:]) / min(30, len(volumes)) if volumes else None
    vol_ratio  = round(vol_today / avg_vol_30, 2) if (vol_today and avg_vol_30) else None

    return {
        "rsi_14":           rsi,
        "price_vs_20d_ma":  vs_20,
        "price_vs_50d_ma":  vs_50,
        "volume_ratio":     vol_ratio,
        "avg_volume_30d":   round(avg_vol_30, 0) if avg_vol_30 else None,
    }
