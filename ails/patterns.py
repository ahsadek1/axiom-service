"""
patterns.py — Pattern library and similarity search.
Stores setup vectors and finds similar historical setups for new trades.
"""

import json
import math
import logging
import sqlite3
from typing import List, Dict, Any, Optional

log = logging.getLogger(__name__)


def _similarity_key(ticker: str, regime: str, strategy: str) -> str:
    """
    Generate a coarse similarity key for fast lookup.
    Groups setups by (regime, strategy) — regime + strategy is the primary dimension.

    Args:
        ticker:   Ticker symbol
        regime:   Market regime
        strategy: Trade strategy type

    Returns:
        Similarity key string
    """
    return f"{regime}:{strategy}"


def _vector_distance(v1: Dict[str, float], v2: Dict[str, float]) -> float:
    """
    Euclidean distance between two setup vectors.
    Only compares keys present in both vectors.

    Args:
        v1: First setup vector
        v2: Second setup vector

    Returns:
        Distance (lower = more similar)
    """
    keys = set(v1.keys()) & set(v2.keys())
    if not keys:
        return float("inf")
    return math.sqrt(sum((v1[k] - v2[k]) ** 2 for k in keys))


def store_pattern(
    ticker: str,
    setup_vector: Dict[str, float],
    outcome: bool,
    regime: str,
    pnl: float,
    strategy: str,
    conn: sqlite3.Connection,
) -> None:
    """
    Store a trade setup as a pattern for future similarity search.

    Args:
        ticker:       Ticker symbol
        setup_vector: Dict of signal values (iv_rank, rsi, momentum, etc.)
        outcome:      True if trade was profitable
        regime:       Market regime at entry
        pnl:          Realized PnL
        strategy:     Strategy type
        conn:         AILS DB connection
    """
    from datetime import datetime, timezone
    key = _similarity_key(ticker, regime, strategy)
    conn.execute(
        "INSERT INTO pattern_library (ts, ticker, setup_vector, outcome, regime, pnl, similarity_key) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            ticker,
            json.dumps(setup_vector),
            int(outcome),
            regime,
            pnl,
            key,
        ),
    )
    conn.commit()


def find_similar(
    setup_vector: Dict[str, float],
    regime: str,
    strategy: str,
    conn: sqlite3.Connection,
    top_n: int = 10,
) -> List[Dict[str, Any]]:
    """
    Find the N most similar historical setups to the given vector.

    Args:
        setup_vector: Current trade setup signals
        regime:       Current market regime
        strategy:     Strategy type
        conn:         AILS DB connection
        top_n:        Number of similar setups to return

    Returns:
        List of similar setups with outcome, pnl, distance, ticker fields
    """
    key = _similarity_key("", regime, strategy)
    rows = conn.execute(
        "SELECT ticker, setup_vector, outcome, pnl FROM pattern_library "
        "WHERE similarity_key=? ORDER BY ts DESC LIMIT 500",
        (key,),
    ).fetchall()

    if not rows:
        return []

    scored: List[Dict[str, Any]] = []
    for row in rows:
        try:
            stored_vec = json.loads(row["setup_vector"])
            dist = _vector_distance(setup_vector, stored_vec)
            scored.append({
                "ticker": row["ticker"],
                "outcome": bool(row["outcome"]),
                "pnl": row["pnl"],
                "distance": round(dist, 4),
            })
        except (json.JSONDecodeError, TypeError):
            continue

    scored.sort(key=lambda x: x["distance"])
    top = scored[:top_n]

    if not top:
        return []

    # Compute summary stats
    win_rate = sum(1 for s in top if s["outcome"]) / len(top)
    avg_pnl = sum(s["pnl"] for s in top) / len(top)

    return [{
        **s,
        "summary": {
            "win_rate": round(win_rate, 3),
            "avg_pnl": round(avg_pnl, 2),
            "sample_count": len(top),
        }
    } for s in top]
