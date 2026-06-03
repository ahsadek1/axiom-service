"""
confidence_engine.py — Signal Quality Gate
===========================================
Ensures every signal entering the trading loop
carries explicit data quality metadata.

No silent defaults. No false conviction.
Every GO signal must declare what data was used.

Rules:
  HIGH confidence   → trade at full size
  MEDIUM confidence → trade at 85% size
  LOW confidence    → trade at 60% size, flag in DB
  ABSENT            → NO TRADE, period

Ahmed directive May 2026:
  "The system has no way to distinguish between a real
   signal and a default signal. This is worse than failing
   loudly. A silent failure that produces false confidence
   is more dangerous than no signal at all."
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from nexus_scorer import NexusScore, Confidence

log = logging.getLogger("nexus.confidence")

CONFIDENCE_DB = os.getenv("PRIME_V2_DB",
                "/Users/ahmedsadek/nexus/data/prime_v2.db")

# Size multipliers by confidence level
CONFIDENCE_SIZE_MULT = {
    Confidence.HIGH:   1.00,
    Confidence.MEDIUM: 0.85,
    Confidence.LOW:    0.60,
    Confidence.ABSENT: 0.00,   # never trade
}

# Minimum confidence to trade
MIN_TRADEABLE_CONFIDENCE = Confidence.LOW

# Confidence order for comparison
CONF_ORDER = [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW, Confidence.ABSENT]


def confidence_rank(c: Confidence) -> int:
    """Lower rank = better confidence."""
    return CONF_ORDER.index(c)


def is_tradeable(score: NexusScore) -> bool:
    """
    Can this score be traded?
    Must meet GO threshold AND have tradeable confidence.
    """
    if score.confidence == Confidence.ABSENT:
        return False
    if not score.go:
        return False
    return True


def get_size_multiplier(score: NexusScore, axiom_fallback: bool = False) -> float:
    """
    Get position size multiplier based on confidence.
    Additional penalty if running on backup universe.
    """
    base_mult = CONFIDENCE_SIZE_MULT.get(score.confidence, 0.0)
    if axiom_fallback:
        base_mult *= 0.70   # additional 30% penalty for backup universe
    return round(base_mult, 2)


def evaluate_signal(
    score:          NexusScore,
    axiom_fallback: bool = False,
) -> dict:
    """
    Full signal evaluation with quality metadata.
    Returns complete signal package for execution engine.

    This is the gate every trade must pass through.
    """
    tradeable    = is_tradeable(score)
    size_mult    = get_size_multiplier(score, axiom_fallback) if tradeable else 0.0
    missing_dims = [
        dim for dim, s in [
            ("technical", score.technical),
            ("options",   score.options),
            ("macro",     score.macro),
        ]
        if s.confidence == Confidence.ABSENT
    ]
    all_missing = (
        score.technical.data_missing +
        score.options.data_missing +
        score.macro.data_missing
    )

    quality_flags = []
    if score.confidence == Confidence.MEDIUM:
        quality_flags.append("MEDIUM_CONFIDENCE")
    if score.confidence == Confidence.LOW:
        quality_flags.append("LOW_CONFIDENCE")
    if axiom_fallback:
        quality_flags.append("AXIOM_FALLBACK")
    if missing_dims:
        quality_flags.append(f"DIMS_ABSENT:{','.join(missing_dims)}")
    if score.options.confidence == Confidence.ABSENT:
        quality_flags.append("NO_OPTIONS_DATA")
    if score.strong_go:
        quality_flags.append("STRONG_GO")

    signal = {
        "ticker":          score.ticker,
        "direction":       score.direction,
        "combined_score":  score.combined,
        "confidence":      score.confidence.value,
        "tradeable":       tradeable,
        "size_mult":       size_mult,
        "go":              score.go,
        "strong_go":       score.strong_go,
        "quality_flags":   quality_flags,
        "missing_data":    all_missing,
        "missing_dims":    missing_dims,
        "dimensions": {
            "technical": {
                "score":      score.technical.score,
                "confidence": score.technical.confidence.value,
                "factors":    score.technical.factors,
            },
            "options": {
                "score":      score.options.score,
                "confidence": score.options.confidence.value,
                "factors":    score.options.factors,
            },
            "macro": {
                "score":      score.macro.score,
                "confidence": score.macro.confidence.value,
                "factors":    score.macro.factors,
            },
        },
        "axiom_fallback":  axiom_fallback,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }

    if tradeable:
        log.info(
            "SIGNAL: %s %s score=%.1f conf=%s size_mult=%.2f flags=%s",
            score.ticker, score.direction, score.combined,
            score.confidence.value, size_mult,
            quality_flags or "clean",
        )
    else:
        log.info(
            "BLOCKED: %s %s score=%.1f conf=%s reason=%s",
            score.ticker, score.direction, score.combined,
            score.confidence.value,
            "ABSENT" if score.confidence == Confidence.ABSENT else "BELOW_THRESHOLD",
        )

    return signal


def filter_tradeable_signals(
    scores:         list[NexusScore],
    axiom_fallback: bool = False,
    max_signals:    int  = 10,
) -> list[dict]:
    """
    Filter and rank a pool of scores to tradeable signals.
    Returns sorted list — best first.
    Applies all quality gates.
    """
    signals = []
    for score in scores:
        signal = evaluate_signal(score, axiom_fallback)
        if signal["tradeable"]:
            signals.append(signal)

    # Sort: strong_go first, then by confidence rank, then by score
    signals.sort(key=lambda s: (
        0 if s["strong_go"] else 1,
        confidence_rank(Confidence(s["confidence"])),
        -s["combined_score"],
    ))

    result = signals[:max_signals]
    log.info(
        "Signal filter: %d/%d tradeable, returning top %d",
        len(signals), len(scores), len(result),
    )
    return result


def log_signal_to_db(signal: dict, position_id: Optional[str] = None) -> None:
    """
    Persist signal quality metadata to DB.
    Used by learning loop to correlate signal quality with outcomes.
    """
    try:
        conn = sqlite3.connect(CONFIDENCE_DB, timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id     TEXT,
                ticker          TEXT,
                direction       TEXT,
                combined_score  REAL,
                confidence      TEXT,
                size_mult       REAL,
                strong_go       INTEGER,
                quality_flags   TEXT,
                missing_dims    TEXT,
                axiom_fallback  INTEGER,
                ts              REAL,
                created_at      TEXT
            )
        """)
        import json
        conn.execute("""
            INSERT INTO signal_log
            (position_id, ticker, direction, combined_score, confidence,
             size_mult, strong_go, quality_flags, missing_dims,
             axiom_fallback, ts, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            position_id,
            signal["ticker"],
            signal["direction"],
            signal["combined_score"],
            signal["confidence"],
            signal["size_mult"],
            int(signal["strong_go"]),
            ",".join(signal["quality_flags"]),
            ",".join(signal["missing_dims"]),
            int(signal["axiom_fallback"]),
            time.time(),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.debug("Signal log failed: %s", exc)


def get_confidence_stats(days: int = 7) -> dict:
    """
    Analyze signal quality distribution over past N days.
    Shows what fraction of signals trade at full vs reduced size.
    """
    try:
        conn = sqlite3.connect(CONFIDENCE_DB, timeout=5)
        rows = conn.execute("""
            SELECT confidence, COUNT(*), AVG(combined_score)
            FROM signal_log
            WHERE ts > ?
            GROUP BY confidence
        """, (time.time() - days * 86400,)).fetchall()
        conn.close()
        return {
            "period_days": days,
            "by_confidence": [
                {"confidence": r[0], "count": r[1], "avg_score": round(r[2],1)}
                for r in rows
            ],
        }
    except Exception:
        return {"period_days": days, "by_confidence": []}
