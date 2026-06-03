"""
ORACLE — Intelligence Layer: Signal Coherence
Uses DeepSeek to assess whether signals in a context packet are aligned or conflicted.
Runs per-ticker AFTER all engines complete.
Never states directional opinions — only assesses signal alignment.
"""

import logging
from typing import Any, Dict, Optional

from clients import deepseek_client
from models import CoherenceFlag, CoherenceResult

logger = logging.getLogger(__name__)


def _build_summary(ticker: str, packet: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract key signals from a full context packet for DeepSeek input.
    Strips raw data to only the signals relevant for coherence assessment.
    """
    summary: Dict[str, Any] = {"ticker": ticker, "signals": {}}

    vol = packet.get("vol") or {}
    summary["signals"]["iv_rank"] = vol.get("iv_rank")
    summary["signals"]["iv_hv_spread"] = vol.get("iv_hv_spread")

    flow = packet.get("flow") or {}
    summary["signals"]["net_flow_bias"] = flow.get("net_flow_bias")
    summary["signals"]["put_call_ratio"] = flow.get("put_call_ratio")
    summary["signals"]["unusual_activity"] = flow.get("unusual_activity")

    gamma = packet.get("gamma") or {}
    summary["signals"]["hiro_signal"] = gamma.get("hiro_signal")
    summary["signals"]["net_gex"] = gamma.get("net_gex")

    macro = packet.get("macro") or {}
    summary["signals"]["regime"] = macro.get("regime")
    summary["signals"]["strategy_bias"] = macro.get("strategy_bias")

    fundamental = packet.get("fundamental") or {}
    summary["signals"]["insider_net_bias"] = fundamental.get("insider_net_bias")
    summary["signals"]["insider_cluster"] = fundamental.get("insider_cluster_flag")
    summary["signals"]["days_to_earnings"] = fundamental.get("days_to_earnings")

    return summary


def score(ticker: str, context_packet: Dict[str, Any]) -> Optional[CoherenceResult]:
    """
    Score signal coherence for an assembled context packet.

    Args:
        ticker:         Ticker symbol.
        context_packet: Full assembled packet dict.

    Returns:
        CoherenceResult with score, level, flags. None on failure.
    """
    summary = _build_summary(ticker, context_packet)

    raw = deepseek_client.get_coherence(ticker, summary)
    if raw is None:
        logger.warning("DeepSeek coherence unavailable for %s — using fallback", ticker)
        return _fallback_coherence(summary)

    try:
        flags = [
            CoherenceFlag(
                type=f.get("type", "UNKNOWN"),
                description=f.get("description", ""),
                severity=f.get("severity", "INFO"),
            )
            for f in raw.get("flags", [])
        ]
        return CoherenceResult(
            coherence_score=int(raw.get("coherence_score", 50)),
            coherence_level=raw.get("coherence_level", "MEDIUM"),
            flags=flags,
            aligned_signals=raw.get("aligned_signals", []),
            conflicting_signals=raw.get("conflicting_signals", []),
        )
    except (TypeError, ValueError) as e:
        logger.error("Coherence result parse error for %s: %s", ticker, e)
        return _fallback_coherence(summary)


def _fallback_coherence(summary: Dict[str, Any]) -> CoherenceResult:
    """
    Rule-based coherence when DeepSeek is unavailable.
    Simple heuristic: count signal directions and flag obvious conflicts.
    """
    signals = summary.get("signals", {})
    score_val = 50  # neutral default
    flags = []

    flow_bias = signals.get("net_flow_bias", "NEUTRAL")
    hiro = signals.get("hiro_signal")
    regime = signals.get("regime", "UNKNOWN")

    if flow_bias == "BULLISH" and hiro is not None and hiro < -0.2:
        flags.append(CoherenceFlag(
            type="FLOW_GAMMA_CONFLICT",
            description="Bullish options flow but negative dealer hedging (HIRO)",
            severity="WARNING",
        ))
        score_val -= 20

    if regime in ("CRISIS", "HIGH_STRESS") and flow_bias == "BULLISH":
        flags.append(CoherenceFlag(
            type="MACRO_FLOW_CONFLICT",
            description="Bullish flow signal in crisis/high-stress macro regime",
            severity="WARNING",
        ))
        score_val -= 15

    level = "HIGH" if score_val >= 75 else "MEDIUM" if score_val >= 50 else "LOW"
    if score_val < 40:
        level = "CONFLICTED"

    return CoherenceResult(
        coherence_score=max(0, score_val),
        coherence_level=level,
        flags=flags,
        aligned_signals=[],
        conflicting_signals=[],
    )
