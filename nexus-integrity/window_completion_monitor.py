"""
window_completion_monitor.py — SOVEREIGN Amendment S4

Tracks scanner window completion rates across the last N windows per agent.

ROOT CAUSE CAUGHT: 2026-04-29, Sage restarted every ~5 minutes all day.
Windows arrived while Sage was down, were recorded as received but completed
with analyzed=0 and completed_at=NULL. v2.1 Stage 2 checks "decisions.created_at
< 20 min ago" — which passes as long as ONE recent decision exists. It cannot
detect that 50% of windows were silently missed.

This monitor checks: of the last 4 windows, how many completed with analyzed > 0?
If rate < 75% (3/4), fire P1. If rate < 50%, fire P0.

Also detects "stale open windows" — windows received but never completed
(analyzed=0, completed_at=NULL) older than 10 minutes. Two or more stale open
windows = service is cycling.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("integrity.window_monitor")

# Per-agent DB paths
AGENT_DBS: Dict[str, str] = {
    "sage":   "/Users/ahmedsadek/nexus/data/sage.db",
    "atlas":  "/Users/ahmedsadek/nexus/data/atlas.db",
    "cipher": "/Users/ahmedsadek/nexus/data/cipher.db",
}

# How many recent windows to evaluate
LOOKBACK_WINDOWS: int = 4

# Completion rate thresholds
MIN_COMPLETION_RATE_P1: float = 0.75   # <75% → P1
MIN_COMPLETION_RATE_P0: float = 0.50   # <50% → P0 (complete breakdown)

# Window age threshold for "stale open" detection (minutes)
STALE_OPEN_THRESHOLD_MINUTES: float = 10.0

# Minimum windows required before monitoring kicks in
MIN_WINDOWS_FOR_MONITORING: int = 2


@dataclass
class AgentWindowStats:
    agent: str
    windows_checked: int
    windows_completed: int       # analyzed > 0 AND completed_at IS NOT NULL
    completion_rate: float
    stale_open_count: int        # windows with analyzed=0 and no completion
    recent_window_ids: List[str] = field(default_factory=list)
    trs_score: float = 100.0    # contribution to TRS agent_reliability sub-score
    alert_severity: Optional[str] = None  # None | "P1" | "P0"
    alert_message: str = ""


@dataclass
class WindowCompletionResult:
    agent_stats: List[AgentWindowStats] = field(default_factory=list)
    overall_trs_score: float = 100.0    # min of all agent TRS scores
    has_alert: bool = False
    worst_severity: Optional[str] = None
    ts: float = field(default_factory=time.time)


def check_window_completion_rates() -> WindowCompletionResult:
    """
    Check window completion rates for all scanner agents.

    Returns WindowCompletionResult with per-agent stats and overall TRS score.
    The overall TRS score is the minimum across all agents (weakest link).
    """
    agent_stats: List[AgentWindowStats] = []
    min_trs = 100.0
    worst_severity: Optional[str] = None

    for agent, db_path in AGENT_DBS.items():
        stats = _check_agent(agent, db_path)
        agent_stats.append(stats)
        min_trs = min(min_trs, stats.trs_score)

        if stats.alert_severity == "P0" and worst_severity != "P0":
            worst_severity = "P0"
        elif stats.alert_severity == "P1" and worst_severity is None:
            worst_severity = "P1"

    has_alert = worst_severity is not None

    return WindowCompletionResult(
        agent_stats=agent_stats,
        overall_trs_score=min_trs,
        has_alert=has_alert,
        worst_severity=worst_severity,
    )


def _check_agent(agent: str, db_path: str) -> AgentWindowStats:
    """Check window completion for a single agent."""
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify windows table exists
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='windows'"
        )
        if not cur.fetchone():
            conn.close()
            return AgentWindowStats(
                agent=agent, windows_checked=0, windows_completed=0,
                completion_rate=1.0, stale_open_count=0, trs_score=100.0,
            )

        # Fetch last N windows
        cur.execute(
            "SELECT window_id, tickers_analyzed, tickers_submitted, "
            "completed_at, received_at "
            "FROM windows ORDER BY rowid DESC LIMIT ?",
            (LOOKBACK_WINDOWS,)
        )
        rows = cur.fetchall()
        conn.close()

    except (sqlite3.Error, OSError) as e:
        logger.warning("Cannot read %s windows DB: %s", agent, e)
        return AgentWindowStats(
            agent=agent, windows_checked=0, windows_completed=0,
            completion_rate=1.0, stale_open_count=0, trs_score=100.0,
            alert_message=f"DB unavailable: {e}",
        )

    if len(rows) < MIN_WINDOWS_FOR_MONITORING:
        return AgentWindowStats(
            agent=agent,
            windows_checked=len(rows),
            windows_completed=len(rows),
            completion_rate=1.0,
            stale_open_count=0,
            trs_score=100.0,
        )

    now = time.time()
    completed_count = 0
    stale_open_count = 0
    window_ids = []

    for row in rows:
        window_ids.append(row["window_id"])
        analyzed = row["tickers_analyzed"] or 0
        completed_at = row["completed_at"]

        if analyzed > 0 and completed_at is not None:
            completed_count += 1
        elif analyzed == 0 and completed_at is None:
            # Check if this window is "stale" (old and never completed)
            received_at_str = row["received_at"]
            if received_at_str:
                received_age_min = _age_minutes(received_at_str)
                if received_age_min is not None and received_age_min > STALE_OPEN_THRESHOLD_MINUTES:
                    stale_open_count += 1

    total = len(rows)
    rate = completed_count / total

    # Compute TRS score and alert severity
    trs_score = 100.0
    alert_severity = None
    alert_message = ""

    if stale_open_count >= 2:
        # Two or more windows that never completed = service cycling
        trs_score = 25.0
        alert_severity = "P1"
        alert_message = (
            f"{agent}: {stale_open_count} windows received but never completed "
            f"(analyzed=0, no completion). Service is cycling during analysis windows."
        )
        logger.warning(alert_message)

    elif rate < MIN_COMPLETION_RATE_P0:
        trs_score = 0.0
        alert_severity = "P0"
        alert_message = (
            f"{agent}: window completion rate {rate:.0%} ({completed_count}/{total}) "
            f"is critically low. Service may be completely broken."
        )
        logger.error(alert_message)

    elif rate < MIN_COMPLETION_RATE_P1:
        trs_score = 50.0
        alert_severity = "P1"
        alert_message = (
            f"{agent}: window completion rate {rate:.0%} ({completed_count}/{total}) "
            f"below 75% threshold. Service likely cycling during analysis."
        )
        logger.warning(alert_message)

    return AgentWindowStats(
        agent=agent,
        windows_checked=total,
        windows_completed=completed_count,
        completion_rate=rate,
        stale_open_count=stale_open_count,
        recent_window_ids=window_ids,
        trs_score=trs_score,
        alert_severity=alert_severity,
        alert_message=alert_message,
    )


def _age_minutes(ts_str: str) -> Optional[float]:
    """Parse a datetime string and return age in minutes. Returns None on failure."""
    try:
        from datetime import datetime, timezone
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None
