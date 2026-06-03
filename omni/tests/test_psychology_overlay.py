"""
Tests for OMNI Market Participant Psychology Overlay.

Covers: all signal readings, adjustment caps, graceful degradation,
NO_GO verdict protection, and sizing adjustment calculations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from psychology_overlay import (
    PsychologyOverlayResult,
    SkewReading,
    VolumePattern,
    EarningsReaction,
    InstitutionalFootprint,
    apply_psychology_overlay,
    _read_skew,
    _read_volume,
    _read_earnings,
    _read_institutional_footprint,
    _CAP_MAX,
    _CAP_MIN,
)
from synthesis import SynthesisVerdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_verdict(
    verdict: str = "GO",
    votes_go: int = 3,
    sizing_mult: float = 1.0,
) -> SynthesisVerdict:
    """Build a minimal SynthesisVerdict for testing."""
    return SynthesisVerdict(
        verdict=verdict,
        votes_go=votes_go,
        brains_responded=4,
        echo_chamber_flagged=False,
        axiom_blocked=False,
        axiom_hard_stops=[],
        sizing_mult=sizing_mult,
        notes=[],
        brain_summary={},
    )


def _oracle_ctx(
    put_call_skew: Optional[float] = None,
    net_flow_bias: Optional[str] = None,
    volume_ratio: Optional[float] = None,
    options_vol_ratio: Optional[float] = None,
    unusual_activity: Optional[str] = None,
    earnings_quality: Optional[str] = None,
) -> dict:
    """Build a minimal oracle_context dict for testing."""
    return {
        "vol": {"put_call_skew": put_call_skew},
        "flow": {
            "net_flow_bias": net_flow_bias,
            "unusual_activity": unusual_activity,
            "options_volume_vs_avg": options_vol_ratio,
        },
        "price": {"volume_ratio": volume_ratio},
        "fundamental": {"earnings_quality": earnings_quality},
    }


# ---------------------------------------------------------------------------
# Test 1 — All bullish signals → +30 cap
# ---------------------------------------------------------------------------

def test_all_bullish_signals():
    """All bullish signals combined → capped at +30, sizing increases."""
    # genuine_confidence (+10) + accumulation (+10) + beat_rally (+10) + bullish_sweep (+20) = +50 → capped +30
    ctx = _oracle_ctx(
        put_call_skew=0.5, net_flow_bias="bullish",
        volume_ratio=2.0,
        unusual_activity="bullish_sweep",
        earnings_quality="beat_rally_strong",
    )
    base = _make_verdict("STRONG_GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.total_adjustment == _CAP_MAX
    assert updated.sizing_mult > 1.0
    assert updated.verdict == "STRONG_GO"  # verdict unchanged


# ---------------------------------------------------------------------------
# Test 2 — All bearish signals → -30 cap
# ---------------------------------------------------------------------------

def test_all_bearish_signals():
    """All bearish signals combined → capped at -30."""
    # informed_fear (-10) + distribution (-15) + miss_selloff (-20) + bearish_sweep (-20) = -65 → capped -30
    ctx = _oracle_ctx(
        put_call_skew=2.0, net_flow_bias=None,  # informed_fear
        volume_ratio=2.0, options_vol_ratio=None,
        unusual_activity="bearish sweep",
        earnings_quality="miss_selloff_disappointing",
    )
    # Make net_flow_bias bearish for distribution
    ctx["flow"]["net_flow_bias"] = "bearish"
    base = _make_verdict("GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.total_adjustment == _CAP_MIN
    assert updated.sizing_mult < 1.0
    assert updated.verdict == "GO"  # verdict still GO


# ---------------------------------------------------------------------------
# Test 3 — Neutral oracle → 0 adjustment
# ---------------------------------------------------------------------------

def test_neutral_all_fields():
    """Neutral oracle data → 0 adjustment, verdict and sizing unchanged."""
    ctx = _oracle_ctx(put_call_skew=1.0, volume_ratio=1.0)
    base = _make_verdict("GO", sizing_mult=0.75)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.total_adjustment == 0
    assert updated.sizing_mult == 0.75
    assert updated.verdict == "GO"


# ---------------------------------------------------------------------------
# Test 4 — Informed fear reduces confidence
# ---------------------------------------------------------------------------

def test_informed_fear_reduces_sizing():
    """High put skew with neutral flow → informed_fear → -10 sizing adj."""
    ctx = _oracle_ctx(put_call_skew=2.0, net_flow_bias=None, volume_ratio=1.0)
    base = _make_verdict("GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.skew_reading.reading == "informed_fear"
    assert overlay.skew_reading.omni_adjustment == -10
    assert updated.sizing_mult < 1.0


# ---------------------------------------------------------------------------
# Test 5 — Herding fear → contrarian boost
# ---------------------------------------------------------------------------

def test_herding_fear_contrarian_boost():
    """High put skew with bearish flow → herding_fear → +10 (contrarian)."""
    ctx = _oracle_ctx(put_call_skew=2.0, net_flow_bias="bearish", volume_ratio=1.0)
    base = _make_verdict("GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.skew_reading.reading == "herding_fear"
    assert overlay.skew_reading.omni_adjustment == 10
    assert updated.sizing_mult > 1.0


# ---------------------------------------------------------------------------
# Test 6 — Euphoria warning
# ---------------------------------------------------------------------------

def test_euphoria_warning():
    """Extreme options volume + bullish flow → euphoria → -20."""
    ctx = _oracle_ctx(
        put_call_skew=1.0,
        volume_ratio=1.0, options_vol_ratio=4.0, net_flow_bias="bullish",
    )
    base = _make_verdict("GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.volume_pattern.pattern == "euphoria"
    assert overlay.volume_pattern.omni_adjustment == -20


# ---------------------------------------------------------------------------
# Test 7 — Panic contrarian signal
# ---------------------------------------------------------------------------

def test_panic_contrarian_signal():
    """Extreme volume spike + bearish flow → panic → +15 contrarian."""
    ctx = _oracle_ctx(volume_ratio=4.0, net_flow_bias="bearish")
    base = _make_verdict("GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.volume_pattern.pattern == "panic"
    assert overlay.volume_pattern.omni_adjustment == 15


# ---------------------------------------------------------------------------
# Test 8 — Dark pool accumulation
# ---------------------------------------------------------------------------

def test_dark_pool_accumulation():
    """Dark pool accumulation signal → +15."""
    ctx = _oracle_ctx(unusual_activity="dark pool accumulation buy", net_flow_bias="bullish")
    base = _make_verdict("GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.institutional_footprint.signal == "dark_pool_accumulation"
    assert overlay.institutional_footprint.omni_adjustment == 15


# ---------------------------------------------------------------------------
# Test 9 — None oracle_context → graceful, all neutral, no exception
# ---------------------------------------------------------------------------

def test_none_oracle_context_graceful():
    """None oracle_context → all readings neutral, 0 adjustment, no exception."""
    base = _make_verdict("GO", sizing_mult=0.75)
    updated, overlay = apply_psychology_overlay(base, None)

    assert overlay.total_adjustment == 0
    assert overlay.skew_reading.reading == "neutral"
    assert overlay.volume_pattern.pattern == "neutral"
    assert overlay.earnings_reaction.reading is None
    assert overlay.institutional_footprint.signal == "neutral"
    assert updated.sizing_mult == 0.75  # unchanged


# ---------------------------------------------------------------------------
# Test 10 — Cap enforcement positive
# ---------------------------------------------------------------------------

def test_cap_enforcement_positive():
    """Raw sum > 30 → capped at exactly 30."""
    # bullish_sweep (+20) + genuine_confidence (+10) + beat_rally (+10) + accumulation (+10) = +50
    ctx = _oracle_ctx(
        put_call_skew=0.5, net_flow_bias="bullish",
        volume_ratio=2.0,
        unusual_activity="bullish sweep",
        earnings_quality="beat_rally_positive",
    )
    base = _make_verdict("STRONG_GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert overlay.total_adjustment == 30


# ---------------------------------------------------------------------------
# Test 11 — Cap enforcement negative
# ---------------------------------------------------------------------------

def test_cap_enforcement_negative():
    """Raw sum < -30 → capped at exactly -30."""
    ctx = _oracle_ctx(
        put_call_skew=2.0, net_flow_bias="bearish",
        volume_ratio=2.0,
        unusual_activity="bearish sweep",
        earnings_quality="miss_selloff",
    )
    base = _make_verdict("GO", sizing_mult=1.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    # distribution(-15) + informed_fear(-10) + bearish_sweep(-20) + miss_selloff(-20) = -65 → -30
    assert overlay.total_adjustment == -30


# ---------------------------------------------------------------------------
# Test 12 — NO_GO verdict not changed to GO by psychology
# ---------------------------------------------------------------------------

def test_no_go_verdict_not_changed():
    """Psychology overlay on NO_GO verdict: sizing stays 0, verdict unchanged."""
    # All bullish signals — but NO_GO verdict must not become GO
    ctx = _oracle_ctx(
        put_call_skew=0.5, net_flow_bias="bullish",
        volume_ratio=2.0,
        unusual_activity="bullish sweep",
    )
    base = _make_verdict("NO_GO", votes_go=1, sizing_mult=0.0)
    updated, overlay = apply_psychology_overlay(base, ctx)

    assert updated.verdict == "NO_GO"
    assert updated.sizing_mult == 0.0   # sizing 0 is not modified by overlay
    # overlay still records the psychology (for CHRONICLE)
    assert overlay.total_adjustment > 0


# ---------------------------------------------------------------------------
# Additional unit tests for individual readers
# ---------------------------------------------------------------------------

def test_dangerous_complacency():
    """Low skew without confirming bullish flow → dangerous_complacency → -15."""
    ctx = _oracle_ctx(put_call_skew=0.5, net_flow_bias=None)
    reading = _read_skew(ctx)
    assert reading.reading == "dangerous_complacency"
    assert reading.omni_adjustment == -15


def test_beat_selloff_exhausted_bulls():
    """Beat + sell reaction → -15 adjustment."""
    ctx = _oracle_ctx(earnings_quality="beat_selloff_weak")
    reading = _read_earnings(ctx)
    assert reading.reading == "beat_selloff"
    assert reading.omni_adjustment == -15


def test_miss_rally_relief():
    """Miss + rally → relief, +10 contrarian."""
    ctx = _oracle_ctx(earnings_quality="miss_rally_relief")
    reading = _read_earnings(ctx)
    assert reading.reading == "miss_rally"
    assert reading.omni_adjustment == 10


def test_overlay_result_serialization():
    """PsychologyOverlayResult.to_dict() produces expected keys."""
    ctx = _oracle_ctx(put_call_skew=1.0, volume_ratio=1.0)
    base = _make_verdict("GO")
    _, overlay = apply_psychology_overlay(base, ctx)

    d = overlay.to_dict()
    assert "skew_reading" in d
    assert "volume_pattern" in d
    assert "earnings_reaction" in d
    assert "institutional_footprint" in d
    assert "total_adjustment" in d
    assert "psychology_summary" in d
