"""
backtest_populator.py — 5-Year Historical Backtest Population
=============================================================
Pulls 5 years of price + VIX data from Polygon for the S&P500 + Nasdaq100
universe and computes historical win rates per (ticker, strategy, regime, direction).

What gets computed and stored in backtest.db:
  - historical_win_rates: per-ticker win rates for bull_put_spread and swing_long
  - regime_history: VIX + regime classification for every trading day
  - backtest_meta: progress tracking (tickers done, last run, etc.)

Designed to run ONCE as a background job (several hours).
Safe to re-run — uses INSERT OR REPLACE (idempotent).

Usage:
    cd /Users/ahmedsadek/nexus/ails
    source .venv/bin/activate
    python backtest_populator.py [--limit N] [--ticker TICKER]

    --limit N      Stop after N tickers (for testing)
    --ticker T     Run only for a single ticker
"""

import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from config import REGIME_THRESHOLDS  # Cipher Pass 3 P3-12: import from canonical source
from universe_full import FULL_UNIVERSE

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ails.backtest")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLYGON_API_KEY: str = os.environ["POLYGON_API_KEY"]
FRED_API_KEY: str = os.environ["FRED_API_KEY"]
BACKTEST_DB_PATH: str = os.environ.get(
    "BACKTEST_DB_PATH", "/Users/ahmedsadek/nexus/data/backtest.db"
)

POLYGON_BASE = "https://api.polygon.io"
RATE_LIMIT_DELAY = 0.25  # 4 req/sec — stay well within free tier (5/min) or paid (unlimited)
LOOKBACK_YEARS = 5
BULL_PUT_SPREAD_DTE = 30   # Simulate entry at ~30 DTE
WIN_THRESHOLD_PCT = 0.25   # Credit spread wins if underlying moves < 25% against short strike
SWING_HOLD_DAYS = 14       # Simulate 14-day swing trade hold

# ---------------------------------------------------------------------------
# Ticker Universe — S&P500 + Nasdaq100 overlap (Tier 1)
# ---------------------------------------------------------------------------

TIER_1_UNIVERSE = FULL_UNIVERSE  # Full S&P 500 + Nasdaq 100 universe (534 tickers)

def classify_vix(vix: float) -> str:
    """
    Classify VIX into regime bucket using canonical REGIME_THRESHOLDS from config.

    Cipher Pass 3 P3-12: previously defined a local copy of REGIME_THRESHOLDS that
    shadowed the config import. Any change to ails/config.py thresholds was silently
    ignored by the populator on re-run. Removed local copy — use config directly.
    """
    for regime, threshold in REGIME_THRESHOLDS.items():
        if vix <= threshold:
            return regime
    return "CRISIS"


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: Dict) -> Optional[dict]:
    """GET from Polygon with retry on 429."""
    params["apiKey"] = POLYGON_API_KEY
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                log.warning("Rate limited — sleeping 60s")
                time.sleep(60)
            elif resp.status_code == 404:
                return None
            else:
                log.warning("Polygon %d for %s", resp.status_code, url)
                return None
        except requests.RequestException as exc:
            log.warning("Request error attempt %d: %s", attempt + 1, exc)
            time.sleep(2 ** attempt)
    return None


def fetch_vix_history(start_date: str, end_date: str) -> Dict[str, float]:
    """
    Fetch daily VIX index closes from FRED (series VIXCLS).
    Returns {date_str: vix_close}.

    FRED is the authoritative free source for CBOE VIX closing values.
    Polygon indices (I:VIX) require an upgraded index plan — NOT included in our tier.
    Using Polygon for VIX caused corrupted values (max 850+) in the first run.
    FRED VIXCLS is free, reliable, correct, and covers 1990→present.

    IMPORTANT: Do NOT use VXX/VIXY as proxies — futures-based ETF prices
    are NOT comparable to VIX index levels. (OMNI Pass 3 Finding 1, 2026-04-12)
    """
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "VIXCLS",
        "observation_start": start_date,
        "observation_end": end_date,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "limit": 10000,
        "sort_order": "asc",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            log.error("FRED VIXCLS fetch failed: HTTP %d", resp.status_code)
            return {}
        data = resp.json()
    except requests.RequestException as exc:
        log.error("FRED VIXCLS request error: %s", exc)
        return {}

    observations = data.get("observations", [])
    if not observations:
        log.error("FRED VIXCLS returned no observations")
        return {}

    result: Dict[str, float] = {}
    for obs in observations:
        date_str = obs.get("date", "")
        value_str = obs.get("value", ".")
        if value_str == "." or not date_str:
            continue  # FRED uses "." for missing values
        try:
            result[date_str] = float(value_str)
        except ValueError:
            continue

    log.info("FRED VIXCLS: %d trading days loaded (%s → %s)", len(result), start_date, end_date)
    return result


def fetch_price_history(
    ticker: str, start_date: str, end_date: str
) -> List[Dict]:
    """
    Fetch daily OHLCV bars for a ticker.
    Returns list of {date, open, high, low, close, volume}.
    """
    data = _get(
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}",
        {"adjusted": "true", "sort": "asc", "limit": 5000},
    )
    if not data or not data.get("results"):
        return []

    bars = []
    for bar in data["results"]:
        dt = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc)
        bars.append({
            "date": dt.strftime("%Y-%m-%d"),
            "open": bar["o"],
            "high": bar["h"],
            "low": bar["l"],
            "close": bar["c"],
            "volume": bar.get("v", 0),
        })
    return bars


# ---------------------------------------------------------------------------
# Win rate computation
# ---------------------------------------------------------------------------

def compute_bull_put_spread_wr(
    bars: List[Dict],
    vix_by_date: Dict[str, float],
) -> Dict[Tuple[str, str], Tuple[float, int, float]]:
    """
    Simulate bull put spread entries across 5 years of price data.
    Entry: any day where regime is NORMAL or ELEVATED (typical spread market)
    Exit: 30 days later
    Win condition: price is still above entry price - 5% (short put not tested)

    Returns: {(regime, direction): (win_rate, sample_count, avg_return_pct)}
    """
    results: Dict[Tuple[str, str], Dict] = {}

    for i, bar in enumerate(bars):
        date = bar["date"]
        vix = vix_by_date.get(date, 18.0)
        regime = classify_vix(vix)

        # Only enter in NORMAL, ELEVATED (typical spread entry conditions)
        if regime not in ("LOW_VOL", "NORMAL", "ELEVATED"):
            continue

        # Look 30 days forward
        exit_idx = min(i + BULL_PUT_SPREAD_DTE, len(bars) - 1)
        exit_bar = bars[exit_idx]

        entry_price = bar["close"]
        exit_price = exit_bar["close"]
        pct_change = (exit_price - entry_price) / entry_price

        # Bull put spread wins if price stays flat or goes up
        # Lose if price drops more than WIN_THRESHOLD_PCT
        direction = "bullish"
        won = pct_change > -WIN_THRESHOLD_PCT

        key = (regime, direction)
        if key not in results:
            results[key] = {"wins": 0, "total": 0, "returns": []}
        results[key]["wins"] += int(won)
        results[key]["total"] += 1
        results[key]["returns"].append(pct_change)

    # Compute final rates
    output = {}
    for (regime, direction), data in results.items():
        if data["total"] < 5:
            continue
        wr = data["wins"] / data["total"]
        avg_ret = sum(data["returns"]) / len(data["returns"])
        output[(regime, direction)] = (round(wr, 4), data["total"], round(avg_ret * 100, 2))

    return output


def compute_swing_long_wr(
    bars: List[Dict],
    vix_by_date: Dict[str, float],
) -> Dict[Tuple[str, str], Tuple[float, int, float]]:
    """
    Simulate swing long entries across 5 years.
    Entry: any day where price is above 20-day moving average
    Exit: 14 days later
    Win condition: price is higher than entry at exit

    Returns: {(regime, direction): (win_rate, sample_count, avg_return_pct)}
    """
    results: Dict[Tuple[str, str], Dict] = {}

    if len(bars) < 25:
        return {}

    closes = [b["close"] for b in bars]

    for i in range(20, len(bars) - SWING_HOLD_DAYS):
        bar = bars[i]
        date = bar["date"]
        vix = vix_by_date.get(date, 18.0)
        regime = classify_vix(vix)

        # 20-day moving average filter
        ma20 = sum(closes[i - 20:i]) / 20
        entry_price = bar["close"]

        # Only enter long setups when price is above MA (uptrend)
        if entry_price < ma20 * 0.99:
            continue

        exit_bar = bars[i + SWING_HOLD_DAYS]
        exit_price = exit_bar["close"]
        pct_change = (exit_price - entry_price) / entry_price

        direction = "bullish"
        won = pct_change > 0

        key = (regime, direction)
        if key not in results:
            results[key] = {"wins": 0, "total": 0, "returns": []}
        results[key]["wins"] += int(won)
        results[key]["total"] += 1
        results[key]["returns"].append(pct_change)

    output = {}
    for (regime, direction), data in results.items():
        if data["total"] < 5:
            continue
        wr = data["wins"] / data["total"]
        avg_ret = sum(data["returns"]) / len(data["returns"])
        output[(regime, direction)] = (round(wr, 4), data["total"], round(avg_ret * 100, 2))

    return output


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

def store_win_rates(
    conn: sqlite3.Connection,
    ticker: str,
    strategy: str,
    rates: Dict[Tuple[str, str], Tuple[float, int, float]],
) -> int:
    """Store computed win rates. Returns count stored."""
    now = datetime.now(timezone.utc).isoformat()
    stored = 0
    for (regime, direction), (win_rate, sample_count, avg_return) in rates.items():
        conn.execute(
            "INSERT OR REPLACE INTO historical_win_rates "
            "(ticker, strategy, regime, direction, win_rate, sample_count, avg_return_pct, last_updated) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ticker, strategy, regime, direction, win_rate, sample_count, avg_return, now),
        )
        stored += 1
    conn.commit()
    return stored


def store_regime_history(
    conn: sqlite3.Connection,
    vix_by_date: Dict[str, float],
) -> int:
    """Store daily regime classifications."""
    now = datetime.now(timezone.utc).isoformat()
    stored = 0
    for date, vix in sorted(vix_by_date.items()):
        regime = classify_vix(vix)
        conn.execute(
            "INSERT OR REPLACE INTO regime_history (date, vix_close, regime) VALUES (?,?,?)",
            (date, vix, regime),
        )
        stored += 1
    conn.commit()
    return stored


def update_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Update backtest_meta tracking."""
    conn.execute(
        "INSERT OR REPLACE INTO backtest_meta (key, value) VALUES (?,?)",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(limit: Optional[int] = None, single_ticker: Optional[str] = None) -> None:
    """
    Run the full backtest population job.

    Args:
        limit:         Stop after this many tickers (for testing)
        single_ticker: Only process this one ticker
    """
    from database import init_backtest_db
    conn = init_backtest_db(BACKTEST_DB_PATH)

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=LOOKBACK_YEARS * 365 + 30)).strftime("%Y-%m-%d")

    log.info("Backtest population starting: %s → %s", start_date, end_date)
    log.info("Universe: %d tickers | Lookback: %d years", len(TIER_1_UNIVERSE), LOOKBACK_YEARS)

    # Step 1: Fetch VIX history once (shared across all tickers)
    log.info("Fetching VIX history...")
    vix_by_date = fetch_vix_history(start_date, end_date)
    if not vix_by_date:
        log.error("Failed to fetch VIX history — cannot classify regimes")
        return

    log.info("VIX history: %d trading days", len(vix_by_date))
    stored_regime = store_regime_history(conn, vix_by_date)
    log.info("Stored %d regime-history rows", stored_regime)
    update_meta(conn, "vix_days_loaded", str(stored_regime))
    time.sleep(RATE_LIMIT_DELAY)

    # Step 2: Process each ticker
    tickers = [single_ticker] if single_ticker else TIER_1_UNIVERSE
    if limit:
        tickers = tickers[:limit]

    total_stored = 0
    for idx, ticker in enumerate(tickers):
        log.info("[%d/%d] Processing %s...", idx + 1, len(tickers), ticker)

        # Check if already done
        existing = conn.execute(
            "SELECT COUNT(*) FROM historical_win_rates WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        if existing > 0:
            log.info("  %s already populated (%d rows) — skipping", ticker, existing)
            continue

        bars = fetch_price_history(ticker, start_date, end_date)
        if len(bars) < 50:
            log.warning("  %s: insufficient price data (%d bars) — skipping", ticker, len(bars))
            time.sleep(RATE_LIMIT_DELAY)
            continue

        log.info("  %s: %d bars fetched", ticker, len(bars))

        # Compute bull_put_spread win rates
        spread_rates = compute_bull_put_spread_wr(bars, vix_by_date)
        n_spread = store_win_rates(conn, ticker, "bull_put_spread", spread_rates)

        # Compute swing_long win rates
        swing_rates = compute_swing_long_wr(bars, vix_by_date)
        n_swing = store_win_rates(conn, ticker, "swing_long", swing_rates)

        total_stored += n_spread + n_swing
        log.info("  %s: stored %d spread + %d swing rows", ticker, n_spread, n_swing)

        update_meta(conn, "last_ticker_processed", ticker)
        update_meta(conn, "tickers_completed", str(idx + 1))
        update_meta(conn, "total_rows_stored", str(total_stored))

        time.sleep(RATE_LIMIT_DELAY)

    # Final summary
    total_rows = conn.execute("SELECT COUNT(*) FROM historical_win_rates").fetchone()[0]
    total_regime = conn.execute("SELECT COUNT(*) FROM regime_history").fetchone()[0]
    now = datetime.now(timezone.utc).isoformat()

    update_meta(conn, "population_complete", now)
    update_meta(conn, "final_row_count", str(total_rows))

    log.info("=" * 60)
    log.info("BACKTEST POPULATION COMPLETE")
    log.info("  Win rate rows: %d", total_rows)
    log.info("  Regime history rows: %d", total_regime)
    log.info("  Tickers processed: %d", len(tickers))
    log.info("=" * 60)

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AILS Backtest Populator")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N tickers (testing)")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Process only this ticker")
    args = parser.parse_args()

    run(limit=args.limit, single_ticker=args.ticker)
