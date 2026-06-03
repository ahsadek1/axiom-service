"""
ORACLE — Intelligence Layer: Cross-Ticker Pattern Detection
Uses Gemini to identify sector-level events and echo chamber risks across the full pool.
Runs ONCE per Axiom cycle after all Full Cards are assembled.
"""

import logging
from typing import Any, Dict, List, Optional

from clients import gemini_client
from models import CrossTickerPattern, CycleIntelligence

logger = logging.getLogger(__name__)


def _build_cycle_summary(packets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Extract key signals from all packets in the cycle for Gemini input.
    Keeps only the signals relevant for cross-ticker pattern detection.
    """
    summaries = []
    for p in packets:
        ticker = p.get("ticker", "UNKNOWN")
        flow = p.get("flow") or {}
        gamma = p.get("gamma") or {}
        vol = p.get("vol") or {}

        summaries.append({
            "ticker": ticker,
            "flow_bias": flow.get("net_flow_bias", "NEUTRAL"),
            "unusual_activity": flow.get("unusual_activity", False),
            "sweeps_count": len(flow.get("sweeps_today", [])),
            "hiro": gamma.get("hiro_signal"),
            "iv_rank": vol.get("iv_rank"),
        })
    return summaries


def detect(cycle_id: str, full_cards: List[Dict[str, Any]]) -> Optional[CycleIntelligence]:
    """
    Run cross-ticker pattern detection on all Full Cards in a cycle.

    Args:
        cycle_id:   Unique identifier for this Axiom cycle.
        full_cards: List of full context packet dicts for all pool tickers.

    Returns:
        CycleIntelligence with detected patterns and echo chamber risks.
        Returns empty CycleIntelligence (no patterns) on failure.
    """
    if not full_cards:
        return None

    summaries = _build_cycle_summary(full_cards)
    tickers = [p.get("ticker", "") for p in full_cards]

    raw = gemini_client.detect_patterns(cycle_id, summaries)

    if raw is None:
        logger.warning("Gemini pattern detection unavailable for cycle %s — using fallback",
                       cycle_id)
        return _fallback_patterns(cycle_id, summaries, tickers)

    try:
        from datetime import datetime, timezone
        patterns = [
            CrossTickerPattern(
                type=p.get("type", "UNKNOWN"),
                tickers_involved=p.get("tickers_involved", []),
                description=p.get("description", ""),
                implication=p.get("implication", ""),
            )
            for p in raw.get("patterns_detected", [])
        ]
        return CycleIntelligence(
            cycle_id=cycle_id,
            cycle_time=datetime.now(tz=timezone.utc).isoformat(),
            tickers_in_pool=tickers,
            patterns_detected=patterns,
            echo_chamber_risk_tickers=raw.get("echo_chamber_risk_tickers", []),
        )
    except (TypeError, ValueError, KeyError) as e:
        logger.error("Pattern result parse error for cycle %s: %s", cycle_id, e)
        return _fallback_patterns(cycle_id, summaries, tickers)


def _fallback_patterns(cycle_id: str, summaries: List[Dict],
                       tickers: List[str]) -> CycleIntelligence:
    """
    Rule-based pattern detection when Gemini is unavailable.
    Detects obvious sector flow events by counting bullish unusual activity.
    """
    from datetime import datetime, timezone

    patterns = []
    unusual_tickers = [s["ticker"] for s in summaries if s.get("unusual_activity")]

    if len(unusual_tickers) >= 3:
        patterns.append(CrossTickerPattern(
            type="SECTOR_FLOW_EVENT",
            tickers_involved=unusual_tickers,
            description=f"{len(unusual_tickers)} tickers showing unusual options activity in same cycle",
            implication="Possible sector-level institutional positioning event — verify signal independence",
        ))

    return CycleIntelligence(
        cycle_id=cycle_id,
        cycle_time=datetime.now(tz=timezone.utc).isoformat(),
        tickers_in_pool=tickers,
        patterns_detected=patterns,
        echo_chamber_risk_tickers=unusual_tickers if len(unusual_tickers) >= 3 else [],
    )
