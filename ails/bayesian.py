"""
bayesian.py — Bayesian win rate updating.
Historical backtest = prior. Live outcomes = updates.
Live outcomes carry LIVE_OUTCOME_WEIGHT× more weight than historical data.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from config import LIVE_OUTCOME_WEIGHT, MIN_LIVE_OUTCOMES_TO_SHIFT, MIN_BACKTEST_SAMPLES

log = logging.getLogger(__name__)


def compute_blended_rate(
    prior_wins: float,
    prior_n: int,
    live_wins: float,
    live_n: int,
) -> float:
    """
    Compute Bayesian blended win rate.
    Live outcomes weighted LIVE_OUTCOME_WEIGHT× vs historical.

    Args:
        prior_wins:  Historical wins count
        prior_n:     Historical sample count
        live_wins:   Live wins count
        live_n:      Live sample count

    Returns:
        Blended win rate between 0.0 and 1.0
    """
    weighted_live_n = live_n * LIVE_OUTCOME_WEIGHT
    weighted_live_wins = live_wins * LIVE_OUTCOME_WEIGHT
    total_n = prior_n + weighted_live_n
    if total_n == 0:
        return 0.5  # No data — return neutral prior
    return (prior_wins + weighted_live_wins) / total_n


def confidence_label(prior_n: int, live_n: int) -> str:
    """
    Label the confidence level of a blended rate estimate.

    Args:
        prior_n: Historical sample count
        live_n:  Live outcome count

    Returns:
        Confidence label string
    """
    if live_n >= 30:
        return "high_live"
    if live_n >= MIN_LIVE_OUTCOMES_TO_SHIFT:
        return "mixed"
    if prior_n >= MIN_BACKTEST_SAMPLES:
        return "backtest_only"
    return "insufficient"


def get_win_rate(
    ticker: str,
    strategy: str,
    regime: str,
    direction: str,
    ails_conn: sqlite3.Connection,
    backtest_conn: sqlite3.Connection,
) -> Dict[str, Any]:
    """
    Get the best available win rate for a (ticker, strategy, regime, direction) combination.

    Priority:
    1. Ticker-specific blended rate from bayesian_rates
    2. Regime-level rate from regime_level_rates (research data)
    3. Neutral prior (0.5)

    Args:
        ticker:        Ticker symbol
        strategy:      Strategy type (bull_put_spread, swing_long, etc.)
        regime:        Current regime
        direction:     Trade direction (bullish, bearish, neutral)
        ails_conn:     Live AILS DB connection
        backtest_conn: Historical backtest DB connection

    Returns:
        Dict with win_rate, confidence, sample_count, source fields
    """
    # Normalize unknown/missing regime to NORMAL (most common: 65.6% of trading days).
    # UNKNOWN occurs when Axiom hasn't fetched VIX yet (market closed, startup).
    # Using NORMAL as fallback is conservative and statistically correct.
    if not regime or regime.upper() in ("UNKNOWN", ""):
        regime = "NORMAL"

    # Try ticker-specific blended rate
    # Adversarial fix #1: query MUST include direction — bullish and bearish rates
    # are different and must never be blended together.
    row = ails_conn.execute(
        "SELECT blended_rate, confidence, prior_n, live_n FROM bayesian_rates "
        "WHERE ticker=? AND strategy=? AND regime=? AND direction=?",
        (ticker, strategy, regime, direction),
    ).fetchone()

    if row and (row["prior_n"] >= MIN_BACKTEST_SAMPLES or row["live_n"] > 0):
        return {
            "win_rate": round(row["blended_rate"], 4),
            "confidence": row["confidence"],
            "sample_count": row["prior_n"] + row["live_n"],
            "source": "bayesian_blended",
        }

    # Fall back to regime-level rates
    regime_row = backtest_conn.execute(
        "SELECT win_rate, sample_count FROM regime_level_rates "
        "WHERE strategy=? AND regime=? AND direction=?",
        (strategy, regime, direction),
    ).fetchone()

    if regime_row:
        return {
            "win_rate": round(regime_row["win_rate"], 4),
            "confidence": "regime_level",
            "sample_count": regime_row["sample_count"],
            "source": "research_prior",
        }

    # Neutral fallback
    return {
        "win_rate": 0.50,
        "confidence": "insufficient",
        "sample_count": 0,
        "source": "neutral_prior",
    }


def ingest_outcome(
    ticker: str,
    strategy: str,
    regime: str,
    direction: str,
    win: bool,
    ails_conn: sqlite3.Connection,
    backtest_conn: sqlite3.Connection,
    auto_commit: bool = False,
) -> None:
    """
    Update bayesian_rates with a new live trade outcome.

    Args:
        ticker:        Ticker symbol
        strategy:      Strategy type
        regime:        Regime at trade entry
        direction:     Trade direction
        win:           Whether the trade was profitable
        ails_conn:     Live AILS DB connection
        backtest_conn: Historical backtest DB connection
        auto_commit:   If True, commit ails_conn after update. Set False (default)
                       when called from post_outcome() — the outer transaction owns
                       the commit. Set True for standalone callers (backtest populator,
                       scripts) to prevent silent data loss. (OMNI M1 fix)
    """
    now = datetime.now(timezone.utc).isoformat()
    win_int = 1 if win else 0

    # Get prior from backtest (historical foundation)
    hist_row = backtest_conn.execute(
        "SELECT win_rate, sample_count FROM historical_win_rates "
        "WHERE ticker=? AND strategy=? AND regime=? AND direction=?",
        (ticker, strategy, regime, direction),
    ).fetchone()

    if hist_row:
        prior_wins = hist_row["win_rate"] * hist_row["sample_count"]
        prior_n = hist_row["sample_count"]
    else:
        # Use regime-level prior
        rlr = backtest_conn.execute(
            "SELECT win_rate, sample_count FROM regime_level_rates "
            "WHERE strategy=? AND regime=? AND direction=?",
            (strategy, regime, direction),
        ).fetchone()
        if rlr:
            prior_wins = rlr["win_rate"] * rlr["sample_count"]
            prior_n = rlr["sample_count"]
        else:
            prior_wins = 0.5 * 20  # Weak neutral prior: 10/20
            prior_n = 20

    # Upsert bayesian_rates — adversarial fix #1: ALL queries include direction.
    # Bullish and bearish trades must NEVER be blended into the same probability estimate.
    existing = ails_conn.execute(
        "SELECT live_wins, live_n FROM bayesian_rates "
        "WHERE ticker=? AND strategy=? AND regime=? AND direction=?",
        (ticker, strategy, regime, direction),
    ).fetchone()

    if existing:
        new_live_wins = existing["live_wins"] + win_int
        new_live_n = existing["live_n"] + 1
    else:
        new_live_wins = float(win_int)
        new_live_n = 1

    blended = compute_blended_rate(prior_wins, prior_n, new_live_wins, new_live_n)
    confidence = confidence_label(prior_n, new_live_n)

    ails_conn.execute(
        "INSERT INTO bayesian_rates "
        "(ticker, strategy, regime, direction, prior_wins, prior_n, live_wins, live_n, "
        "blended_rate, confidence, last_updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker, strategy, regime, direction) DO UPDATE SET "
        "live_wins=excluded.live_wins, live_n=excluded.live_n, "
        "blended_rate=excluded.blended_rate, confidence=excluded.confidence, "
        "last_updated=excluded.last_updated",
        (ticker, strategy, regime, direction, prior_wins, prior_n,
         new_live_wins, new_live_n, blended, confidence, now),
    )
    if auto_commit:
        ails_conn.commit()
    log.debug("Updated bayesian_rates: %s/%s/%s/%s → %.3f (%s) [commit=%s]",
              ticker, strategy, regime, direction, blended, confidence, auto_commit)
