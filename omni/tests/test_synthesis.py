"""
test_synthesis.py — Unit tests for OMNI synthesis/voting engine.

Tests all verdict paths, P3/P4 rules, Axiom hard stops,
echo chamber downgrade, and insufficient brain handling.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from synthesis import compute_verdict, build_context, SynthesisVerdict


def make_brain_results(votes: dict[str, str], confidence: int = 80) -> dict[str, dict]:
    """Build brain results dict with specified votes. No errors."""
    results = {}
    for brain in ("claude", "o3mini", "gemini", "deepseek"):
        vote = votes.get(brain, "GO")
        results[brain] = {
            "vote":         vote,
            "confidence":   confidence,
            "concern_1":    "None",
            "concern_2":    "None",
            "echo_chamber": False,
            "reasoning":    f"{brain} voted {vote}",
        }
    return results


def make_error_brain_results(error_count: int) -> dict[str, dict]:
    """Build brain results with some brains errored."""
    results = {}
    brains = ["claude", "o3mini", "gemini", "deepseek"]
    for i, brain in enumerate(brains):
        if i < error_count:
            results[brain] = {"vote": None, "confidence": None, "error": "timeout"}
        else:
            results[brain] = {
                "vote":         "GO",
                "confidence":   80,
                "concern_1":    "None",
                "concern_2":    "None",
                "echo_chamber": False,
                "reasoning":    "voted GO",
            }
    return results


class TestVotingVerdict:
    """Test standard voting outcomes."""

    def test_4_of_4_go_is_strong_go(self):
        results = make_brain_results({b: "GO" for b in ("claude","o3mini","gemini","deepseek")})
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "STRONG_GO"
        assert v.votes_go == 4
        assert v.can_execute() is True

    def test_3_of_4_go_is_go(self):
        results = make_brain_results({"claude":"GO","o3mini":"GO","gemini":"GO","deepseek":"NO_GO"})
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "GO"
        assert v.votes_go == 3
        assert v.can_execute() is True

    def test_2_of_4_go_is_conditional(self):
        results = make_brain_results({"claude":"GO","o3mini":"GO","gemini":"NO_GO","deepseek":"NO_GO"})
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "CONDITIONAL"
        assert v.can_execute() is False

    def test_1_of_4_go_is_no_go(self):
        results = make_brain_results({"claude":"GO","o3mini":"NO_GO","gemini":"NO_GO","deepseek":"NO_GO"})
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "NO_GO"
        assert v.can_execute() is False

    def test_0_go_is_no_go(self):
        results = make_brain_results({b: "NO_GO" for b in ("claude","o3mini","gemini","deepseek")})
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "NO_GO"


class TestP3P4Rules:
    """P3 and P4 require minimum 3/4 GO."""

    def test_p3_requires_3_go_passes(self):
        results = make_brain_results({"claude":"GO","o3mini":"GO","gemini":"GO","deepseek":"NO_GO"})
        v = compute_verdict(results, "P3", 0.5, None)
        assert v.verdict == "GO"
        assert v.can_execute() is True

    def test_p3_with_only_2_go_is_no_go(self):
        results = make_brain_results({"claude":"GO","o3mini":"GO","gemini":"NO_GO","deepseek":"NO_GO"})
        v = compute_verdict(results, "P3", 0.5, None)
        assert v.verdict == "NO_GO"
        assert v.can_execute() is False

    def test_p4_requires_3_go(self):
        results = make_brain_results({"claude":"GO","o3mini":"GO","gemini":"NO_GO","deepseek":"NO_GO"})
        v = compute_verdict(results, "P4", 0.25, None)
        assert v.verdict == "NO_GO"

    def test_p1_allows_2_go(self):
        """P1/P2 do NOT have the 3-GO minimum — 2/4 = CONDITIONAL (not NO_GO)."""
        results = make_brain_results({"claude":"GO","o3mini":"GO","gemini":"NO_GO","deepseek":"NO_GO"})
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "CONDITIONAL"


class TestAxiomHardStop:
    """Axiom hard stops block execution regardless of brain votes."""

    def test_hard_stop_overrides_4_go_votes(self):
        results = make_brain_results({b: "GO" for b in ("claude","o3mini","gemini","deepseek")})
        axiom   = {"hard_stops": ["CRISIS regime — no new entries"], "sizing_mult": 0.0, "risk_score": 10}
        v = compute_verdict(results, "P1", 1.0, axiom)
        assert v.verdict == "NO_GO"
        assert v.axiom_blocked is True
        assert v.can_execute() is False
        assert len(v.axiom_hard_stops) > 0

    def test_no_hard_stops_does_not_block(self):
        results = make_brain_results({b: "GO" for b in ("claude","o3mini","gemini","deepseek")})
        axiom   = {"hard_stops": [], "sizing_mult": 1.0, "risk_score": 2.5}
        v = compute_verdict(results, "P1", 1.0, axiom)
        assert v.axiom_blocked is False
        assert v.can_execute() is True


class TestEchoChamber:
    """Echo chamber flag downgrades STRONG_GO → GO."""

    def test_echo_chamber_downgrades_strong_go(self):
        results = make_brain_results({b: "GO" for b in ("claude","o3mini","gemini","deepseek")})
        results["claude"]["echo_chamber"] = True  # claude detects echo chamber
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "GO"              # downgraded from STRONG_GO
        assert v.echo_chamber_flagged is True

    def test_echo_chamber_does_not_downgrade_go(self):
        results = make_brain_results({"claude":"GO","o3mini":"GO","gemini":"GO","deepseek":"NO_GO"})
        results["o3mini"]["echo_chamber"] = True
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "GO"              # stays GO
        assert v.echo_chamber_flagged is True


class TestInsufficientBrains:
    """Fewer than 3 brains responding → CONDITIONAL."""

    def test_2_brains_error_triggers_conditional(self):
        """Only 2 brains respond → below MIN_BRAINS_REQUIRED (3) → CONDITIONAL."""
        results = make_error_brain_results(error_count=2)
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "CONDITIONAL"
        assert v.brains_responded == 2

    def test_3_brains_respond_proceeds_normally(self):
        """3 brains respond → synthesis proceeds."""
        results = make_error_brain_results(error_count=1)
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.brains_responded == 3
        # 3 GO votes from non-error brains → GO
        assert v.verdict in ("GO", "STRONG_GO")

    def test_all_brains_error_triggers_conditional(self):
        results = make_error_brain_results(error_count=4)
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.verdict == "CONDITIONAL"
        assert v.can_execute() is False


class TestSizingMultiplier:
    """Test sizing multiplier calculation."""

    def test_sizing_mult_applied(self):
        results = make_brain_results({b: "GO" for b in ("claude","o3mini","gemini","deepseek")})
        axiom   = {"hard_stops": [], "sizing_mult": 0.75, "risk_score": 3.0}
        v = compute_verdict(results, "P2", 0.75, axiom)
        # P2 sizing (0.75) × Axiom sizing (0.75) = 0.56
        assert v.sizing_mult == round(0.75 * 0.75, 2)

    def test_axiom_blocks_zeros_sizing(self):
        results = make_brain_results({b: "GO" for b in ("claude","o3mini","gemini","deepseek")})
        axiom   = {"hard_stops": ["blocked"], "sizing_mult": 0.0, "risk_score": 10}
        v = compute_verdict(results, "P1", 1.0, axiom)
        assert v.sizing_mult == 0.0

    def test_no_axiom_uses_concordance_sizing(self):
        results = make_brain_results({b: "GO" for b in ("claude","o3mini","gemini","deepseek")})
        v = compute_verdict(results, "P1", 1.0, None)
        assert v.sizing_mult == 1.0


class TestBuildContext:
    """Test context builder for brain prompts."""

    def test_context_includes_all_keys(self):
        concordance = {
            "ticker": "NVDA", "direction": "bullish", "system": "alpha",
            "pathway": "P1", "weighted_score": 82.0, "agents_involved": ["Cipher"],
            "scores": {"Cipher": 82}, "verdict": "GO", "sizing_mult": 1.0,
            "window_id": "2026-04-10-0930", "echo_chamber": False, "notes": [],
        }
        ctx = build_context(concordance, None, None)
        assert ctx["ticker"]         == "NVDA"
        assert ctx["system"]         == "alpha"
        assert ctx["pathway"]        == "P1"
        assert "axiom"               in ctx
        assert "regime"              in ctx
        assert "performance_targets" in ctx

    def test_context_performance_targets_correct(self):
        concordance = {
            "ticker": "AAPL", "direction": "bullish", "system": "prime",
            "pathway": "P2", "weighted_score": 79.0, "agents_involved": [],
            "scores": {}, "verdict": "GO", "sizing_mult": 0.75,
            "window_id": "2026-04-10-1000", "echo_chamber": False, "notes": [],
        }
        ctx = build_context(concordance, None, None)
        targets = ctx["performance_targets"]
        assert targets["win_rate_target"]     == 0.75
        assert targets["avg_win_pct_target"]  == 0.40
        assert targets["loss_rate_ceiling"]   == 0.25
        assert targets["avg_loss_pct_ceiling"] == 0.20
