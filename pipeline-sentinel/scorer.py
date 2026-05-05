"""
scorer.py — Health score computation engine for Pipeline Sentinel.

Computes a 0–100 composite system health score from pipeline trace data.
Score is stored to DB and cached in app.state for fast retrieval.

Deduction table (score starts at 100):
  - Pipeline completion rate (trailing 20 picks):  0 pts at 100%, -30 pts at 60%
  - Inter-service P95 latency (hop timestamps):    0 pts at 500ms, -20 pts at 3000ms
  - OMNI brain P95 latency (started→completed):    0 pts at 2000ms, -15 pts at 8000ms
  - Active stalled picks:                          -5 per stall, max -15
  - Active failure classes:                        -5 per class, max -15
  - Guardian Angel anomaly flags:                  -5 if any in last 5 min

Health bands:
  85–100  NOMINAL   multiplier=1.0  submissions=open    no alert
  70–84   DEGRADED  multiplier=0.95 submissions=open    no alert
  55–69   IMPAIRED  multiplier=0.85 submissions=open    alert Ahmed
  30–54   CRITICAL  multiplier=0.0  submissions=halted  alert Ahmed (immediate)
  0–29    EMERGENCY multiplier=0.0  submissions=halted  alert Ahmed (immediate)
"""

import logging
import os
import time
from datetime import datetime, timezone
from statistics import mean, quantiles
from typing import Any, Dict, List, Optional

from database import (
    get_active_failures,
    get_recent_anomalies,
    get_recent_traces,
    get_stalled_picks,
    insert_health_score,
)
from models import HealthScoreComponents, SystemHealthResponse
from notifier import TelegramNotifier

logger = logging.getLogger("sentinel.scorer")

_HALT_THRESHOLD   = int(os.environ.get("HEALTH_HALT_THRESHOLD",   "55"))
_IMPAIR_THRESHOLD = int(os.environ.get("HEALTH_IMPAIR_THRESHOLD", "70"))


def _p95(values: List[float]) -> float:
    """
    Compute P95 of a list of float values.

    Args:
        values: Non-empty list of floats.

    Returns:
        P95 value. Returns 0.0 for empty or single-element lists.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    # quantiles needs at least 2 data points; n=20 gives us 5th percentile increments
    n = min(len(values), 20)
    qs = quantiles(values, n=n)
    # Index 18 out of 20 = 95th percentile (0-indexed: qs[18] = 95th)
    idx = min(int(0.95 * n) - 1, len(qs) - 1)
    return qs[max(idx, 0)]


def _linear_deduction(value: float, zero_at: float, max_deduction_at: float, max_deduction: float) -> float:
    """
    Compute a linear deduction based on a metric value.

    Args:
        value:             The current metric value.
        zero_at:           Value at which deduction is 0 (good threshold).
        max_deduction_at:  Value at which full deduction is applied.
        max_deduction:     Maximum points to deduct.

    Returns:
        Deduction amount (0 to max_deduction).
    """
    if value <= zero_at:
        return 0.0
    if value >= max_deduction_at:
        return max_deduction
    fraction = (value - zero_at) / (max_deduction_at - zero_at)
    return round(fraction * max_deduction, 2)


def _compute_completion_rate(traces: List[Dict[str, Any]], window: int = 20) -> float:
    """
    Compute the fraction of the last `window` unique picks that reached alpaca_confirmed.

    Args:
        traces: Recent trace hops from DB.
        window: How many recent unique picks to consider.

    Returns:
        Completion rate 0.0–1.0. Returns 1.0 if no picks exist yet (no penalty for empty system).
    """
    # Group by trace_id, find which ones completed
    seen: Dict[str, bool] = {}
    for row in traces:
        tid = row["trace_id"]
        if tid not in seen:
            seen[tid] = False
        if row["hop"] == "alpaca_confirmed":
            seen[tid] = True

    # Take the most recent `window` picks (ordering by first-seen would be ideal;
    # since we process in ts order, dict insertion order gives us roughly that)
    recent = list(seen.items())[-window:]
    if not recent:
        return 1.0
    completed = sum(1 for _, done in recent if done)
    return completed / len(recent)


def _compute_inter_service_p95(traces: List[Dict[str, Any]]) -> float:
    """
    Compute P95 latency between consecutive hops within each pick.

    Args:
        traces: Recent trace hops ordered by ts.

    Returns:
        P95 inter-hop latency in milliseconds.
    """
    # Build per-trace hop sequences
    by_trace: Dict[str, List[float]] = {}
    for row in traces:
        tid = row["trace_id"]
        if tid not in by_trace:
            by_trace[tid] = []
        by_trace[tid].append(row["ts"])

    gaps: List[float] = []
    for hops in by_trace.values():
        hops.sort()
        for i in range(1, len(hops)):
            gap_ms = (hops[i] - hops[i - 1]) * 1000
            gaps.append(gap_ms)

    return _p95(gaps)


def _compute_omni_brain_p95(traces: List[Dict[str, Any]]) -> float:
    """
    Compute P95 OMNI synthesis latency (omni_started → omni_completed).

    Args:
        traces: Recent trace hops.

    Returns:
        P95 OMNI latency in milliseconds.
    """
    starts: Dict[str, float] = {}
    latencies: List[float] = []

    for row in traces:
        tid = row["trace_id"]
        if row["hop"] == "omni_started":
            starts[tid] = row["ts"]
        elif row["hop"] == "omni_completed" and tid in starts:
            latency_ms = (row["ts"] - starts[tid]) * 1000
            latencies.append(latency_ms)
            del starts[tid]

    return _p95(latencies)


def _band_from_score(score: float) -> tuple:
    """
    Return (status, size_multiplier, submissions_open) for a given score.

    Args:
        score: Computed health score 0–100.

    Returns:
        Tuple of (status_str, multiplier_float, submissions_open_bool).
    """
    if score >= 85:
        return ("NOMINAL",   1.0,  True)
    if score >= 70:
        return ("DEGRADED",  0.95, True)
    if score >= 55:
        return ("IMPAIRED",  0.85, True)
    if score >= 30:
        return ("CRITICAL",  0.0,  False)
    return ("EMERGENCY", 0.0,  False)


class HealthScorer:
    """Computes, stores, and returns system health scores."""

    def __init__(self, db_path: str, stall_window_s: int, notifier: TelegramNotifier) -> None:
        """
        Initialize the scorer.

        Args:
            db_path:       Path to the SQLite database.
            stall_window_s: Seconds without activity before a pick is stalled.
            notifier:      TelegramNotifier instance for alerts.
        """
        self._db_path       = db_path
        self._stall_window  = stall_window_s
        self._notifier      = notifier
        self._last_status:  Optional[str] = None   # track band changes for alerts

    def compute_score(self) -> SystemHealthResponse:
        """
        Compute the current system health score from DB data.

        Reads recent traces, active failures, stalled picks, and anomaly reports.
        Stores the result to DB. Sends alerts if band degrades below IMPAIRED.

        Returns:
            A fully populated SystemHealthResponse.
        """
        try:
            traces         = get_recent_traces(self._db_path, window_s=900)
            active_failures = get_active_failures(self._db_path)
            stalled        = get_stalled_picks(self._db_path, self._stall_window)
            anomalies      = get_recent_anomalies(self._db_path, window_s=300)

            # --- Component metrics ---
            completion_rate     = _compute_completion_rate(traces)
            latency_p95_ms      = _compute_inter_service_p95(traces)
            omni_p95_ms         = _compute_omni_brain_p95(traces)
            stall_count         = len(stalled)
            failure_count       = len(active_failures)
            anomaly_flag        = 1 if anomalies else 0

            # --- Deductions ---
            completion_deduction = _linear_deduction(
                1.0 - completion_rate,   # invert: higher is worse
                0.0, 0.40, 30.0,         # 0% miss → 0 pts, 40% miss → 30 pts
            )
            latency_deduction = _linear_deduction(latency_p95_ms,  500.0,  3000.0, 20.0)
            omni_deduction    = _linear_deduction(omni_p95_ms,     2000.0, 8000.0, 15.0)
            stall_deduction   = min(stall_count * 5.0,   15.0)
            failure_deduction = min(failure_count * 5.0, 15.0)
            anomaly_deduction = 5.0 if anomaly_flag else 0.0

            raw_score = (
                100.0
                - completion_deduction
                - latency_deduction
                - omni_deduction
                - stall_deduction
                - failure_deduction
                - anomaly_deduction
            )
            score = max(0.0, min(100.0, round(raw_score, 1)))

            status, multiplier, submissions_open = _band_from_score(score)

            components = HealthScoreComponents(
                pipeline_completion_rate     = round(completion_rate, 4),
                inter_service_latency_p95_ms = round(latency_p95_ms, 1),
                omni_brain_latency_p95_ms    = round(omni_p95_ms, 1),
                oracle_cache_freshness       = 1.0,   # reserved for future ORACLE integration
                active_anomaly_count         = anomaly_flag,
                stalled_picks_count          = stall_count,
            )

            context_block: Dict[str, Any] = {
                "health_score":               score,
                "executor_latency_p95_ms":    round(latency_p95_ms, 1),
                "pipeline_completion_rate":   round(completion_rate, 4),
                "active_anomalies":           active_failures,
                "recommended_size_multiplier": multiplier,
            }

            response = SystemHealthResponse(
                health_score               = score,
                score_components           = components,
                status                     = status,
                recommended_size_multiplier = multiplier,
                active_failures            = active_failures,
                context_block              = context_block,
                submissions_open           = submissions_open,
                computed_at                = datetime.now(tz=timezone.utc).isoformat(),
                stale                      = False,
            )

            # Persist to DB
            insert_health_score(self._db_path, response)

            # Alert on status change into bad bands
            if status != self._last_status:
                if status in ("IMPAIRED", "CRITICAL", "EMERGENCY"):
                    emoji = "🚨" if status in ("CRITICAL", "EMERGENCY") else "⚠️"
                    self._notifier.send_alert(
                        f"{emoji} *Pipeline Sentinel — {status}*\n"
                        f"Health score: {score}/100\n"
                        f"Submissions: {'HALTED' if not submissions_open else 'open (penalized)'}\n"
                        f"Active failures: {', '.join(active_failures) or 'none'}\n"
                        f"Stalled picks: {stall_count}",
                        failure_class=f"HEALTH_{status}",
                    )
                self._last_status = status

            logger.info(
                "Score computed: %.1f (%s) | completion=%.1f%% | latency_p95=%.0fms | stalls=%d | failures=%d",
                score, status, completion_rate * 100, latency_p95_ms, stall_count, failure_count,
            )
            return response

        except Exception as exc:
            logger.error("compute_score failed: %s", exc)
            # Return a stale/safe default rather than crashing
            return SystemHealthResponse(
                health_score               = 100.0,
                score_components           = HealthScoreComponents(
                    pipeline_completion_rate     = 1.0,
                    inter_service_latency_p95_ms = 0.0,
                    omni_brain_latency_p95_ms    = 0.0,
                    oracle_cache_freshness       = 1.0,
                    active_anomaly_count         = 0,
                    stalled_picks_count          = 0,
                ),
                status                     = "NOMINAL",
                recommended_size_multiplier = 1.0,
                active_failures            = [],
                context_block              = {
                    "health_score": 100.0,
                    "executor_latency_p95_ms": 0.0,
                    "pipeline_completion_rate": 1.0,
                    "active_anomalies": [],
                    "recommended_size_multiplier": 1.0,
                },
                submissions_open           = True,
                computed_at                = datetime.now(tz=timezone.utc).isoformat(),
                stale                      = True,
            )
