"""
test_concordance.py — Unit tests for concordance engine.

Tests all pathways, edge cases, echo chamber detection, and weighted scoring.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from concordance import evaluate_concordance, _weighted_score, _detect_echo_chamber


def make_sub(agent: str, score: float, reasoning: str = "") -> dict:
    return {"agent": agent, "score": score, "reasoning": reasoning}


class TestP1Pathway:
    """P1: All 3 agents, weighted score ≥ 65."""

    def test_p1_forms_on_three_agents(self):
        subs = [
            make_sub("Cipher", 80),
            make_sub("Atlas",  75),
            make_sub("Sage",   70),
        ]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        assert result is not None
        assert result.pathway == "P1"
        assert result.agent_count == 3
        assert result.sizing_mult == 1.0

    def test_p1_strong_go_above_80(self):
        subs = [
            make_sub("Cipher", 88),
            make_sub("Atlas",  82),
            make_sub("Sage",   78),
        ]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        assert result is not None
        assert result.verdict == "STRONG_GO"

    def test_p1_go_between_65_and_80(self):
        subs = [
            make_sub("Cipher", 70),
            make_sub("Atlas",  65),
            make_sub("Sage",   60),
        ]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        assert result is not None
        assert result.pathway == "P1"
        assert result.verdict == "GO"

    def test_p1_fails_below_threshold(self):
        """3 agents but weighted score < 65 → no concordance."""
        subs = [
            make_sub("Cipher", 60),
            make_sub("Atlas",  58),
            make_sub("Sage",   58),
        ]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        assert result is None

    def test_p1_echo_chamber_downgrades_to_p2(self):
        """P1 with echo chamber → P2 (if both agents meet P2 score threshold)."""
        repeated_reasoning = (
            "institutional momentum breakout with strong volume surge above resistance "
            "level technical setup showing continuation pattern"
        )
        subs = [
            make_sub("Cipher", 85, repeated_reasoning),
            make_sub("Atlas",  80, repeated_reasoning),
            make_sub("Sage",   78, repeated_reasoning),
        ]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        # Should be P2 after echo chamber downgrade
        assert result is not None
        assert result.pathway == "P2"
        assert result.echo_chamber is True


class TestP2Pathway:
    """P2: 2/3 agents, each score ≥ 78."""

    def test_p2_forms_with_two_qualifying_agents(self):
        subs = [
            make_sub("Cipher", 82),
            make_sub("Atlas",  79),
        ]
        result = evaluate_concordance("2026-04-10-0930", "AAPL", "bullish", subs, False)
        assert result is not None
        assert result.pathway == "P2"
        assert result.agent_count == 2
        assert result.sizing_mult == 0.75

    def test_p2_does_not_form_if_only_one_qualifies(self):
        """Only Cipher meets the P2 threshold."""
        subs = [
            make_sub("Cipher", 85),
            make_sub("Atlas",  60),   # below P2 threshold
        ]
        result = evaluate_concordance("2026-04-10-0930", "AAPL", "bullish", subs, False)
        assert result is None

    def test_p2_selects_best_two_when_three_submit(self):
        """
        3 agents submit, but Sage's score is 0 — drags weighted below 65 so P1 fails.
        Cipher=78 and Atlas=78 both meet the P2 threshold → P2 fires.
        weighted = 78*0.45 + 78*0.30 + 0*0.25 = 35.1 + 23.4 = 58.5 < 65.
        """
        subs = [
            make_sub("Cipher", 78),
            make_sub("Atlas",  78),
            make_sub("Sage",   0),    # drags weighted below 65; Sage excluded from P2
        ]
        result = evaluate_concordance("2026-04-10-0930", "MSFT", "bearish", subs, False)
        assert result is not None
        assert result.pathway == "P2"
        assert "Sage" not in result.agents_involved

    def test_p2_not_p1_when_weighted_score_low(self):
        """
        All 3 agents submit. Sage=0 drags weighted below 65 — P1 fails.
        Cipher=80 and Atlas=79 both ≥ 78 → P2 fires.
        weighted = 80*0.45 + 79*0.30 + 0*0.25 = 36.0 + 23.7 = 59.7 < 65.
        """
        subs = [
            make_sub("Cipher", 80),
            make_sub("Atlas",  79),
            make_sub("Sage",   0),   # Sage present but score too low for P1 or P2
        ]
        result = evaluate_concordance("2026-04-10-0930", "TSLA", "bullish", subs, False)
        assert result is not None
        assert result.pathway == "P2"


class TestP3Pathway:
    """P3: Solo, score ≥ 90, flag enabled."""

    def test_p3_forms_when_enabled_and_score_meets(self):
        subs = [make_sub("Cipher", 92)]
        result = evaluate_concordance("2026-04-10-0930", "AMD", "bullish", subs, solo_entries_enabled=True)
        assert result is not None
        assert result.pathway == "P3"
        assert result.sizing_mult == 0.50

    def test_p3_blocked_when_disabled(self):
        subs = [make_sub("Cipher", 95)]
        result = evaluate_concordance("2026-04-10-0930", "AMD", "bullish", subs, solo_entries_enabled=False)
        assert result is None

    def test_p3_fails_below_threshold(self):
        subs = [make_sub("Cipher", 89)]
        result = evaluate_concordance("2026-04-10-0930", "AMD", "bullish", subs, solo_entries_enabled=True)
        assert result is None

    def test_p3_strong_go_above_80(self):
        subs = [make_sub("Cipher", 93)]
        result = evaluate_concordance("2026-04-10-0930", "AMD", "bullish", subs, solo_entries_enabled=True)
        assert result is not None
        assert result.verdict == "STRONG_GO"


class TestWeightedScore:
    """Test weighted score calculation."""

    def test_all_three_agents(self):
        scores = {"Cipher": 80.0, "Atlas": 80.0, "Sage": 80.0}
        assert round(_weighted_score(scores), 1) == 80.0

    def test_cipher_dominates(self):
        """Cipher at 45% weight dominates the score."""
        scores = {"Cipher": 100.0, "Atlas": 50.0, "Sage": 50.0}
        weighted = _weighted_score(scores)
        assert weighted > 70.0    # Cipher's 45% should pull well above 50

    def test_two_agent_normalization(self):
        """With 2 agents, weights normalize to their subset."""
        scores = {"Cipher": 80.0, "Atlas": 80.0}
        weighted = _weighted_score(scores)
        assert round(weighted, 1) == 80.0

    def test_empty_returns_zero(self):
        assert _weighted_score({}) == 0.0


class TestEchoChamber:
    """Test echo chamber detection."""

    def test_detects_identical_reasoning(self):
        shared = "institutional momentum breakout volume surge technical resistance continuation"
        reasoning = {
            "Cipher": shared + " more words here",
            "Atlas":  shared + " different ending",
        }
        assert _detect_echo_chamber(reasoning) is True

    def test_ignores_short_reasoning(self):
        """Short texts (< 30 chars) should not trigger echo chamber."""
        reasoning = {"Cipher": "bullish", "Atlas": "bullish"}
        assert _detect_echo_chamber(reasoning) is False

    def test_distinct_reasoning_no_echo(self):
        reasoning = {
            "Cipher": "strong options flow institutional buying pressure above key support level",
            "Atlas":  "earnings revision cycle positive fundamental growth catalyst quarterly beat",
        }
        assert _detect_echo_chamber(reasoning) is False

    def test_single_agent_no_echo(self):
        reasoning = {"Cipher": "strong momentum breakout volume confirmation pattern"}
        assert _detect_echo_chamber(reasoning) is False


class TestNoSubmissions:
    def test_empty_submissions_returns_none(self):
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", [], False)
        assert result is None


class TestConcordanceResult:
    def test_to_dict_includes_system(self):
        subs = [make_sub("Cipher", 80), make_sub("Atlas", 79)]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        d = result.to_dict()
        assert d["system"] == "alpha"
        assert "pathway" in d
        assert "sizing_mult" in d
        assert "echo_chamber" in d
