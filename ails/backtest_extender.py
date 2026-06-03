"""
backtest_extender.py — Extended Strategy Backtest Population
============================================================
Adds four missing strategies to backtest.db alongside the existing
bull_put_spread and swing_long data:

  NEW STRATEGIES:
    swing_5day     — 5-day hold, wins if close > entry (MA20 trend filter)
    intraday_2day  — 2-day hold, wins if close > entry
    0dte_call      — Same-day open→close, wins if close > open + 0.3% (call territory)
    0dte_put       — Same-day open→close, wins if close < open - 0.3% (put territory)

REGIME THRESHOLDS (from ails/config.py):
    LOW_VOL    VIX ≤ 12
    NORMAL     VIX ≤ 20
    ELEVATED   VIX ≤ 30
    STRESS     VIX ≤ 40
    HIGH_STRESS VIX ≤ 55
    CRISIS     VIX > 55

Designed to run AFTER backtest_populator.py has completed.
Safe to re-run — skips tickers that already have each strategy populated.
Only fetches price data once per ticker (shared across all 4 new strategies).

Usage:
    cd /Users/ahmedsadek/nexus/ails
    source .venv/bin/activate
    python backtest_extender.py [--limit N] [--ticker TICKER]

    --limit N      Stop after N tickers (for testing)
    --ticker T     Run only for a single ticker
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

from config import REGIME_THRESHOLDS
from universe_full import FULL_UNIVERSE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ails.backtest_extender")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLYGON_API_KEY: str = os.environ["POLYGON_API_KEY"]
BACKTEST_DB_PATH: str = os.environ.get(
    "BACKTEST_DB_PATH", "/Users/ahmedsadek/nexus/data/backtest.db"
)

POLYGON_BASE = "https://api.polygon.io"
RATE_LIMIT_DELAY = 0.25       # 4 req/sec

LOOKBACK_YEARS = 5

# Strategy parameters
SWING_5DAY_HOLD = 5           # trading days
INTRADAY_2DAY_HOLD = 2        # trading days
ZERO_DTE_MIN_MOVE = 0.003     # 0.3% minimum move to profit on 0DTE (covers premium)

NEW_STRATEGIES = ["swing_5day", "intraday_2day", "0dte_call", "0dte_put"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_vix(vix: float) -> str:
    for regime, threshold in REGIME_THRESHOLDS.items():
        if vix <= threshold:
            return regime
    return "CRISIS"


def _get(url: str, params: Dict) -> Optional[dict]:
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


def fetch_price_history(ticker: str, start_date: str, end_date: str) -> List[Dict]:
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


def load_vix_from_db(conn: sqlite3.Connection) -> Dict[str, float]:
    """Load pre-existing VIX regime history from backtest.db — no re-fetch needed."""
    rows = conn.execute("SELECT date, vix_close FROM regime_history").fetchall()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# Strategy computation functions
# ---------------------------------------------------------------------------

def compute_swing_5day_wr(
    bars: List[Dict],
    vix_by_date: Dict[str, float],
) -> Dict[Tuple[str, str], Tuple[float, int, float]]:
    """
    Simulate 5-day swing long entries.
    Entry filter: price above 20-day MA (uptrend confirmation).
    Exit: 5 trading days later.
    Win: close at exit > entry close.
    """
    results: Dict[Tuple[str, str], Dict] = {}

    if len(bars) < 25:
        return {}

    closes = [b["close"] for b in bars]

    for i in range(20, len(bars) - SWING_5DAY_HOLD):
        bar = bars[i]
        date = bar["date"]
        vix = vix_by_date.get(date, 18.0)
        regime = classify_vix(vix)

        ma20 = sum(closes[i - 20:i]) / 20
        entry_price = bar["close"]

        if entry_price < ma20 * 0.99:
            continue

        exit_bar = bars[i + SWING_5DAY_HOLD]
        exit_price = exit_bar["close"]
        pct_change = (exit_price - entry_price) / entry_price

        key = (regime, "bullish")
        if key not in results:
            results[key] = {"wins": 0, "total": 0, "returns": []}
        results[key]["wins"] += int(pct_change > 0)
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


def compute_intraday_2day_wr(
    bars: List[Dict],
    vix_by_date: Dict[str, float],
) -> Dict[Tuple[str, str], Tuple[float, int, float]]:
    """
    Simulate 2-day intraday hold.
    Entry: close of day 0 (signal day).
    Exit: close of day 2.
    Win: exit close > entry close.
    No trend filter — captures all market conditions.
    Both bullish (long) and bearish (short) directions tracked.
    """
    results: Dict[Tuple[str, str], Dict] = {}

    if len(bars) < 5:
        return {}

    for i in range(len(bars) - INTRADAY_2DAY_HOLD):
        bar = bars[i]
        date = bar["date"]
        vix = vix_by_date.get(date, 18.0)
        regime = classify_vix(vix)

        entry_price = bar["close"]
        exit_bar = bars[i + INTRADAY_2DAY_HOLD]
        exit_price = exit_bar["close"]
        pct_change = (exit_price - entry_price) / entry_price

        # Bullish (long) direction
        key_bull = (regime, "bullish")
        if key_bull not in results:
            results[key_bull] = {"wins": 0, "total": 0, "returns": []}
        results[key_bull]["wins"] += int(pct_change > 0)
        results[key_bull]["total"] += 1
        results[key_bull]["returns"].append(pct_change)

        # Bearish (short) direction
        key_bear = (regime, "bearish")
        if key_bear not in results:
            results[key_bear] = {"wins": 0, "total": 0, "returns": []}
        results[key_bear]["wins"] += int(pct_change < 0)
        results[key_bear]["total"] += 1
        results[key_bear]["returns"].append(-pct_change)

    output = {}
    for (regime, direction), data in results.items():
        if data["total"] < 5:
            continue
        wr = data["wins"] / data["total"]
        avg_ret = sum(data["returns"]) / len(data["returns"])
        output[(regime, direction)] = (round(wr, 4), data["total"], round(avg_ret * 100, 2))
    return output


def compute_0dte_call_wr(
    bars: List[Dict],
    vix_by_date: Dict[str, float],
) -> Dict[Tuple[str, str], Tuple[float, int, float]]:
    """
    Simulate 0DTE call entries using daily OHLC.
    Entry: open of the day.
    Exit: close of the same day.
    Win: close > open + ZERO_DTE_MIN_MOVE (0.3%) — covers premium decay,
         approximates ATM call profitability on a bullish intraday move.
    Regime-classified by VIX that morning.
    """
    results: Dict[Tuple[str, str], Dict] = {}

    for bar in bars:
        date = bar["date"]
        vix = vix_by_date.get(date, 18.0)
        regime = classify_vix(vix)

        open_px = bar["open"]
        close_px = bar["close"]

        if open_px <= 0:
            continue

        pct_move = (close_px - open_px) / open_px
        won = pct_move > ZERO_DTE_MIN_MOVE

        key = (regime, "bullish")
        if key not in results:
            results[key] = {"wins": 0, "total": 0, "returns": []}
        results[key]["wins"] += int(won)
        results[key]["total"] += 1
        results[key]["returns"].append(pct_move)

    output = {}
    for (regime, direction), data in results.items():
        if data["total"] < 5:
            continue
        wr = data["wins"] / data["total"]
        avg_ret = sum(data["returns"]) / len(data["returns"])
        output[(regime, direction)] = (round(wr, 4), data["total"], round(avg_ret * 100, 2))
    return output


def compute_0dte_put_wr(
    bars: List[Dict],
    vix_by_date: Dict[str, float],
) -> Dict[Tuple[str, str], Tuple[float, int, float]]:
    """
    Simulate 0DTE put entries using daily OHLC.
    Entry: open of the day.
    Exit: close of the same day.
    Win: close < open - ZERO_DTE_MIN_MOVE (0.3%) — approximates ATM put
         profitability on a bearish intraday move.
    """
    results: Dict[Tuple[str, str], Dict] = {}

    for bar in bars:
        date = bar["date"]
        vix = vix_by_date.get(date, 18.0)
        regime = classify_vix(vix)

        open_px = bar["open"]
        close_px = bar["close"]

        if open_px <= 0:
            continue

        pct_move = (open_px - close_px) / open_px  # positive = bearish move
        won = pct_move > ZERO_DTE_MIN_MOVE

        key = (regime, "bearish")
        if key not in results:
            results[key] = {"wins": 0, "total": 0, "returns": []}
        results[key]["wins"] += int(won)
        results[key]["total"] += 1
        results[key]["returns"].append(pct_move)

    output = {}
    for (regime, direction), data in results.items():
        if data["total"] < 5:
            continue
        wr = data["wins"] / data["total"]
        avg_ret = sum(data["returns"]) / len(data["returns"])
        output[(regime, direction)] = (round(wr, 4), data["total"], round(avg_ret * 100, 2))
    return output


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def store_win_rates(
    conn: sqlite3.Connection,
    ticker: str,
    strategy: str,
    rates: Dict[Tuple[str, str], Tuple[float, int, float]],
) -> int:
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


def ticker_has_strategy(conn: sqlite3.Connection, ticker: str, strategy: str) -> bool:
    count = conn.execute(
        "SELECT COUNT(*) FROM historical_win_rates WHERE ticker=? AND strategy=?",
        (ticker, strategy),
    ).fetchone()[0]
    return count > 0


def update_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO backtest_meta (key, value) VALUES (?,?)",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(limit: Optional[int] = None, single_ticker: Optional[str] = None) -> None:
    conn = sqlite3.connect(BACKTEST_DB_PATH)

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (
        datetime.now() - timedelta(days=LOOKBACK_YEARS * 365 + 30)
    ).strftime("%Y-%m-%d")

    log.info("Backtest EXTENSION starting: %s → %s", start_date, end_date)
    log.info("New strategies: %s", NEW_STRATEGIES)

    # Load VIX history from existing regime_history (no re-fetch)
    log.info("Loading VIX history from regime_history...")
    vix_by_date = load_vix_from_db(conn)
    if not vix_by_date:
        log.error("No regime_history found — run backtest_populator.py first")
        return
    log.info("Loaded %d VIX days from DB", len(vix_by_date))

    tickers = [single_ticker] if single_ticker else FULL_UNIVERSE
    if limit:
        tickers = tickers[:limit]

    total_stored = 0
    skipped = 0

    for idx, ticker in enumerate(tickers):
        # Check which strategies are missing for this ticker
        missing = [
            s for s in NEW_STRATEGIES
            if not ticker_has_strategy(conn, ticker, s)
        ]

        if not missing:
            skipped += 1
            if skipped % 50 == 0:
                log.info("Skipped %d already-complete tickers...", skipped)
            continue

        log.info(
            "[%d/%d] %s — computing: %s",
            idx + 1, len(tickers), ticker, missing
        )

        # Fetch price history once per ticker (shared across all 4 strategies)
        bars = fetch_price_history(ticker, start_date, end_date)
        if len(bars) < 10:
            log.warning("  %s: insufficient bars (%d) — skipping", ticker, len(bars))
            time.sleep(RATE_LIMIT_DELAY)
            continue

        log.info("  %s: %d bars fetched", ticker, len(bars))

        ticker_stored = 0

        if "swing_5day" in missing:
            rates = compute_swing_5day_wr(bars, vix_by_date)
            n = store_win_rates(conn, ticker, "swing_5day", rates)
            ticker_stored += n
            log.info("  swing_5day: %d rows", n)

        if "intraday_2day" in missing:
            rates = compute_intraday_2day_wr(bars, vix_by_date)
            n = store_win_rates(conn, ticker, "intraday_2day", rates)
            ticker_stored += n
            log.info("  intraday_2day: %d rows", n)

        if "0dte_call" in missing:
            rates = compute_0dte_call_wr(bars, vix_by_date)
            n = store_win_rates(conn, ticker, "0dte_call", rates)
            ticker_stored += n
            log.info("  0dte_call: %d rows", n)

        if "0dte_put" in missing:
            rates = compute_0dte_put_wr(bars, vix_by_date)
            n = store_win_rates(conn, ticker, "0dte_put", rates)
            ticker_stored += n
            log.info("  0dte_put: %d rows", n)

        total_stored += ticker_stored
        update_meta(conn, "extension_last_ticker", ticker)
        update_meta(conn, "extension_rows_added", str(total_stored))

        time.sleep(RATE_LIMIT_DELAY)

    # Final summary
    total_rows = conn.execute(
        "SELECT COUNT(*) FROM historical_win_rates"
    ).fetchone()[0]
    now = datetime.now(timezone.utc).isoformat()
    update_meta(conn, "extension_complete", now)
    update_meta(conn, "extension_final_row_count", str(total_rows))

    log.info("=" * 60)
    log.info("BACKTEST EXTENSION COMPLETE")
    log.info("  New rows added:       %d", total_stored)
    log.info("  Total rows in DB:     %d", total_rows)
    log.info("  Tickers processed:    %d", len(tickers) - skipped)
    log.info("  Tickers skipped:      %d", skipped)
    log.info("=" * 60)

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AILS Backtest Extender")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N tickers (testing)")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Process only this ticker")
    args = parser.parse_args()

    run(limit=args.limit, single_ticker=args.ticker)
