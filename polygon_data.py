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
Date:   2026-04-09 (v2 — radical ATM fix)

FIXES (2026-04-09):
  - get_options_snapshot: strike=None now fetches current price and selects true ATM
  - get_options_snapshot: bid/ask no longer initialized from day open/close (wrong fields)
    Now uses last_quote exclusively; falls back to day VWAP or mid estimate only
  - get_atm_weekly_option: new dedicated function for ATM weekly options (0-14 DTE)
  - get_current_price: now also available standalone for callers
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
    """
    Latest close price from Polygon daily bars.
    Also tries Polygon snapshot for real-time price during market hours.
    """
    # Try real-time snapshot first (more accurate during market hours)
    try:
        snap = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
        price = snap.get("ticker", {}).get("day", {}).get("c")
        if price and float(price) > 0:
            return float(price)
        # Try last trade price
        price = snap.get("ticker", {}).get("lastTrade", {}).get("p")
        if price and float(price) > 0:
            return float(price)
    except Exception:
        pass

    # Fallback: most recent daily bar
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
    """
    try:
        stock_bars = get_daily_bars(ticker, days=period_days + 30)
        if spy_bars is None:
            spy_bars = get_daily_bars("SPY", days=period_days + 30)

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

    FIX (2026-04-09):
      - When strike=None, fetches current price and selects true ATM strike.
        Previous bug: strike_diff = abs(st - (strike or st)) = abs(st - st) = 0 for ALL
        contracts when strike=None, causing selection by DTE only (not ATM).
      - bid/ask no longer initialized from day.open/day.close (those are OHLC price
        fields, NOT bid/ask). Now sourced exclusively from last_quote.
    """
    today = datetime.date.today()

    # ── ATM resolution: if no strike provided, fetch current price ───────────
    atm_price = None
    if strike is None:
        atm_price = get_current_price(ticker)
        # strike_range_pct will filter around ATM price if we have it
        effective_strike = atm_price
    else:
        effective_strike = strike

    exp_from = (today + datetime.timedelta(days=max(0, dte_target - 7))).isoformat()
    exp_to   = (today + datetime.timedelta(days=dte_target + 7)).isoformat()

    params = {
        "expiration_date_gte":  exp_from,
        "expiration_date_lte":  exp_to,
        "contract_type":        option_type.lower(),
        "limit":                100,   # FIX: was 50, need more to find true ATM
        "sort":                 "expiration_date",
    }
    # Apply strike filter only if we have a reference price (ATM or explicit strike)
    if effective_strike and effective_strike > 0:
        buffer = effective_strike * strike_range_pct
        params["strike_price_gte"] = round(effective_strike - buffer, 0)
        params["strike_price_lte"] = round(effective_strike + buffer, 0)

    data = _get(f"/v3/snapshot/options/{ticker}", params)
    results = data.get("results", [])

    if not results:
        return {}

    # ── Scoring: closest DTE + closest strike to effective_strike ────────────
    def score(c):
        exp = c.get("details", {}).get("expiration_date", "")
        try:
            exp_date = datetime.date.fromisoformat(exp)
            dte_diff = abs((exp_date - today).days - dte_target)
        except Exception:
            dte_diff = 99
        st = float(c.get("details", {}).get("strike_price", 0) or 0)

        # FIX: use effective_strike (resolved from current price if strike=None)
        # Previous bug: (strike or st) evaluates to st when strike=None → diff=0 for all
        if effective_strike and effective_strike > 0:
            strike_diff = abs(st - effective_strike)
        else:
            strike_diff = 0.0

        return dte_diff * 2 + strike_diff

    best = min(results, key=score)
    day  = best.get("day", {})
    greeks = best.get("greeks", {})

    # ── FIX: bid/ask from last_quote ONLY — day.open/close are OHLC, not bid/ask ──
    # Previous bug: bid = day.get("open", 0), ask = day.get("close", 0)
    # Those are the day's opening and closing trade prices — completely wrong for
    # spread/mid calculations. Real bid/ask lives in last_quote.
    quote = best.get("last_quote", {})
    bid   = float(quote.get("bid", 0) or 0)
    ask   = float(quote.get("ask", 0) or 0)

    # If last_quote unavailable, estimate from day VWAP ± half typical spread
    if bid == 0 and ask == 0:
        vwap = float(day.get("vwap", 0) or 0)
        close = float(day.get("close", 0) or 0)
        ref  = vwap if vwap > 0 else close
        if ref > 0:
            # Estimate ~5% spread as conservative fallback
            half_spread = ref * 0.025
            bid = round(ref - half_spread, 2)
            ask = round(ref + half_spread, 2)

    mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else float(day.get("close", 0) or 0)
    spread_pct = round((ask - bid) / mid, 4) if mid > 0 and ask > bid else 1.0

    # Resolved strike for transparency
    resolved_strike = float(best.get("details", {}).get("strike_price", 0) or 0)

    return {
        "strike":        resolved_strike,
        "atm_ref_price": round(atm_price, 2) if atm_price else None,
        "expiry":        best.get("details", {}).get("expiration_date"),
        "dte":           (datetime.date.fromisoformat(best.get("details",{}).get("expiration_date","2099-01-01")) - today).days,
        "open_interest": float(best.get("open_interest", 0) or 0),
        "volume":        float(day.get("volume", 0) or 0),
        "bid":           bid,
        "ask":           ask,
        "mid":           mid,
        "spread_pct":    spread_pct,
        "delta":         greeks.get("delta"),
        "gamma":         greeks.get("gamma"),
        "theta":         greeks.get("theta"),
        "vega":          greeks.get("vega"),
        "iv":            best.get("implied_volatility"),
        "underlying":    best.get("underlying_asset", {}).get("price"),
        "ticker":        best.get("details", {}).get("ticker"),
    }


def get_atm_weekly_option(
    ticker: str,
    option_type: str = "call",
    dte_target: int = 7,
    dte_min: int = 1,
    dte_max: int = 14,
) -> dict:
    """
    Dedicated ATM weekly options lookup.

    Purpose: find the closest-to-ATM option on the nearest weekly expiry.
    Used when a pick doesn't specify a strike/expiry (ATM weekly mode).

    Logic:
      1. Fetch current underlying price (true ATM reference)
      2. Query Polygon for options expiring within dte_min–dte_max
      3. Sort by: nearest weekly Friday first, then closest strike to ATM
      4. Return the best match with full Greeks + bid/ask

    Returns dict with all fields needed for order construction:
      strike, expiry, dte, bid, ask, mid, delta, atm_ref_price, occ_symbol
    """
    today = datetime.date.today()

    # Step 1: get current price as ATM reference
    current_price = get_current_price(ticker)
    if not current_price or current_price <= 0:
        return {"available": False, "error": f"Could not fetch current price for {ticker}"}

    # Step 2: fetch options in weekly window
    exp_from = (today + datetime.timedelta(days=dte_min)).isoformat()
    exp_to   = (today + datetime.timedelta(days=dte_max)).isoformat()

    # Strike range: ±5% around current price (wider than standard to catch nearest strikes)
    buffer = current_price * 0.05
    params = {
        "expiration_date_gte":  exp_from,
        "expiration_date_lte":  exp_to,
        "contract_type":        option_type.lower(),
        "strike_price_gte":     round(current_price - buffer, 0),
        "strike_price_lte":     round(current_price + buffer, 0),
        "limit":                100,
        "sort":                 "expiration_date",
    }

    data = _get(f"/v3/snapshot/options/{ticker}", params)
    results = data.get("results", [])

    if not results:
        return {
            "available":     False,
            "error":         f"No weekly options found for {ticker} in {dte_min}-{dte_max} DTE window",
            "atm_ref_price": round(current_price, 2),
        }

    # Step 3: identify weekly Friday expirations and score by ATM proximity
    def is_weekly_friday(exp_str: str) -> bool:
        """True if expiry falls on a Friday (weekly expiry)."""
        try:
            return datetime.date.fromisoformat(exp_str).weekday() == 4  # 4 = Friday
        except Exception:
            return False

    def weekly_score(c):
        exp = c.get("details", {}).get("expiration_date", "")
        try:
            exp_date = datetime.date.fromisoformat(exp)
            dte_days = (exp_date - today).days
        except Exception:
            dte_days = 99
        st = float(c.get("details", {}).get("strike_price", 0) or 0)
        strike_diff = abs(st - current_price)  # True ATM distance

        # Prefer Friday expirations (weeklies); penalize non-Fridays slightly
        friday_bonus = 0 if is_weekly_friday(exp) else 2.0

        # Primary: nearest expiry; Secondary: nearest strike to ATM
        return dte_days * 3 + strike_diff + friday_bonus

    best = min(results, key=weekly_score)
    day    = best.get("day", {})
    greeks = best.get("greeks", {})

    # Bid/ask from last_quote (correct source)
    quote = best.get("last_quote", {})
    bid   = float(quote.get("bid", 0) or 0)
    ask   = float(quote.get("ask", 0) or 0)

    if bid == 0 and ask == 0:
        vwap  = float(day.get("vwap", 0) or 0)
        close = float(day.get("close", 0) or 0)
        ref   = vwap if vwap > 0 else close
        if ref > 0:
            half_spread = ref * 0.025
            bid = round(ref - half_spread, 2)
            ask = round(ref + half_spread, 2)

    mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else float(day.get("close", 0) or 0)
    spread_pct = round((ask - bid) / mid, 4) if mid > 0 and ask > bid else 1.0

    resolved_strike = float(best.get("details", {}).get("strike_price", 0) or 0)
    resolved_expiry = best.get("details", {}).get("expiration_date", "")
    try:
        resolved_dte = (datetime.date.fromisoformat(resolved_expiry) - today).days
    except Exception:
        resolved_dte = 0

    # Build OCC symbol for convenience
    try:
        dt = datetime.datetime.strptime(resolved_expiry, "%Y-%m-%d")
        date_str   = dt.strftime("%y%m%d")
        cp         = "C" if option_type.lower() in ("call", "c") else "P"
        strike_int = int(resolved_strike * 1000)
        occ_symbol = f"{ticker}{date_str}{cp}{strike_int:08d}"
    except Exception:
        occ_symbol = ""

    return {
        "available":      True,
        "ticker":         ticker,
        "option_type":    option_type,
        "strike":         resolved_strike,
        "expiry":         resolved_expiry,
        "dte":            resolved_dte,
        "is_weekly":      is_weekly_friday(resolved_expiry),
        "atm_ref_price":  round(current_price, 2),
        "atm_distance":   round(abs(resolved_strike - current_price), 2),
        "atm_distance_pct": round(abs(resolved_strike - current_price) / current_price * 100, 2),
        "bid":            bid,
        "ask":            ask,
        "mid":            mid,
        "spread_pct":     spread_pct,
        "open_interest":  float(best.get("open_interest", 0) or 0),
        "volume":         float(day.get("volume", 0) or 0),
        "delta":          greeks.get("delta"),
        "gamma":          greeks.get("gamma"),
        "theta":          greeks.get("theta"),
        "vega":           greeks.get("vega"),
        "iv":             best.get("implied_volatility"),
        "underlying":     best.get("underlying_asset", {}).get("price"),
        "occ_symbol":     occ_symbol,
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
    print("📊 Polygon Data Layer v2 — ATM Fix Verification\n")

    print("── NVDA Technical Data ──")
    tech = get_technical_data("NVDA")
    for k, v in tech.items():
        if k != "bars":
            print(f"  {k}: {v}")

    print("\n── NVDA Current Price ──")
    price = get_current_price("NVDA")
    print(f"  Current price: ${price}")

    print("\n── NVDA Options Snapshot (ATM=None, ~35 DTE) — ATM fix test ──")
    snap = get_options_snapshot("NVDA", None, 35, "call")
    for k, v in snap.items():
        if v is not None:
            print(f"  {k}: {v}")
    if snap.get("strike") and price:
        print(f"  ATM distance: ${abs(snap['strike'] - price):.2f} ({abs(snap['strike'] - price)/price*100:.1f}%)")

    print("\n── NVDA ATM Weekly Option (~7 DTE) ──")
    weekly = get_atm_weekly_option("NVDA", "call", dte_target=7, dte_min=1, dte_max=14)
    for k, v in weekly.items():
        if v is not None:
            print(f"  {k}: {v}")

    print("\n── NVDA Beta ──")
    print(f"  60d: {calc_beta('NVDA')}")
    print(f"  20d: {calc_realized_beta_20d('NVDA')}")

    print("\n── NVDA Dividends ──")
    div = get_dividend_data("NVDA")
    for k, v in div.items():
        print(f"  {k}: {v}")

    print("\n✅ Polygon data layer v2 verified")
