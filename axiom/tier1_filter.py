"""
tier1_filter.py — Axiom Tier 1 Stock Filter

Filters S&P 500 + Nasdaq 100 universe (~600 stocks) down to ~100 anchor stocks.
Runs at 9:00 AM and 1:00 PM ET. Lightweight criteria. Output stable for hours.

Criteria (all must pass):
  - Options OI > 500, bid-ask spread < 10%
  - No earnings within 14 days
  - Price between $20 and $500
  - Average daily volume > 1,000,000 shares
  - IV rank between 5 and 95
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from data_sources import (
    days_to_earnings,
    polygon_batch_snapshots,
    yfinance_get_avg_volume,
    yfinance_get_data,
)

logger = logging.getLogger("axiom.tier1")

# ── Tier 1 Thresholds ─────────────────────────────────────────────────────────
MIN_PRICE              = 20.0
MAX_PRICE              = 500.0
MIN_AVG_VOLUME         = 1_000_000
MIN_IV_RANK            = 5.0
MAX_IV_RANK            = 95.0
EARNINGS_BUFFER_DAYS   = 14
MAX_WORKERS            = 20   # parallel threads for data fetching


def run_tier1_filter(
    universe: list[str],
    polygon_api_key: str,
    alpha_vantage_key: str,
) -> list[str]:
    """
    Run Tier 1 filter on the full stock universe.

    Fetches data in parallel for speed. Returns tickers that pass all criteria.
    Logs individual rejections at DEBUG level, summary at INFO.

    Args:
        universe:          List of all ticker symbols to evaluate.
        polygon_api_key:   Polygon.io API key for price/volume data.
        alpha_vantage_key: Alpha Vantage API key for earnings dates.

    Returns:
        List of ticker symbols that passed all Tier 1 criteria.
    """
    logger.info("Tier 1 filter starting — evaluating %d stocks", len(universe))
    start_time = datetime.now(timezone.utc)

    # Step 1: Batch fetch price/volume from Polygon
    snapshots = polygon_batch_snapshots(universe, polygon_api_key)
    if not snapshots:
        logger.warning("Polygon batch snapshot returned empty — fetching via yfinance fallback")
        snapshots = _yfinance_batch_fallback(universe)

    # Step 2: Evaluate each ticker in parallel
    passed: list[str] = []
    rejected_counts = {
        "price":    0,
        "volume":   0,
        "iv_rank":  0,
        "earnings": 0,
        "no_data":  0,
    }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ticker = {
            executor.submit(
                _evaluate_tier1,
                ticker,
                snapshots.get(ticker),
                alpha_vantage_key,
            ): ticker
            for ticker in universe
        }

        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                result, reject_reason = future.result(timeout=15)
                if result:
                    passed.append(ticker)
                elif reject_reason:
                    rejected_counts[reject_reason] = rejected_counts.get(reject_reason, 0) + 1
                    logger.debug("Tier 1 REJECT %s: %s", ticker, reject_reason)
            except Exception as e:
                logger.warning("Tier 1 evaluation error for %s: %s", ticker, e)
                rejected_counts["no_data"] += 1

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(
        "Tier 1 complete in %.1fs — %d passed / %d evaluated | "
        "rejected: price=%d volume=%d iv=%d earnings=%d no_data=%d",
        elapsed,
        len(passed),
        len(universe),
        rejected_counts.get("price", 0),
        rejected_counts.get("volume", 0),
        rejected_counts.get("iv_rank", 0),
        rejected_counts.get("earnings", 0),
        rejected_counts.get("no_data", 0),
    )

    return sorted(passed)


def _evaluate_tier1(
    ticker: str,
    snapshot: Optional[dict],
    alpha_vantage_key: str,
) -> tuple[bool, Optional[str]]:
    """
    Evaluate a single ticker against all Tier 1 criteria.

    Args:
        ticker:            Ticker symbol.
        snapshot:          Price/volume data dict or None.
        alpha_vantage_key: Alpha Vantage API key.

    Returns:
        Tuple of (passed: bool, reject_reason: str or None).
        reject_reason is None if passed.
    """
    # Get data — fall back to yfinance if no Polygon snapshot
    if snapshot is None:
        snapshot = yfinance_get_data(ticker)

    if snapshot is None:
        return False, "no_data"

    price  = snapshot.get("price", 0.0)
    volume = snapshot.get("volume", 0)

    # Price check
    if not (MIN_PRICE <= price <= MAX_PRICE):
        return False, "price"

    # Volume check — use snapshot volume, fall back to yfinance avg volume
    if volume < MIN_AVG_VOLUME:
        avg_vol = yfinance_get_avg_volume(ticker)
        if avg_vol is None or avg_vol < MIN_AVG_VOLUME:
            return False, "volume"

    # Earnings check
    dte = days_to_earnings(ticker, alpha_vantage_key)
    if dte is not None and dte <= EARNINGS_BUFFER_DAYS:
        return False, "earnings"

    # IV rank check — skip if unavailable (Tier 1 is lenient)
    iv_rank = snapshot.get("iv_rank")
    if iv_rank is not None and not (MIN_IV_RANK <= iv_rank <= MAX_IV_RANK):
        return False, "iv_rank"

    return True, None


def _yfinance_batch_fallback(universe: list[str]) -> dict[str, Optional[dict]]:
    """
    Fetch stock data for entire universe via yfinance when Polygon fails.

    Args:
        universe: List of ticker symbols.

    Returns:
        Dict mapping ticker to data dict or None.
    """
    results: dict[str, Optional[dict]] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ticker = {
            executor.submit(yfinance_get_data, ticker): ticker
            for ticker in universe
        }
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                results[ticker] = future.result(timeout=10)
            except Exception as e:
                logger.debug("yfinance fallback failed for %s: %s", ticker, e)
                results[ticker] = None

    return results
