"""
composite.py — Composite health score aggregator for nexus-integrity.

Computes the Trading Readiness Score (TRS) from all component scores.
Severity (P0-P4) is derived from the score and which sub-components failed —
no manual classification required (Vector V9 amendment).

Score bands:
  GREEN: 90-100  → size multiplier 1.0 (full capacity)
  AMBER: 60-89   → size multiplier 0.5 (cap size 50%)
  RED:   30-59   → size multiplier 0.0 (halt new entries)
  BLACK: 0-29    → size multiplier 0.0 (halt everything + emergency alert)
"""

import logging
import time
from typing import Dict, List, Optional

import config
from models import AlertTier, ComponentScore, CompositeColor, TRSResult

logger = logging.getLogger("integrity.composite")

# ---------------------------------------------------------------------------
# Runtime threshold cache (refreshed from CHRONICLE)
# ---------------------------------------------------------------------------

_threshold_cache: Dict[str, float] = dict(config.DEFAULT_THRESHOLDS)
_threshold_cache_time: float = 0.0
_THRESHOLD_REFRESH_INTERVAL_S: int = 300  # 5 minutes


def _get_thresholds() -> Dict[str, float]:
    """Get current thresholds, refreshing from CHRONICLE if cache is stale.

    Falls back to DEFAULT_THRESHOLDS if CHRONICLE is unreachable.

    Returns:
        Dict of threshold key → value.
    """
    global _threshold_cache, _threshold_cache_time

    now = time.time()
    if now - _threshold_cache_time < _THRESHOLD_REFRESH_INTERVAL_S:
        return _threshold_cache

    try:
        import requests as _requests
        resp = _requests.get(
            f"{config.CHRONICLE_URL}/governance/thresholds",
            headers={"X-Chronicle-Secret": config.CHRONICLE_SECRET},
            timeout=3.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                _threshold_cache = {**config.DEFAULT_THRESHOLDS, **data}
                _threshold_cache_time = now
                logger.debug("Thresholds refreshed from CHRONICLE")
    except Exception as e:  # noqa: BLE001
        logger.warning("CHRONICLE threshold refresh failed (using cache): %s", e)

    return _threshold_cache


def _score_to_color(score: float, thresholds: Dict[str, float]) -> CompositeColor:
    """Map a composite score to a color band.

    Args:
        score: Composite score 0-100.
        thresholds: Current threshold dict.

    Returns:
        CompositeColor enum value.
    """
    green_min = thresholds.get("GREEN_MIN", 90.0)
    amber_min = thresholds.get("AMBER_MIN", 60.0)
    red_min = thresholds.get("RED_MIN", 30.0)

    if score >= green_min:
        return CompositeColor.GREEN
    elif score >= amber_min:
        return CompositeColor.AMBER
    elif score >= red_min:
        return CompositeColor.RED
    else:
        return CompositeColor.BLACK


def _derive_alert_tier(
    score: float,
    color: CompositeColor,
    component_scores: Dict[str, float],
    block: bool,
) -> AlertTier:
    """Derive alert tier from score and component failures (V9 amendment).

    P0-P4 are determined by TRS score and which sub-components failed,
    with no manual classification required.

    Args:
        score: Composite score 0-100.
        color: CompositeColor band.
        component_scores: Dict of component name → score (0-100 each).
        block: Whether trading is blocked.

    Returns:
        AlertTier enum value.
    """
    # P0: score=0 or block=True (TRS dead or hard-blocked)
    if block or score == 0:
        return AlertTier.P0

    # P1: score < GREEN threshold (trading degraded)
    if color in (CompositeColor.AMBER, CompositeColor.RED, CompositeColor.BLACK):
        return AlertTier.P1

    # P2: one component failed but overall score is GREEN (single probe failure)
    failed_components = [k for k, v in component_scores.items() if v < 50.0]
    if failed_components:
        return AlertTier.P2

    # P3: score is GREEN, minor anomalies only
    return AlertTier.P3


def compute_composite_score(
    service_availability: float,
    canary_success_rate: float,
    config_correctness: float,
    error_rate_score: float,
    pipeline_throughput: float,
    data_freshness: float,
    session_activity: float,
) -> TRSResult:
    """Compute the composite TRS score from all component inputs.

    Each input is a 0-100 score for that component. Weights are applied
    as defined in config.COMPONENT_WEIGHTS.

    Args:
        service_availability: Score for Layer 1 presence checks (0-100).
        canary_success_rate: Score based on last 5 canary results (0-100).
        config_correctness: Score for /limits vs expected values (0-100).
        error_rate_score: Score based on HTTP 4xx/5xx rate (0-100, inverted).
        pipeline_throughput: Score for submission→verdict→execution ratio (0-100).
        data_freshness: Score for VIX age, agent scan age, Axiom pool age (0-100).
        session_activity: Score for agent session activity (0-100).

    Returns:
        TRSResult with composite score, color, alert tier, and component breakdown.
    """
    thresholds = _get_thresholds()
    weights = config.COMPONENT_WEIGHTS

    raw_inputs = {
        "service_availability": service_availability,
        "canary_success_rate": canary_success_rate,
        "config_correctness": config_correctness,
        "error_rate": error_rate_score,
        "pipeline_throughput": pipeline_throughput,
        "data_freshness": data_freshness,
        "session_activity": session_activity,
    }

    # Clamp all inputs to [0, 100]
    clamped = {k: max(0.0, min(100.0, v)) for k, v in raw_inputs.items()}

    # Compute weighted sum
    total_weight = sum(weights.values())
    weighted_sum = sum(
        clamped[k] * weights.get(k, 0.0)
        for k in clamped
    )
    composite = weighted_sum / total_weight if total_weight > 0 else 0.0
    composite = max(0.0, min(100.0, composite))

    color = _score_to_color(composite, thresholds)
    block = color in (CompositeColor.RED, CompositeColor.BLACK)

    alert_tier = _derive_alert_tier(composite, color, clamped, block)

    # Build component scores dict for TRS storage
    component_scores = {k: round(v, 2) for k, v in clamped.items()}

    reason = _build_reason(clamped, color)

    return TRSResult(
        score=round(composite, 2),
        block=block,
        reason=reason,
        component_scores=component_scores,
        alert_tier=alert_tier,
    )


def _build_reason(component_scores: Dict[str, float], color: CompositeColor) -> str:
    """Build a human-readable reason string from component scores.

    Args:
        component_scores: Dict of component name → score.
        color: CompositeColor band.

    Returns:
        Reason string.
    """
    if color == CompositeColor.GREEN:
        return "All systems nominal"

    failed = [
        f"{k}={v:.0f}"
        for k, v in sorted(component_scores.items(), key=lambda x: x[1])
        if v < 70.0
    ]
    if failed:
        return f"{color.value}: degraded components: {', '.join(failed[:3])}"
    return f"{color.value}: composite below threshold"


def score_service_availability(healthy_count: int, total_count: int) -> float:
    """Compute service availability score from health check results.

    Args:
        healthy_count: Number of services returning healthy.
        total_count: Total number of services monitored.

    Returns:
        Score 0-100.
    """
    if total_count == 0:
        return 0.0
    return (healthy_count / total_count) * 100.0


def score_canary_success_rate(recent_canary_results: List[bool]) -> float:
    """Compute canary success rate score from last N canary results.

    Args:
        recent_canary_results: List of bool (True=pass) for last N canaries.

    Returns:
        Score 0-100.
    """
    if not recent_canary_results:
        # GENESIS-FIX-COMPOSITE-001 2026-04-30: Empty canary list (post-restart) was
        # returning 50.0, causing TRS=AMBER immediately after every nexus-integrity
        # restart until the first scheduled canary run. No data != failure.
        # Default to 100.0 (healthy) until first canary result arrives.
        return 100.0  # No data yet — assume healthy until first canary run proves otherwise

    passed = sum(1 for r in recent_canary_results if r)
    return (passed / len(recent_canary_results)) * 100.0


def score_error_rate(error_count: int, total_requests: int) -> float:
    """Compute error rate score (inverted — higher error rate = lower score).

    Args:
        error_count: Number of 4xx/5xx responses in window.
        total_requests: Total requests in window.

    Returns:
        Score 0-100 (100 = no errors).
    """
    if total_requests == 0:
        return 100.0  # No data — assume healthy

    error_rate = error_count / total_requests
    # 0% errors → 100, 10% errors → 50, 20%+ errors → 0
    score = max(0.0, 100.0 - (error_rate * 500.0))
    return min(100.0, score)


def score_data_freshness(
    vix_age_s: Optional[float],
    last_agent_scan_age_s: Optional[float],
    last_pool_age_s: Optional[float],
) -> float:
    """Compute data freshness score from component ages.

    Args:
        vix_age_s: Age of VIX data in seconds (None if unavailable).
        last_agent_scan_age_s: Age of last agent scan in seconds.
        last_pool_age_s: Age of last Axiom pool in seconds.

    Returns:
        Score 0-100.
    """
    scores = []

    # VIX: should be < 5 min during market hours
    if vix_age_s is not None:
        vix_score = max(0.0, 100.0 - (vix_age_s / 300.0) * 100.0)
        scores.append(vix_score)

    # Agent scan: should be < 20 min
    if last_agent_scan_age_s is not None:
        scan_score = max(0.0, 100.0 - (last_agent_scan_age_s / 1200.0) * 100.0)
        scores.append(scan_score)

    # Pool: should be < 16 min
    if last_pool_age_s is not None:
        pool_score = max(0.0, 100.0 - (last_pool_age_s / 960.0) * 100.0)
        scores.append(pool_score)

    if not scores:
        return 50.0  # No data — neutral

    return sum(scores) / len(scores)
