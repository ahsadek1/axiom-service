"""
test_concordance.py — Prime concordance engine unit tests.

Key differences from Alpha:
  - MIN_SUBMISSION_SCORE = 63 (validated in main.py, not concordance engine)
  - GO_THRESHOLD_P1 = 70 (higher than Alpha's 65)
  - system label = 'prime' in to_dict()
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from concordance import evaluate_concordance, _weighted_score, _detect_echo_chamber


def make_sub(agent: str, score: float, reasoning: str = "") -> dict:
    return {"agent": agent, "score": score, "reasoning": reasoning}


class TestP1PrimeThreshold:
    """P1 for Prime requires weighted score ≥ 70 (not 65 like Alpha)."""

    def test_p1_forms_above_prime_threshold(self):
        subs = [
            make_sub("Cipher", 85),
            make_sub("Atlas",  78),
            make_sub("Sage",   72),
        ]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        assert result is not None
        assert result.pathway == "P1"
        assert result.system_label_check()

    def test_p1_fails_between_65_and_70(self):
        """Score between 65 (Alpha pass) and 70 (Prime pass) must FAIL for Prime."""
        subs = [
            make_sub("Cipher", 72),   # weighted ≈ 68 — passes Alpha, fails Prime
            make_sub("Atlas",  65),
            make_sub("Sage",   60),
        ]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        # Should NOT be P1 (weighted ≈ 67 < 70); may be P2 if 2 agents ≥ 78 (they're not)
        assert result is None or result.pathway != "P1"

    def test_p1_strong_go_above_80(self):
        subs = [
            make_sub("Cipher", 88),
            make_sub("Atlas",  82),
            make_sub("Sage",   76),
        ]
        result = evaluate_concordance("2026-04-10-0930", "AAPL", "bullish", subs, False)
        assert result is not None
        assert result.verdict == "STRONG_GO"

    def test_p1_go_between_70_and_80(self):
        subs = [
            make_sub("Cipher", 78),
            make_sub("Atlas",  72),
            make_sub("Sage",   68),
        ]
        result = evaluate_concordance("2026-04-10-0930", "MSFT", "bearish", subs, False)
        assert result is not None
        assert result.pathway == "P1"
        assert result.verdict == "GO"

    def test_p1_falls_through_to_p2_when_below_70(self):
        """
        3 agents but weighted < 70. Sage=0 drags it down.
        Cipher=80, Atlas=79 both ≥ 78 → P2 fires.
        weighted = 80*0.45 + 79*0.30 + 0*0.25 = 36.0 + 23.7 = 59.7 < 70.
        """
        subs = [
            make_sub("Cipher", 80),
            make_sub("Atlas",  79),
            make_sub("Sage",   0),
        ]
        result = evaluate_concordance("2026-04-10-0930", "AMD", "bullish", subs, False)
        assert result is not None
        assert result.pathway == "P2"

    def test_p1_echo_chamber_downgrades_to_p2(self):
        shared = "institutional momentum breakout with strong volume surge above resistance " \
                 "level technical setup showing continuation pattern"
        subs = [
            make_sub("Cipher", 85, shared),
            make_sub("Atlas",  82, shared),
            make_sub("Sage",   80, shared),
        ]
        result = evaluate_concordance("2026-04-10-0930", "TSLA", "bullish", subs, False)
        assert result is not None
        assert result.pathway == "P2"
        assert result.echo_chamber is True


class TestP2Prime:
    def test_p2_forms_with_two_qualifying(self):
        subs = [make_sub("Cipher", 82), make_sub("Sage", 79)]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bearish", subs, False)
        assert result is not None
        assert result.pathway == "P2"
        assert result.sizing_mult == 0.75

    def test_p2_not_formed_if_one_below_78(self):
        subs = [make_sub("Cipher", 82), make_sub("Atlas", 70)]  # Atlas < 78
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bearish", subs, False)
        assert result is None


class TestP3Prime:
    def test_p3_forms_when_enabled(self):
        subs = [make_sub("Cipher", 93)]
        result = evaluate_concordance("2026-04-10-0930", "AMD", "bullish", subs, True)
        assert result is not None
        assert result.pathway == "P3"
        assert result.sizing_mult == 0.50

    def test_p3_blocked_when_disabled(self):
        subs = [make_sub("Cipher", 95)]
        result = evaluate_concordance("2026-04-10-0930", "AMD", "bullish", subs, False)
        assert result is None


class TestSystemLabel:
    def test_to_dict_has_prime_label(self):
        subs = [make_sub("Cipher", 82), make_sub("Atlas", 79)]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        assert result is not None
        assert result.to_dict()["system"] == "prime"

    def test_to_dict_never_has_alpha_label(self):
        subs = [make_sub("Cipher", 82), make_sub("Atlas", 79)]
        result = evaluate_concordance("2026-04-10-0930", "NVDA", "bullish", subs, False)
        assert result is not None
        assert result.to_dict().get("system") != "alpha"


# Patch missing helper used in test above
from concordance import ConcordanceResult as _CR
_CR.system_label_check = lambda self: self.to_dict()["system"] == "prime"


class TestWeightedScore:
    def test_all_agents_score_80(self):
        assert round(_weighted_score({"Cipher": 80, "Atlas": 80, "Sage": 80}), 1) == 80.0

    def test_normalization_with_two_agents(self):
        assert round(_weighted_score({"Cipher": 80, "Atlas": 80}), 1) == 80.0


class TestEchoChamber:
    def test_identical_reasoning_triggers(self):
        shared = "earnings momentum catalyst fundamental revision cycle quarterly beat"
        assert _detect_echo_chamber({"Cipher": shared + " more", "Atlas": shared + " end"}) is True

    def test_distinct_reasoning_no_trigger(self):
        assert _detect_echo_chamber({
            "Cipher": "fundamental earnings growth catalyst strong institutional buying pressure",
            "Atlas":  "technical breakout above resistance volume confirmation momentum pattern",
        }) is False
