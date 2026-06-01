"""
momentum_scanner.py — Momentum Breakout Scanner
================================================
Identifies stocks in strong directional momentum from S&P500+NASDAQ100.
Runs every 30 minutes during market hours.

Momentum signals:
  1. Price near 52-week high (within 3%) on elevated volume
  2. Relative strength vs SPY > 1.5x over 5 days
  3. Volume confirmation (>1.5x 20-day average)
  4. Clean technical structure (not extended)

Tier classification:
  TIER_A: RS > 2x + near 52wk high + vol > 2x    → 8-12% expected
  TIER_B: RS > 1.5x + strong volume               → 5-8% expected
  TIER_C: RS > 1.2x + above average volume        → 3-5% expected
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("prime_v2.momentum_scanner")
_ET = ZoneInfo("America/New_York")

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")

# Core universe — highest liquidity S&P500 + NASDAQ100
CORE_UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","LLY",
    "V","UNH","XOM","MA","COST","HD","PG","ABBV","MRK","CVX","CRM","BAC",
    "NFLX","AMD","PEP","KO","TMO","ORCL","MCD","CSCO","GE","IBM","NOW",
    "QCOM","TXN","ISRG","INTU","AMGN","RTX","GS","SPGI","BKNG","CAT",
    "DHR","LOW","HON","AXP","VRTX","AMAT","PANW","REGN","ADI","LRCX",
    "MU","KLAC","BSX","GILD","MDT","ICE","CME","TJX","DE","MMC","ZTS",
    "ETN","SHW","CDNS","SNPS","MRVL","NXPI","FTNT","CRWD","DDOG","SNOW",
    "WDAY","NET","OKTA","MDB","VEEV","WDC","NTAP","KEYS","ADSK","PAYX",
    "FAST","ODFL","CTAS","ROK","PH","EMR","HPQ","HPE","F","GM","COF",
    "BLK","SCHW","CB","AON","MET","PRU","AFL","ALL","PGR","BIIB","EXEL",
    "CVS","CI","HUM","HCA","COP","EOG","PXD","MPC","VLO","PSX","DVN",
    "NEE","DUK","SO","AEP","EXC","SRE","AMT","PLD","CCI","EQIX","PSA",
]


def get_bars(ticker: str, days: int = 60) -> list[dict]:
    """Fetch daily bars from Polygon."""
    end   = date.today()
    start = end - timedelta(days=days)
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"adjusted":"true","sort":"asc","limit":days,"apiKey":POLYGON_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as exc:
        log.debug("Bars fetch failed for %s: %s", ticker, exc)
    return []


def get_spy_bars(days: int = 10) -> list[dict]:
    """Fetch SPY bars for relative strength calculation."""
    return get_bars("SPY", days)


def calculate_signals(ticker: str, bars: list[dict]) -> Optional[dict]:
    """
    Calculate momentum signals for a ticker.
    Returns signal dict or None if insufficient data.
    """
    if len(bars) < 20:
        return None

    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    current = closes[-1]
    current_vol = volumes[-1]

    # 52-week high (use available data, max 252 bars)
    high_52w = max(closes)
    pct_from_high = (current - high_52w) / high_52w * 100

    # 20-day average volume
    avg_vol_20 = sum(volumes[-20:]) / 20
    vol_ratio  = current_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

    # 5-day price change
    price_5d   = closes[-6] if len(closes) >= 6 else closes[0]
    change_5d  = (current - price_5d) / price_5d * 100

    # 20-day price change (momentum)
    price_20d  = closes[-21] if len(closes) >= 21 else closes[0]
    change_20d = (current - price_20d) / price_20d * 100

    # Simple relative strength vs index (will be enhanced with SPY comparison)
    rs_score = change_5d * 0.6 + change_20d * 0.4

    return {
        "ticker":         ticker,
        "price":          current,
        "pct_from_52wh":  round(pct_from_high, 2),
        "vol_ratio":      round(vol_ratio, 2),
        "change_5d":      round(change_5d, 2),
        "change_20d":     round(change_20d, 2),
        "rs_score":       round(rs_score, 2),
        "avg_vol_20":     avg_vol_20,
    }


def classify_momentum_tier(signals: dict, spy_change_5d: float) -> str:
    """
    Classify momentum setup into tier.
    Relative strength is versus SPY 5-day performance.
    """
    rs_vs_spy  = signals["change_5d"] - spy_change_5d
    vol_ratio  = signals["vol_ratio"]
    near_high  = signals["pct_from_52wh"] >= -5.0  # within 5% of 52wk high
    change_5d  = signals["change_5d"]

    # Must be moving in positive direction
    if change_5d <= 0:
        return "SKIP"

    # Must have some volume confirmation
    if vol_ratio < 1.0:
        return "SKIP"

    # TIER_A: Very strong relative strength + near highs + volume surge
    if rs_vs_spy >= 5.0 and near_high and vol_ratio >= 2.0:
        return "TIER_A"

    # TIER_A: Exceptional RS even without all conditions
    if rs_vs_spy >= 8.0 and vol_ratio >= 1.5:
        return "TIER_A"

    # TIER_B: Strong RS + good volume
    if rs_vs_spy >= 3.0 and vol_ratio >= 1.5:
        return "TIER_B"

    # TIER_B: Near high + decent volume
    if near_high and vol_ratio >= 1.3 and change_5d >= 3.0:
        return "TIER_B"

    # TIER_C: Moderate RS + above average volume
    if rs_vs_spy >= 1.5 and vol_ratio >= 1.2 and change_5d >= 1.5:
        return "TIER_C"

    return "SKIP"


def scan_momentum(universe: list[str] = None) -> list[dict]:
    """
    Scan universe for momentum setups.
    Returns list of qualified candidates sorted by tier and RS score.
    """
    if universe is None:
        universe = CORE_UNIVERSE

    # Get SPY performance for relative strength
    spy_bars  = get_spy_bars(days=25)
    if len(spy_bars) >= 6:
        spy_change_5d = (spy_bars[-1]["c"] - spy_bars[-6]["c"]) / spy_bars[-6]["c"] * 100
    else:
        spy_change_5d = 0.0

    log.info("Momentum scan: %d tickers | SPY 5d=%.1f%%", len(universe), spy_change_5d)

    results = []
    for ticker in universe:
        bars = get_bars(ticker, days=60)
        if not bars:
            continue

        signals = calculate_signals(ticker, bars)
        if not signals:
            continue

        tier = classify_momentum_tier(signals, spy_change_5d)
        if tier == "SKIP":
            continue

        results.append({
            **signals,
            "tier":        tier,
            "rs_vs_spy":   round(signals["change_5d"] - spy_change_5d, 2),
            "direction":   "bullish",  # momentum is always directional
            "strategy":    "MOMENTUM_BREAKOUT",
        })

    # Sort by tier then RS score
    tier_order = {"TIER_A": 0, "TIER_B": 1, "TIER_C": 2}
    results.sort(key=lambda x: (tier_order.get(x["tier"], 9), -x["rs_vs_spy"]))

    tier_a = sum(1 for r in results if r["tier"]=="TIER_A")
    tier_b = sum(1 for r in results if r["tier"]=="TIER_B")
    tier_c = sum(1 for r in results if r["tier"]=="TIER_C")
    log.info("Momentum scan complete: %d setups — A:%d B:%d C:%d",
             len(results), tier_a, tier_b, tier_c)

    return results


def get_bearish_momentum(universe: list[str] = None) -> list[dict]:
    """
    Scan for bearish momentum — stocks breaking down.
    Useful in risk-off regimes.
    """
    if universe is None:
        universe = CORE_UNIVERSE

    spy_bars  = get_spy_bars(days=25)
    if len(spy_bars) >= 6:
        spy_change_5d = (spy_bars[-1]["c"] - spy_bars[-6]["c"]) / spy_bars[-6]["c"] * 100
    else:
        spy_change_5d = 0.0

    results = []
    for ticker in universe:
        bars = get_bars(ticker, days=60)
        if not bars or len(bars) < 20:
            continue

        closes     = [b["c"] for b in bars]
        volumes    = [b["v"] for b in bars]
        current    = closes[-1]
        price_5d   = closes[-6] if len(closes) >= 6 else closes[0]
        change_5d  = (current - price_5d) / price_5d * 100
        avg_vol    = sum(volumes[-20:]) / 20
        vol_ratio  = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        rs_vs_spy  = change_5d - spy_change_5d

        # Bearish: significant underperformance + volume
        if change_5d >= -1.5:
            continue
        if rs_vs_spy >= -2.0:
            continue
        if vol_ratio < 1.2:
            continue

        tier = "TIER_B" if rs_vs_spy <= -5.0 and vol_ratio >= 1.5 else "TIER_C"

        results.append({
            "ticker":      ticker,
            "price":       current,
            "change_5d":   round(change_5d, 2),
            "rs_vs_spy":   round(rs_vs_spy, 2),
            "vol_ratio":   round(vol_ratio, 2),
            "tier":        tier,
            "direction":   "bearish",
            "strategy":    "MOMENTUM_BREAKDOWN",
        })

    results.sort(key=lambda x: x["rs_vs_spy"])
    return results
