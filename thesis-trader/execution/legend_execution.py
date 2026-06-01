"""
legend_execution.py — Legend-Specific Execution Parameters
===========================================================
Each legend influences HOW the trade is structured,
not just WHETHER to trade.

Druckenmiller: Macro-timed, larger size on high conviction
Jones:         Strict R:R, never enters without defined max loss
Soros:         Reflexivity — size up when narrative accelerating
Cohen:         Flow-based — only enters on institutional confirmation
Buffett:       Quality filter — sells premium only on fortress names

The composite of endorsing legends shapes:
  - Position size (Kelly multiplier adjustment)
  - Strike selection (more/less aggressive OTM)
  - DTE preference (shorter/longer)
  - Entry timing (immediate vs scaled)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("thesis.legend_execution")


@dataclass
class LegendParams:
    """Execution parameters from a single legend."""
    legend:          str
    size_multiplier: float = 1.0   # adjusts Kelly size
    delta_target:    float = 0.25  # short put delta target
    dte_preference:  int   = 37    # preferred DTE
    spread_width:    float = 5.0   # spread width preference
    min_rr:          float = 0.30  # minimum risk/reward ratio
    scale_in:        bool  = False # scale in vs full position
    rationale:       str   = ""


@dataclass
class CompositeParams:
    """Blended execution parameters from all endorsing legends."""
    endorsing_legends:  list[str]
    size_multiplier:    float
    delta_target:       float
    dte_preference:     int
    spread_width:       float
    min_rr:             float
    scale_in:           bool
    conviction_score:   float      # 0-100, drives final size
    rationale:          str


# ---------------------------------------------------------------------------
# Legend-specific parameter generators
# ---------------------------------------------------------------------------

def druckenmiller_params(
    regime: str,
    win_rate: float,
    composite_score: float,
) -> LegendParams:
    """
    Druckenmiller: Macro regime alignment is everything.
    Sizes up aggressively when macro tailwind is strong.
    Prefers slightly longer DTE for macro thesis to play out.
    """
    size_mult = 1.0
    if regime in ("LOW_VOL", "NORMAL") and win_rate >= 0.95:
        size_mult = 1.4  # strong macro + high win rate = larger size
    elif regime == "ELEVATED":
        size_mult = 0.7  # cautious in elevated vol
    elif regime in ("STRESS", "CRISIS"):
        size_mult = 0.3  # minimal in stress

    return LegendParams(
        legend          = "Druckenmiller",
        size_multiplier = size_mult,
        delta_target    = 0.22,   # slightly more conservative
        dte_preference  = 42,     # longer DTE for macro thesis
        spread_width    = 5.0,
        min_rr          = 0.25,
        scale_in        = composite_score < 75,  # scale in if not max conviction
        rationale       = f"Macro regime={regime} win_rate={win_rate:.0%} size={size_mult}x",
    )


def jones_params(
    win_rate: float,
    risk_reward: float,
    composite_score: float,
) -> LegendParams:
    """
    Jones: R:R is sacred. Never enters without defined max loss.
    Only trades when risk/reward is clearly favorable.
    Prefers tighter spreads with better-defined risk.
    """
    if risk_reward < 0.25:
        # R:R too low — Jones would not take this trade
        return LegendParams(
            legend          = "Jones",
            size_multiplier = 0.0,  # zero = Jones vetoes this trade
            rationale       = f"R:R {risk_reward:.2f} below Jones minimum 0.25",
        )

    size_mult = 1.0
    if risk_reward >= 0.40 and win_rate >= 0.90:
        size_mult = 1.3
    elif risk_reward >= 0.35:
        size_mult = 1.1
    elif risk_reward < 0.30:
        size_mult = 0.7

    return LegendParams(
        legend          = "Jones",
        size_multiplier = size_mult,
        delta_target    = 0.20,   # more OTM = better defined risk
        dte_preference  = 35,     # shorter DTE = faster resolution
        spread_width    = 5.0,
        min_rr          = 0.25,
        scale_in        = False,  # Jones enters full size or not at all
        rationale       = f"R:R={risk_reward:.2f} win_rate={win_rate:.0%} size={size_mult}x",
    )


def soros_params(
    composite_score: float,
    endorsement_count: int,
    regime: str,
) -> LegendParams:
    """
    Soros: Reflexivity — when a narrative is building, it accelerates.
    Sizes up when all legends agree (high concordance = narrative strength).
    """
    size_mult = 1.0
    if endorsement_count == 5 and composite_score >= 80:
        size_mult = 1.5   # 5/5 endorsements = maximum conviction
    elif endorsement_count >= 4 and composite_score >= 70:
        size_mult = 1.2   # strong consensus
    elif endorsement_count == 3:
        size_mult = 0.9   # minimum quorum

    # Soros reduces size in stress regimes
    if regime in ("STRESS", "HIGH_STRESS", "CRISIS"):
        size_mult *= 0.5

    return LegendParams(
        legend          = "Soros",
        size_multiplier = size_mult,
        delta_target    = 0.28,   # slightly more aggressive when high conviction
        dte_preference  = 38,
        spread_width    = 5.0,
        min_rr          = 0.28,
        scale_in        = endorsement_count < 5,
        rationale       = (
            f"Endorsements={endorsement_count}/5 score={composite_score:.0f} "
            f"regime={regime} size={size_mult}x"
        ),
    )


def cohen_params(
    win_rate: float,
    composite_score: float,
) -> LegendParams:
    """
    Cohen: Institutional flow is the signal.
    Scores highest on stocks with unusual volume and smart money accumulation.
    Sizes based on signal strength from Atlas (IV/options flow).
    """
    size_mult = 1.0
    if win_rate >= 0.99 and composite_score >= 80:
        size_mult = 1.35  # elite backtest + strong signal
    elif win_rate >= 0.95:
        size_mult = 1.15
    elif win_rate < 0.85:
        size_mult = 0.7   # below Cohen's threshold

    return LegendParams(
        legend          = "Cohen",
        size_multiplier = size_mult,
        delta_target    = 0.25,
        dte_preference  = 33,     # shorter DTE = tighter to the signal
        spread_width    = 5.0,
        min_rr          = 0.30,
        scale_in        = False,  # Cohen acts on the signal immediately
        rationale       = f"win_rate={win_rate:.0%} score={composite_score:.0f} size={size_mult}x",
    )


def buffett_params(
    ticker: str,
    win_rate: float,
) -> LegendParams:
    """
    Buffett: Quality first. Only sells premium on fundamentally strong names.
    More conservative sizing — Buffett does not chase.
    Prefers longer DTE to avoid noise.
    """
    # Buffett's preferred names — quality moat businesses
    BUFFETT_TIER_1 = {
        "AAPL","KO","AXP","BAC","MCO","BK","USB","WFC",
        "JNJ","PG","MMM","GE","CAT","DE","HON",
        "AMZN","GOOGL","MSFT","V","MA",
    }
    BUFFETT_TIER_2 = {
        "JPM","GS","CVX","XOM","UNH","LLY","ABBV",
        "BRK","MCD","SBUX","NKE","HD","LOW",
    }

    if ticker in BUFFETT_TIER_1:
        size_mult = 1.2
        delta    = 0.20  # very conservative strike
    elif ticker in BUFFETT_TIER_2:
        size_mult = 1.0
        delta    = 0.22
    else:
        size_mult = 0.6  # Buffett doesn't chase unknown names
        delta    = 0.18

    return LegendParams(
        legend          = "Buffett",
        size_multiplier = size_mult,
        delta_target    = delta,
        dte_preference  = 45,    # longer DTE = more patient
        spread_width    = 5.0,
        min_rr          = 0.25,
        scale_in        = True,  # Buffett scales in
        rationale       = (
            f"Quality={'T1' if ticker in BUFFETT_TIER_1 else 'T2' if ticker in BUFFETT_TIER_2 else 'unknown'} "
            f"win_rate={win_rate:.0%} size={size_mult}x"
        ),
    )


# ---------------------------------------------------------------------------
# Composite parameter blender
# ---------------------------------------------------------------------------

def compute_composite_params(
    ticker:            str,
    endorsing_legends: list[str],
    composite_score:   float,
    win_rate:          float,
    regime:            str,
    risk_reward:       float = 0.33,
) -> CompositeParams:
    """
    Blend parameters from all endorsing legends.
    Jones veto is absolute — if Jones endorses but R:R fails, trade is blocked.
    All other parameters are weighted averages.
    """
    legend_params = []

    for legend in endorsing_legends:
        if legend == "Druckenmiller":
            legend_params.append(
                druckenmiller_params(regime, win_rate, composite_score)
            )
        elif legend == "Jones":
            p = jones_params(win_rate, risk_reward, composite_score)
            if p.size_multiplier == 0.0:
                # Jones veto — block trade entirely
                log.warning("%s: Jones VETO — R:R %.2f below minimum", ticker, risk_reward)
                return CompositeParams(
                    endorsing_legends = endorsing_legends,
                    size_multiplier   = 0.0,
                    delta_target      = 0.25,
                    dte_preference    = 37,
                    spread_width      = 5.0,
                    min_rr            = 0.25,
                    scale_in          = False,
                    conviction_score  = 0.0,
                    rationale         = f"Jones VETO: R:R {risk_reward:.2f} too low",
                )
            legend_params.append(p)
        elif legend == "Soros":
            legend_params.append(
                soros_params(composite_score, len(endorsing_legends), regime)
            )
        elif legend == "Cohen":
            legend_params.append(cohen_params(win_rate, composite_score))
        elif legend == "Buffett":
            legend_params.append(buffett_params(ticker, win_rate))

    if not legend_params:
        return CompositeParams(
            endorsing_legends = endorsing_legends,
            size_multiplier   = 1.0,
            delta_target      = 0.25,
            dte_preference    = 37,
            spread_width      = 5.0,
            min_rr            = 0.25,
            scale_in          = False,
            conviction_score  = composite_score,
            rationale         = "Default params (no legend data)",
        )

    # Weighted average of all legend params
    n = len(legend_params)
    avg_size  = sum(p.size_multiplier for p in legend_params) / n
    avg_delta = sum(p.delta_target    for p in legend_params) / n
    avg_dte   = int(sum(p.dte_preference for p in legend_params) / n)
    any_scale = any(p.scale_in for p in legend_params)
    rationale = " | ".join(p.rationale for p in legend_params)

    # Conviction score drives final size adjustment
    conviction = composite_score * (avg_size / 1.0)

    log.info(
        "%s composite params: size=%.2fx delta=%.2f dte=%d "
        "conviction=%.0f legends=%s",
        ticker, avg_size, avg_delta, avg_dte,
        conviction, ",".join(endorsing_legends)
    )

    return CompositeParams(
        endorsing_legends = endorsing_legends,
        size_multiplier   = round(avg_size, 2),
        delta_target      = round(avg_delta, 3),
        dte_preference    = avg_dte,
        spread_width      = 5.0,
        min_rr            = max(p.min_rr for p in legend_params),
        scale_in          = any_scale,
        conviction_score  = round(conviction, 1),
        rationale         = rationale,
    )
