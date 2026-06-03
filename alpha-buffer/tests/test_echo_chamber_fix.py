"""
test_echo_chamber_fix.py — Tests for the 2026-04-24 echo chamber fix and
score threshold recalibration.

Validates:
  1. Natural vocabulary overlap from same ORACLE data → NOT flagged as echo chamber
  2. Structurally identical reasoning → IS flagged as echo chamber
  3. P2 now fires at score 65 (was 78, blocked all trades)
  4. P2 does NOT fire below 65
  5. Short reasoning strings skip echo chamber check (avoids small-set false positives)
  6. P1 fires at exactly 65.0 weighted
  7. P1 score 64.9 falls through to P2
"""

import pytest
from concordance import _detect_echo_chamber, evaluate_concordance


# ─────────────────────────────────────────────────────────────
# Echo Chamber: Natural Vocabulary Overlap (Should NOT Flag)
# ─────────────────────────────────────────────────────────────

class TestNaturalVocabularyNotEchoChambered:

    def test_same_stock_independent_analysis_no_echo(self):
        """
        Two agents analyze the same stock from ORACLE data, use overlapping
        financial vocabulary — bullish, momentum, support, earnings, etc.
        This is legitimate independent analysis. Should NOT flag echo chamber.
        """
        agent_reasoning = {
            "Cipher": (
                "NVDA is exhibiting strong bullish momentum following a decisive breakout "
                "above the 200-day moving average. The recent earnings beat exceeded analyst "
                "expectations by 15% on revenue and margin expansion. With the AI chip demand "
                "cycle intact and institutional accumulation visible in the order flow, the "
                "risk/reward setup favors a bull put spread. VIX regime is benign. "
                "Technical confluence at $145 puts the probability of staying above "
                "the short strike at approximately 72%."
            ),
            "Atlas": (
                "NVDA shows a clear bullish structure with the stock reclaiming the 200-day "
                "moving average on above-average volume. Earnings results were materially "
                "above consensus on both revenue and operating margin. Demand dynamics in the "
                "datacenter and AI accelerator space remain structurally elevated. The options "
                "market implied move is manageable relative to the spread width. "
                "A bull put spread at the 145/140 strikes offers a favorable credit-to-risk "
                "ratio given the underlying's current range."
            ),
            "Sage": (
                "NVDA is in a bullish trend structure after breaking above the 200-day average. "
                "The latest earnings report demonstrated strength in datacenter revenue and "
                "margins. Macro tailwinds from AI infrastructure spending support continued "
                "momentum in the near term. Options positioning is consistent with limited "
                "downside risk in the 30-day window. The spread structure at 145/140 aligns "
                "with key support identified in the price action."
            ),
        }
        # All three agents say "NVDA bullish 200-day earnings datacenter" — but independently.
        # This must NOT flag as echo chamber.
        result = _detect_echo_chamber(agent_reasoning)
        assert result is False, (
            "Natural vocabulary overlap on same ORACLE data should NOT trigger echo chamber. "
            "Agents independently analyzing the same stock will share financial domain terms."
        )

    def test_different_tickers_independent_analysis_no_echo(self):
        """Two distinct analyses for different setups — definitely not echo chamber."""
        agent_reasoning = {
            "Cipher": (
                "JPM demonstrates strong technical confluence at the $200 support level "
                "following the pullback from recent highs. Net interest margin guidance was "
                "above consensus and loan loss provisions are declining. The financials sector "
                "is benefiting from the current yield curve steepening. Credit spreads on "
                "consumer loans remain within historical norms. A bull put spread on JPM "
                "with 60 DTE captures the dividend yield support and options skew."
            ),
            "Atlas": (
                "JPM's recent technical structure shows a classic support test at the prior "
                "breakout level near $200. The bank's capital position is strong with a CET1 "
                "ratio well above regulatory minimums. Management provided positive guidance "
                "on net interest income which exceeded what the street had modeled. "
                "The rate environment continues to favor money-center banks with floating "
                "rate loan books. Options flow is skewed toward calls, suggesting institutional "
                "positioning for upside continuation in the near term."
            ),
        }
        result = _detect_echo_chamber(agent_reasoning)
        assert result is False


# ─────────────────────────────────────────────────────────────
# Echo Chamber: Structural Copying (SHOULD Flag)
# ─────────────────────────────────────────────────────────────

class TestStructuralEchoChamberDetected:

    def test_identical_reasoning_strings_flagged(self):
        """Completely identical reasoning strings → definitive echo chamber."""
        text = (
            "AAPL is showing bullish momentum above key support with strong earnings catalyst "
            "and favorable technical setup. The options spread offers attractive risk-reward "
            "given the current volatility regime. Institutional accumulation is visible "
            "in the order flow and the risk-reward favors the bull put spread structure "
            "with a 72% probability of expiring worthless at the short strike."
        )
        agent_reasoning = {"Cipher": text, "Atlas": text}
        assert _detect_echo_chamber(agent_reasoning) is True

    def test_near_identical_same_opening_same_length_flagged(self):
        """
        Same first 8 words + within 10% length = structural copy.
        This is the primary echo chamber signal.
        """
        base = (
            "the stock is exhibiting strong bullish momentum above key technical support "
            "following a decisive earnings beat that exceeded consensus expectations on "
            "revenue growth and operating margin expansion in the most recent quarter. "
            "the risk reward setup is highly favorable for a bull put spread structure "
            "with approximately 70 percent probability of expiring worthless."
        )
        # Trivially different ending — same structure, same opening
        variant = (
            "the stock is exhibiting strong bullish momentum above key technical support "
            "following a decisive earnings beat that exceeded consensus expectations on "
            "revenue growth and operating margin expansion in the most recent quarter. "
            "the risk reward setup is highly favorable for a bull put spread structure "
            "with approximately 71 percent probability of expiring worthless."
        )
        agent_reasoning = {"Cipher": base, "Atlas": variant}
        assert _detect_echo_chamber(agent_reasoning) is True

    def test_single_agent_never_echo_chamber(self):
        """Only one agent — cannot have echo chamber. Must return False."""
        agent_reasoning = {
            "Cipher": (
                "TSLA is in a strong bullish trend with momentum above key support levels. "
                "Earnings beat was significant and the technical setup is very favorable "
                "for continuation into the next earnings period with strong institutional "
                "buying visible in the options flow and order book structure."
            )
        }
        assert _detect_echo_chamber(agent_reasoning) is False


# ─────────────────────────────────────────────────────────────
# Short Reasoning: Skip Echo Chamber Check
# ─────────────────────────────────────────────────────────────

class TestShortReasoningSkipsEchoCheck:

    def test_short_reasoning_not_flagged(self):
        """
        If either agent has < 15 significant words after filtering domain stopwords,
        skip the vocabulary overlap check entirely.
        Short strings produce false positives (e.g., 3 shared words / 4 total = 75%).
        """
        agent_reasoning = {
            "Cipher": "AAPL bullish above support strong setup.",  # Too short, filtered by len>30
            "Atlas":  "AAPL bullish above support strong setup.",  # Same, too short
        }
        # Both strings are < 30 chars — filtered at the outer guard
        assert _detect_echo_chamber(agent_reasoning) is False

    def test_sparse_significant_words_after_domain_filtering(self):
        """
        Reasoning that is long but almost entirely domain stopwords, with
        DIFFERENT openings (so Stage 1 structural check doesn't fire).
        After filtering, both sets have < 15 significant words → vocabulary
        overlap check skipped. The strings are NOT structurally identical.
        """
        # Different openings so Stage 1 (structural) doesn't trigger.
        # Content is almost entirely financial domain stopwords — after filtering,
        # significant word sets will be < 15 words → Stage 2 vocab check skipped.
        agent_reasoning = {
            "Cipher": (
                "cipher assessment: strong momentum above support levels showing breakout "
                "pattern with earnings catalyst and revenue growth margin expansion sector "
                "index moving average signal setup volume levels target resistance trend "
                "technical conditions market outlook volatility regime rates yields inflation "
                "federal reserve economic uncertainty spread credit options theta delta gamma "
                "vega contracts expiration implied trading currently strong conviction thesis."
            ),
            "Atlas": (
                "atlas assessment: strong bullish stock with earnings catalyst showing breakout "
                "above resistance with revenue growth margin expansion sector outlook index "
                "moving average signal setup volume levels target support trend technical "
                "conditions market volatility regime rates yields inflation federal reserve "
                "economic uncertainty spread credit options theta delta gamma vega contracts "
                "expiration implied trading currently strong conviction thesis."
            ),
        }
        # Different first 8 words → Stage 1 won't fire.
        # After domain stopword filtering, significant words < 15 → Stage 2 skipped.
        result = _detect_echo_chamber(agent_reasoning)
        assert result is False, (
            "When significant word sets are too small after domain filtering (and strings "
            "are not structurally identical), echo chamber check should be skipped."
        )


# ─────────────────────────────────────────────────────────────
# P2 Threshold Recalibration: 78 → 65
# ─────────────────────────────────────────────────────────────

class TestP2ThresholdRecalibration:

    def _make_submission(self, agent: str, score: float, ticker: str = "CBRE",
                          direction: str = "bullish", window_id: str = "2026-04-24-1000") -> dict:
        return {
            "agent":      agent,
            "score":      score,
            "reasoning":  f"{agent} analysis of {ticker} shows a favorable {direction} setup.",
            "window_id":  window_id,
            "ticker":     ticker,
            "direction":  direction,
        }

    def test_p2_fires_at_66_and_67(self):
        """
        Two agents scoring 66 and 67 — previously blocked by MIN_SCORE_P2=78.
        After fix (MIN_SCORE_P2=65), P2 should fire.
        """
        submissions = [
            self._make_submission("Cipher", 66.0),
            self._make_submission("Atlas",  67.0),
        ]
        result = evaluate_concordance(
            window_id="2026-04-24-1000",
            ticker="CBRE",
            direction="bullish",
            submissions=submissions,
            solo_entries_enabled=False,
        )
        assert result is not None, (
            "P2 should fire when 2 agents both score >= 65. "
            "This was blocked at 78 — that's the bug being fixed."
        )
        assert result.pathway == "P2"
        assert result.verdict in ("GO", "STRONG_GO")

    def test_p2_fires_at_exactly_65_each(self):
        """Boundary: both agents at exactly 65 → P2 fires."""
        submissions = [
            self._make_submission("Sage",  65.0),
            self._make_submission("Atlas", 65.0),
        ]
        result = evaluate_concordance(
            window_id="2026-04-24-1000",
            ticker="HD",
            direction="bullish",
            submissions=submissions,
            solo_entries_enabled=False,
        )
        assert result is not None
        assert result.pathway == "P2"

    def test_p2_does_not_fire_below_65(self):
        """
        Both agents score 63 and 64 — below the new P2 threshold of 65.
        P2 should NOT fire.
        """
        submissions = [
            self._make_submission("Cipher", 63.0),
            self._make_submission("Atlas",  64.0),
        ]
        result = evaluate_concordance(
            window_id="2026-04-24-1000",
            ticker="BAC",
            direction="bullish",
            submissions=submissions,
            solo_entries_enabled=False,
        )
        assert result is None, (
            "P2 should NOT fire when agents score below 65. "
            "64.9 and below remain correctly filtered."
        )

    def test_p2_one_agent_above_one_below_does_not_fire(self):
        """One agent at 70, one at 62 — only one qualifies. P2 needs 2."""
        submissions = [
            self._make_submission("Cipher", 70.0),
            self._make_submission("Atlas",  62.0),
        ]
        result = evaluate_concordance(
            window_id="2026-04-24-1000",
            ticker="COF",
            direction="bullish",
            submissions=submissions,
            solo_entries_enabled=False,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────
# P1 Boundary Cases
# ─────────────────────────────────────────────────────────────

class TestP1BoundaryCases:

    def _make_distinct_reasoning(self, agent: str, ticker: str, extra: str) -> str:
        """Generate reasoning strings that are clearly distinct (won't trigger echo chamber)."""
        agent_perspectives = {
            "Cipher": f"{ticker} demonstrates a technically confirmed {extra} trend structure with "
                      f"quantifiable momentum metrics and volume confirmation. The risk/reward "
                      f"calculation favors entry at current levels based on historical analog patterns "
                      f"and the current implied volatility surface pricing. Kelly criterion applied "
                      f"to the probability distribution suggests optimal sizing at 75 basis points.",
            "Atlas":  f"{ticker} exhibits {extra} fundamental characteristics supported by "
                      f"accelerating free cash flow generation and balance sheet resilience. "
                      f"The competitive moat has strengthened through recent product cycle execution. "
                      f"Relative to sector peers, the valuation multiple appears undemanding given "
                      f"the projected three-year compound annual growth trajectory.",
            "Sage":   f"{ticker} is positioned favorably in the current {extra} macro regime. "
                      f"Cross-asset signals including bond market positioning and currency flows "
                      f"align with the directional thesis. Sentiment indicators have normalized "
                      f"from extreme levels, reducing the contrarian headwind. The statistical "
                      f"edge in this setup type has been persistent across multiple market cycles.",
        }
        return agent_perspectives.get(agent, f"{agent} analysis of {ticker}: {extra} setup confirmed.")

    def test_p1_fires_at_exactly_65_weighted(self):
        """
        P1 weighted score exactly 65.0 → GO verdict.
        Cipher(65) * 0.45 + Atlas(65) * 0.30 + Sage(65) * 0.25 = 65.0
        """
        submissions = [
            {"agent": "Cipher", "score": 65.0,
             "reasoning": self._make_distinct_reasoning("Cipher", "CMS", "bullish")},
            {"agent": "Atlas",  "score": 65.0,
             "reasoning": self._make_distinct_reasoning("Atlas",  "CMS", "bullish")},
            {"agent": "Sage",   "score": 65.0,
             "reasoning": self._make_distinct_reasoning("Sage",   "CMS", "bullish")},
        ]
        result = evaluate_concordance(
            window_id="2026-04-24-1000",
            ticker="CMS",
            direction="bullish",
            submissions=submissions,
            solo_entries_enabled=False,
        )
        assert result is not None, "P1 must fire when weighted score is exactly 65.0"
        assert result.pathway == "P1"
        assert result.verdict == "GO"
        assert abs(result.weighted_score - 65.0) < 0.01

    def test_p1_score_649_falls_through_to_p2(self):
        """
        Weighted score 64.9 (just below P1 threshold) → P1 fails, P2 attempted.
        Cipher(64) * 0.45 + Atlas(66) * 0.30 + Sage(65) * 0.25 = 64.85 → below 65
        Then P2 checks: all 3 agents ≥ 65? Atlas=66✓ Sage=65✓ → P2 fires.
        """
        submissions = [
            {"agent": "Cipher", "score": 64.0,
             "reasoning": self._make_distinct_reasoning("Cipher", "DHI", "bearish")},
            {"agent": "Atlas",  "score": 66.0,
             "reasoning": self._make_distinct_reasoning("Atlas",  "DHI", "bearish")},
            {"agent": "Sage",   "score": 65.0,
             "reasoning": self._make_distinct_reasoning("Sage",   "DHI", "bearish")},
        ]
        result = evaluate_concordance(
            window_id="2026-04-24-1000",
            ticker="DHI",
            direction="bearish",
            submissions=submissions,
            solo_entries_enabled=False,
        )
        # P1 fails (weighted ~64.85 < 65). P2 should catch Atlas(66) + Sage(65).
        assert result is not None, (
            "When P1 just misses, P2 should catch qualifying 2-agent subset."
        )
        assert result.pathway == "P2"
        assert "Atlas" in result.agents_involved
        assert "Sage" in result.agents_involved
