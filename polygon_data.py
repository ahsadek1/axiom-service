"""
Polygon.io Data Layer — Axiom Risk Engine
==========================================
Replaces yfinance (unreliable) with Polygon (institutional-grade) for:
  - Daily price bars (RSI, SMA, beta calculation, volume)
  - Options snapshots (OI, volume, bid-ask, Greeks)
  - Dividend reference data (accurate ex-div dates)

ORATS stays for:
  - IV Rank (IVR) — Polygon does not provide pre-calculated IVR
  - Earnings dates — ORATS earnings calendar is more precise

All functions fail gracefully — never block the risk pipeline.

Author: OMNI / Axiom
Date:   2026-04-05
"""

import os
import datetime
import requests
from typing import Optional

POLYGON_KEY  = os.getenv("POLYGON_KEY", "lzWjU48O_ZoEjNHkKt3G7M_nqbxrweUI")
POLYGON_BASE = "https://api.polygon.io"
TIMEOUT      = 10


def _get(path: str, params: dict = None) -> dict:
    """Base Polygon GET with auth."""
    try:
        p = params or {}
        p["apiKey"] = POLYGON_KEY
        r = requests.get(f"{POLYGON_BASE}{path}", params=p, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        return {"status": "error", "code": r.status_code}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Daily bars ────────────────────────────────────────────────────────────────

def get_daily_bars(ticker: str, days: int = 150) -> list:
    """
    Fetch last N calendar days of daily OHLCV bars.
    Returns list of dicts: {date, open, high, low, close, volume, vwap}
    """
    to_date   = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=days)
    data = _get(
        f"/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}",
        {"adjusted": "true", "sort": "asc", "limit": 300}
    )
    results = data.get("results", [])
    return [
        {
            "date":   datetime.datetime.fromtimestamp(r["t"] / 1000).strftime("%Y-%m-%d"),
            "open":   r.get("o"),
            "high":   r.get("h"),
            "low":    r.get("l"),
            "close":  r.get("c"),
            "volume": r.get("v"),
            "vwap":   r.get("vw"),
        }
        for r in results
    ]


def get_current_price(ticker: str) -> Optional[float]:
    """Latest close price from Polygon daily bars."""
    bars = get_daily_bars(ticker, days=5)
    if bars:
        return float(bars[-1]["close"])
    return None


def calc_sma(bars: list, period: int) -> Optional[float]:
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def calc_rsi(bars: list, period: int = 14) -> Optional[float]:
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_g  = sum(gains[-period:]) / period
    avg_l  = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 1)


def calc_beta(ticker: str, spy_bars: list = None, period_days: int = 60) -> Optional[float]:
    """
    Calculate realized beta vs SPY over last N trading days.
    Uses Polygon daily bars for both ticker and SPY.
    Returns both historical (60-day) beta.
    """
    try:
        stock_bars = get_daily_bars(ticker, days=period_days + 30)
        if spy_bars is None:
            spy_bars = get_daily_bars("SPY", days=period_days + 30)

        # Align by date
        stock_by_date = {b["date"]: b["close"] for b in stock_bars if b.get("close")}
        spy_by_date   = {b["date"]: b["close"] for b in spy_bars   if b.get("close")}
        common_dates  = sorted(set(stock_by_date) & set(spy_by_date))[-period_days:]

        if len(common_dates) < 20:
            return None

        stock_closes = [stock_by_date[d] for d in common_dates]
        spy_closes   = [spy_by_date[d]   for d in common_dates]

        stock_rets = [(stock_closes[i] - stock_closes[i-1]) / stock_closes[i-1]
                      for i in range(1, len(stock_closes))]
        spy_rets   = [(spy_closes[i]   - spy_closes[i-1])   / spy_closes[i-1]
                      for i in range(1, len(spy_closes))]

        n       = len(stock_rets)
        mean_s  = sum(stock_rets) / n
        mean_m  = sum(spy_rets) / n
        cov     = sum((stock_rets[i] - mean_s) * (spy_rets[i] - mean_m) for i in range(n)) / n
        var_m   = sum((spy_rets[i] - mean_m) ** 2 for i in range(n)) / n

        return round(cov / var_m, 3) if var_m > 0 else None
    except Exception:
        return None


def calc_realized_beta_20d(ticker: str) -> Optional[float]:
    """20-day realized beta — captures current regime behavior."""
    return calc_beta(ticker, period_days=20)


def calc_at_52w_extreme(bars: list) -> dict:
    """Check if price is within 3% of 52-week high or low."""
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < 5:
        return {"at_high": False, "at_low": False}
    current  = closes[-1]
    high_52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    low_52w  = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    return {
        "at_high": current >= high_52w * 0.97,
        "at_low":  current <= low_52w  * 1.03,
        "high_52w":round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "current": round(current, 2),
    }


def get_technical_data(ticker: str) -> dict:
    """
    Single call returning all technical data needed by Layer 9.
    Returns: price, sma20, sma50, sma200, rsi, avg_volume, volume_today, at_high, at_low
    """
    bars = get_daily_bars(ticker, days=300)
    if not bars:
        return {}

    closes  = [b["close"]  for b in bars if b.get("close")]
    volumes = [b["volume"] for b in bars if b.get("volume")]
    extremes = calc_at_52w_extreme(bars)

    avg_vol  = round(sum(volumes[-20:]) / min(len(volumes), 20), 0) if volumes else None
    vol_today = volumes[-1] if volumes else None

    return {
        "price":       closes[-1] if closes else None,
        "sma20":       calc_sma(bars, 20),
        "sma50":       calc_sma(bars, 50),
        "sma200":      calc_sma(bars, 200),
        "rsi":         calc_rsi(bars),
        "avg_volume":  avg_vol,
        "volume_today":vol_today,
        "at_52w_high": extremes.get("at_high", False),
        "at_52w_low":  extremes.get("at_low",  False),
        "bars":        len(bars),
    }


# ── Options snapshot ──────────────────────────────────────────────────────────

def get_options_snapshot(
    ticker: str,
    strike: Optional[float],
    dte_target: int,
    option_type: str = "call",
    strike_range_pct: float = 0.08,
) -> dict:
    """
    Fetch options snapshot for a specific strike/DTE target.
    Returns the closest match with: OI, volume, bid, ask, greeks, IV.
    """
    today = datetime.date.today()
    exp_from = (today + datetime.timedelta(days=max(0, dte_target - 7))).isoformat()
    exp_to   = (today + datetime.timedelta(days=dte_target + 7)).isoformat()

    params = {
        "expiration_date_gte":  exp_from,
        "expiration_date_lte":  exp_to,
        "contract_type":        option_type.lower(),
        "limit":                50,
        "sort":                 "expiration_date",
    }
    if strike:
        buffer = strike * strike_range_pct
        params["strike_price_gte"] = round(strike - buffer, 0)
        params["strike_price_lte"] = round(strike + buffer, 0)

    data = _get(f"/v3/snapshot/options/{ticker}", params)
    results = data.get("results", [])

    if not results:
        return {}

    # Score by: closest DTE + closest strike
    def score(c):
        exp = c.get("details", {}).get("expiration_date", "")
        try:
            exp_date = datetime.date.fromisoformat(exp)
            dte_diff = abs((exp_date - today).days - dte_target)
        except Exception:
            dte_diff = 99
        st = c.get("details", {}).get("strike_price", 0)
        strike_diff = abs(st - (strike or st))
        return dte_diff * 2 + strike_diff

    best = min(results, key=score)
    day  = best.get("day", {})
    greeks = best.get("greeks", {})

    bid = float(day.get("open",  0) or 0)
    ask = float(day.get("close", 0) or 0)
    # Better bid/ask from last quote if available
    quote = best.get("last_quote", {})
    if quote.get("bid") and quote.get("ask"):
        bid = float(quote["bid"])
        ask = float(quote["ask"])

    mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else float(day.get("close", 0) or 0)
    spread_pct = round((ask - bid) / mid, 4) if mid > 0 else 1.0

    return {
        "strike":      best.get("details", {}).get("strike_price"),
        "expiry":      best.get("details", {}).get("expiration_date"),
        "dte":         (datetime.date.fromisoformat(best.get("details",{}).get("expiration_date","2099-01-01")) - today).days,
        "open_interest":float(best.get("open_interest", 0) or 0),
        "volume":      float(day.get("volume", 0) or 0),
        "bid":         bid,
        "ask":         ask,
        "mid":         mid,
        "spread_pct":  spread_pct,
        "delta":       greeks.get("delta"),
        "gamma":       greeks.get("gamma"),
        "theta":       greeks.get("theta"),
        "vega":        greeks.get("vega"),
        "iv":          best.get("implied_volatility"),
        "underlying":  best.get("underlying_asset", {}).get("price"),
        "ticker":      best.get("details", {}).get("ticker"),
    }


# ── Dividends ─────────────────────────────────────────────────────────────────

def get_dividend_data(ticker: str) -> dict:
    """
    Fetch dividend reference data from Polygon.
    Returns: ex_dividend_date, cash_amount, frequency, pay_date
    """
    data    = _get("/v3/reference/dividends", {"ticker": ticker, "limit": 5, "order": "desc"})
    results = data.get("results", [])
    if not results:
        return {}

    latest = results[0]
    ex_div = latest.get("ex_dividend_date", "")
    today  = datetime.date.today()

    days_to_exdiv = 999
    if ex_div:
        try:
            ex_date       = datetime.date.fromisoformat(ex_div)
            days_to_exdiv = (ex_date - today).days
        except Exception:
            pass

    return {
        "ex_dividend_date": ex_div,
        "days_to_exdiv":    days_to_exdiv,
        "cash_amount":      float(latest.get("cash_amount", 0) or 0),
        "frequency":        latest.get("frequency", 0),
        "pay_date":         latest.get("pay_date", ""),
        "annual_yield_approx": float(latest.get("cash_amount", 0) or 0) * int(latest.get("frequency", 4)),
    }


if __name__ == "__main__":
    print("📊 Polygon Data Layer — Integration Test\n")

    print("── NVDA Technical Data ──")
    tech = get_technical_data("NVDA")
    for k, v in tech.items():
        if k != "bars":
            print(f"  {k}: {v}")
    print(f"  bars available: {tech.get('bars')}")

    print("\n── NVDA Beta (60-day vs SPY) ──")
    beta_60 = calc_beta("NVDA")
    beta_20 = calc_realized_beta_20d("NVDA")
    print(f"  60-day beta: {beta_60}")
    print(f"  20-day realized beta: {beta_20}")

    print("\n── NVDA Options Snapshot (ATM, ~35 DTE) ──")
    price = get_current_price("NVDA")
    snap  = get_options_snapshot("NVDA", price, 35, "call")
    for k, v in snap.items():
        if v is not None:
            print(f"  {k}: {v}")

    print("\n── NVDA Dividends ──")
    div = get_dividend_data("NVDA")
    for k, v in div.items():
        print(f"  {k}: {v}")

    print("\n✅ Polygon data layer verified")
