"""
OMNI — Market Participant Psychology Overlay.

Final synthesis step applied AFTER compute_verdict().  Reads the psychological
state embedded in options positioning, volume patterns, earnings reactions, and
institutional footprints — and translates that into a confidence/sizing adjustment.

Key design constraints (spec-exact):
  - Psychology overlay NEVER changes the verdict string (STRONG_GO / GO / NO_GO / etc.)
  - Psychology overlay NEVER blocks execution
  - Only adjusts sizing_mult (and appends to notes)
  - Total adjustment capped at [-30, +30]
  - All fields gracefully degrade to "neutral" if oracle_context data is missing

Adds ~50-100ms latency to synthesis (acceptable — not in ms-critical path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from synthesis import SynthesisVerdict

logger = logging.getLogger("omni.psychology_overlay")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SkewReading:
    """Options skew interpretation as a fear/confidence detector."""
    reading: str          # informed_fear | herding_fear | genuine_confidence | dangerous_complacency | neutral
    omni_adjustment: int
    evidence: str = ""

    def to_dict(self) -> dict:
        """Serialise to dict for logging and storage."""
        return {
            "reading": self.reading,
            "omni_adjustment": self.omni_adjustment,
            "evidence": self.evidence,
        }


@dataclass
class VolumePattern:
    """Volume pattern as a behavioural signal."""
    pattern: str          # accumulation | distribution | panic | euphoria | neutral
    omni_adjustment: int
    evidence: str = ""

    def to_dict(self) -> dict:
        """Serialise to dict for logging and storage."""
        return {
            "pattern": self.pattern,
            "omni_adjustment": self.omni_adjustment,
            "evidence": self.evidence,
        }


@dataclass
class EarningsReaction:
    """Earnings reaction as a sentiment decoder."""
    reading: Optional[str]   # beat_rally | beat_selloff | miss_selloff | miss_rally | inline_large_move | None
    omni_adjustment: int
    evidence: str = ""

    def to_dict(self) -> dict:
        """Serialise to dict for logging and storage."""
        return {
            "reading": self.reading,
            "omni_adjustment": self.omni_adjustment,
            "evidence": self.evidence,
        }


@dataclass
class InstitutionalFootprint:
    """Dark pool and institutional options footprint signal."""
    signal: str           # dark_pool_accumulation | dark_pool_distribution | bullish_sweep | bearish_sweep | neutral
    omni_adjustment: int
    evidence: str = ""

    def to_dict(self) -> dict:
        """Serialise to dict for logging and storage."""
        return {
            "signal": self.signal,
            "omni_adjustment": self.omni_adjustment,
            "evidence": self.evidence,
        }


@dataclass
class PsychologyOverlayResult:
    """Complete psychology overlay result for one synthesis cycle."""
    skew_reading: SkewReading
    volume_pattern: VolumePattern
    earnings_reaction: EarningsReaction
    institutional_footprint: InstitutionalFootprint
    total_adjustment: int      # sum of all 4, capped at [-30, +30]
    psychology_summary: str
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise full overlay result for CHRONICLE log and Telegram card."""
        return {
            "skew_reading": self.skew_reading.to_dict(),
            "volume_pattern": self.volume_pattern.to_dict(),
            "earnings_reaction": self.earnings_reaction.to_dict(),
            "institutional_footprint": self.institutional_footprint.to_dict(),
            "total_adjustment": self.total_adjustment,
            "psychology_summary": self.psychology_summary,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Adjustment tables (spec-exact)
# ---------------------------------------------------------------------------

_SKEW_ADJUSTMENTS: dict[str, int] = {
    "informed_fear":          -10,
    "herding_fear":           +10,
    "genuine_confidence":     +10,
    "dangerous_complacency":  -15,
    "neutral":                  0,
}

_VOLUME_ADJUSTMENTS: dict[str, int] = {
    "accumulation":  +10,
    "distribution":  -15,
    "panic":         +15,   # contrarian: fear may be exhausted
    "euphoria":      -20,   # Buffett "others are greedy" signal
    "neutral":         0,
}

_EARNINGS_ADJUSTMENTS: dict[str, int] = {
    "beat_rally":        +10,
    "beat_selloff":      -15,
    "miss_selloff":      -20,
    "miss_rally":        +10,
    "inline_large_move":   0,  # investigate before taking directional view
}

_INSTITUTIONAL_ADJUSTMENTS: dict[str, int] = {
    "dark_pool_accumulation":  +15,
    "dark_pool_distribution":  -20,
    "bullish_sweep":           +20,
    "bearish_sweep":           -20,
    "neutral":                   0,
}

_CAP_MAX = 30
_CAP_MIN = -30

# Sizing delta per point of psychology adjustment: +30 adj → +0.30 sizing
_SIZING_SCALE = 0.01
_SIZING_MIN = 0.10
_SIZING_MAX = 2.00


# ---------------------------------------------------------------------------
# Signal interpretation helpers
# ---------------------------------------------------------------------------

def _read_skew(oracle_context: Optional[dict]) -> SkewReading:
    """Interpret options skew as a fear/confidence signal.

    Maps put_call_skew and net_flow_bias from oracle_context to a skew reading.
    Defaults to neutral on missing/None data.

    Args:
        oracle_context: Full ORACLE intelligence packet or None.

    Returns:
        SkewReading with reading label and omni_adjustment.
    """
    if not oracle_context:
        return SkewReading("neutral", 0, "oracle_context unavailable")

    vol = oracle_context.get("vol") or {}
    flow = oracle_context.get("flow") or {}

    put_call_skew: Optional[float] = vol.get("put_call_skew")
    net_flow_bias: Optional[str] = flow.get("net_flow_bias")  # "bullish"|"bearish"|"neutral"|None

    if put_call_skew is None:
        return SkewReading("neutral", 0, "put_call_skew unavailable")

    # Skew > 1.5: elevated put skew — determine if informed or herding
    if put_call_skew > 1.5:
        # Herding signal: flow is also strongly bearish (retail piling in)
        if net_flow_bias == "bearish":
            return SkewReading(
                "herding_fear", _SKEW_ADJUSTMENTS["herding_fear"],
                f"skew={put_call_skew:.2f} + bearish flow bias → herding, contrarian opportunity"
            )
        # Informed fear: skew elevated without the usual retail panic flow
        return SkewReading(
            "informed_fear", _SKEW_ADJUSTMENTS["informed_fear"],
            f"skew={put_call_skew:.2f}, flow neutral → institutional hedging, respect positioning"
        )

    # Skew < 0.7: compressed — genuine confidence or dangerous complacency
    if put_call_skew < 0.7:
        if net_flow_bias == "bullish":
            return SkewReading(
                "genuine_confidence", _SKEW_ADJUSTMENTS["genuine_confidence"],
                f"skew={put_call_skew:.2f} + bullish flow → genuine confidence, premium selling favourable"
            )
        return SkewReading(
            "dangerous_complacency", _SKEW_ADJUSTMENTS["dangerous_complacency"],
            f"skew={put_call_skew:.2f}, flow not confirming → Buffett warning, reduce unhedged exposure"
        )

    return SkewReading("neutral", 0, f"skew={put_call_skew:.2f} within normal range")


def _read_volume(oracle_context: Optional[dict]) -> VolumePattern:
    """Interpret volume patterns as a behavioural signal.

    Uses volume_ratio (vs average daily) and options_volume_vs_avg.

    Args:
        oracle_context: Full ORACLE intelligence packet or None.

    Returns:
        VolumePattern with pattern label and omni_adjustment.
    """
    if not oracle_context:
        return VolumePattern("neutral", 0, "oracle_context unavailable")

    price = oracle_context.get("price") or {}
    flow = oracle_context.get("flow") or {}

    volume_ratio: Optional[float] = price.get("volume_ratio")
    options_vol_ratio: Optional[float] = flow.get("options_volume_vs_avg")
    net_flow_bias: Optional[str] = flow.get("net_flow_bias")

    if volume_ratio is None:
        return VolumePattern("neutral", 0, "volume_ratio unavailable")

    # Panic: extreme volume spike (>3x avg) — contrarian exhaustion signal
    if volume_ratio > 3.0 and net_flow_bias == "bearish":
        return VolumePattern(
            "panic", _VOLUME_ADJUSTMENTS["panic"],
            f"volume={volume_ratio:.1f}x avg + bearish flow → panic, potential exhaustion"
        )

    # Euphoria: extreme call-side options volume + bullish flow
    if options_vol_ratio is not None and options_vol_ratio > 3.0 and net_flow_bias == "bullish":
        return VolumePattern(
            "euphoria", _VOLUME_ADJUSTMENTS["euphoria"],
            f"options_vol={options_vol_ratio:.1f}x avg + bullish flow → greed peak, Buffett warning"
        )

    # Accumulation: elevated volume with bullish bias
    if volume_ratio > 1.5 and net_flow_bias == "bullish":
        return VolumePattern(
            "accumulation", _VOLUME_ADJUSTMENTS["accumulation"],
            f"volume={volume_ratio:.1f}x avg + bullish flow → patient accumulation"
        )

    # Distribution: elevated volume with bearish bias
    if volume_ratio > 1.5 and net_flow_bias == "bearish":
        return VolumePattern(
            "distribution", _VOLUME_ADJUSTMENTS["distribution"],
            f"volume={volume_ratio:.1f}x avg + bearish flow → distribution, smart money exiting"
        )

    return VolumePattern("neutral", 0, f"volume={volume_ratio:.1f}x avg, no directional signal")


def _read_earnings(oracle_context: Optional[dict]) -> EarningsReaction:
    """Interpret earnings reaction as a sentiment decoder.

    Uses earnings_quality from fundamental data as a proxy for recent
    earnings beat/miss/reaction.  Returns None reading (0 adjustment)
    when no earnings data is available.

    Args:
        oracle_context: Full ORACLE intelligence packet or None.

    Returns:
        EarningsReaction with reading label and omni_adjustment.
    """
    if not oracle_context:
        return EarningsReaction(None, 0, "oracle_context unavailable")

    fundamental = oracle_context.get("fundamental") or {}
    earnings_quality: Optional[str] = fundamental.get("earnings_quality")

    if not earnings_quality:
        return EarningsReaction(None, 0, "no recent earnings data")

    # earnings_quality values used as proxy for reaction type
    eq = str(earnings_quality).lower()

    if "beat" in eq and ("rally" in eq or "positive" in eq or "strong" in eq):
        return EarningsReaction(
            "beat_rally", _EARNINGS_ADJUSTMENTS["beat_rally"],
            f"earnings_quality={earnings_quality} → genuine positive surprise"
        )
    if "beat" in eq and ("sell" in eq or "disappoint" in eq or "weak" in eq):
        return EarningsReaction(
            "beat_selloff", _EARNINGS_ADJUSTMENTS["beat_selloff"],
            f"earnings_quality={earnings_quality} → buy rumour sell news, exhausted bulls"
        )
    if "miss" in eq and ("rally" in eq or "relief" in eq):
        return EarningsReaction(
            "miss_rally", _EARNINGS_ADJUSTMENTS["miss_rally"],
            f"earnings_quality={earnings_quality} → relief rally, bears covering"
        )
    if "miss" in eq:
        return EarningsReaction(
            "miss_selloff", _EARNINGS_ADJUSTMENTS["miss_selloff"],
            f"earnings_quality={earnings_quality} → expectations missed, disappointed bulls"
        )

    return EarningsReaction(None, 0, f"earnings_quality={earnings_quality}, no clear reaction pattern")


def _read_institutional_footprint(oracle_context: Optional[dict]) -> InstitutionalFootprint:
    """Interpret dark pool and unusual options sweep signals.

    Uses unusual_activity and net_flow_bias from flow data.

    Args:
        oracle_context: Full ORACLE intelligence packet or None.

    Returns:
        InstitutionalFootprint with signal label and omni_adjustment.
    """
    if not oracle_context:
        return InstitutionalFootprint("neutral", 0, "oracle_context unavailable")

    flow = oracle_context.get("flow") or {}

    unusual_activity: Optional[str] = flow.get("unusual_activity")
    net_flow_bias: Optional[str] = flow.get("net_flow_bias")

    if not unusual_activity:
        return InstitutionalFootprint("neutral", 0, "no unusual activity detected")

    ua = str(unusual_activity).lower()

    # Bullish sweep: paid-up urgency, confirming bullish view
    if "bullish" in ua and "sweep" in ua:
        return InstitutionalFootprint(
            "bullish_sweep", _INSTITUTIONAL_ADJUSTMENTS["bullish_sweep"],
            f"unusual_activity={unusual_activity} → urgency buy, high conviction signal"
        )

    # Bearish sweep: paid-up urgency on downside
    if "bearish" in ua and "sweep" in ua:
        return InstitutionalFootprint(
            "bearish_sweep", _INSTITUTIONAL_ADJUSTMENTS["bearish_sweep"],
            f"unusual_activity={unusual_activity} → urgency hedge/short, flag caution"
        )

    # Dark pool accumulation
    if "dark" in ua and ("accum" in ua or ("buy" in ua and net_flow_bias == "bullish")):
        return InstitutionalFootprint(
            "dark_pool_accumulation", _INSTITUTIONAL_ADJUSTMENTS["dark_pool_accumulation"],
            f"unusual_activity={unusual_activity} → institutional building quietly"
        )

    # Dark pool distribution
    if "dark" in ua and ("distrib" in ua or ("sell" in ua and net_flow_bias == "bearish")):
        return InstitutionalFootprint(
            "dark_pool_distribution", _INSTITUTIONAL_ADJUSTMENTS["dark_pool_distribution"],
            f"unusual_activity={unusual_activity} → institution exiting patiently"
        )

    return InstitutionalFootprint("neutral", 0, f"unusual_activity={unusual_activity}, no directional read")


def _generate_psychology_summary(
    skew: SkewReading,
    volume: VolumePattern,
    earnings: EarningsReaction,
    institutional: InstitutionalFootprint,
    total_adj: int,
) -> str:
    """Generate a concise human-readable psychology summary.

    Args:
        skew: Skew reading result.
        volume: Volume pattern result.
        earnings: Earnings reaction result.
        institutional: Institutional footprint result.
        total_adj: Total capped adjustment.

    Returns:
        One-line summary string for Telegram card and logs.
    """
    direction = "bullish" if total_adj > 0 else ("bearish" if total_adj < 0 else "neutral")
    parts = [
        f"Skew:{skew.reading}({skew.omni_adjustment:+d})",
        f"Vol:{volume.pattern}({volume.omni_adjustment:+d})",
        f"Earn:{earnings.reading or 'none'}({earnings.omni_adjustment:+d})",
        f"Inst:{institutional.signal}({institutional.omni_adjustment:+d})",
    ]
    return f"Psychology overlay [{direction} {total_adj:+d}] — {' | '.join(parts)}"


# ---------------------------------------------------------------------------
# Main overlay function
# ---------------------------------------------------------------------------

def apply_psychology_overlay(
    base_verdict: SynthesisVerdict,
    oracle_context: Optional[dict],
) -> tuple:
    """Apply the Market Participant Psychology Overlay to a synthesis verdict.

    Reads four psychological signals from oracle_context and adjusts the
    sizing_mult of the verdict accordingly.  Does NOT change the verdict
    string.  Returns both the (possibly modified) verdict and the full
    overlay result for logging.

    The overlay is a no-op on NO_GO / CONDITIONAL / BLOCKED verdicts —
    sizing is already 0.0 in those cases.

    Args:
        base_verdict: SynthesisVerdict from compute_verdict().
        oracle_context: Full ORACLE intelligence packet or None.

    Returns:
        Tuple of (updated SynthesisVerdict, PsychologyOverlayResult).
    """
    skew = _read_skew(oracle_context)
    volume = _read_volume(oracle_context)
    earnings = _read_earnings(oracle_context)
    institutional = _read_institutional_footprint(oracle_context)

    raw_total = skew.omni_adjustment + volume.omni_adjustment + \
                earnings.omni_adjustment + institutional.omni_adjustment
    total_adj = max(_CAP_MIN, min(_CAP_MAX, raw_total))

    summary = _generate_psychology_summary(skew, volume, earnings, institutional, total_adj)
    notes: list = [summary]

    overlay = PsychologyOverlayResult(
        skew_reading=skew,
        volume_pattern=volume,
        earnings_reaction=earnings,
        institutional_footprint=institutional,
        total_adjustment=total_adj,
        psychology_summary=summary,
        notes=notes,
    )

    # Only adjust sizing if the verdict allows execution (sizing > 0)
    if base_verdict.sizing_mult <= 0.0:
        logger.debug(
            "Psychology overlay: sizing_mult=0 (verdict=%s), overlay recorded but no sizing change",
            base_verdict.verdict,
        )
        return base_verdict, overlay

    sizing_delta = total_adj * _SIZING_SCALE
    new_sizing = round(
        max(_SIZING_MIN, min(_SIZING_MAX, base_verdict.sizing_mult + sizing_delta)),
        2,
    )

    if new_sizing != base_verdict.sizing_mult:
        logger.info(
            "Psychology overlay: sizing %.2f → %.2f (adj=%+d) verdict=%s",
            base_verdict.sizing_mult, new_sizing, total_adj, base_verdict.verdict,
        )

    # Build updated verdict (replace sizing_mult and extend notes)
    updated_notes = list(base_verdict.notes) + notes
    updated_verdict = SynthesisVerdict(
        verdict=base_verdict.verdict,
        votes_go=base_verdict.votes_go,
        brains_responded=base_verdict.brains_responded,
        echo_chamber_flagged=base_verdict.echo_chamber_flagged,
        axiom_blocked=base_verdict.axiom_blocked,
        axiom_hard_stops=list(base_verdict.axiom_hard_stops),
        sizing_mult=new_sizing,
        notes=updated_notes,
        brain_summary=dict(base_verdict.brain_summary),
    )

    return updated_verdict, overlay
