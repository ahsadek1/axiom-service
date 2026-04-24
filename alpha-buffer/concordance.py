"""
concordance.py — Alpha Concordance Engine

Core logic for evaluating submissions and determining concordance pathways.
Called after every submission. Returns a ConcordanceResult or None.

Pathways:
  P1 — 3/3 agents, same ticker/direction, weighted score ≥ 65. OMNI validates.
  P2 — 2/3 agents, each score ≥ 78, same direction. OMNI arbitrates.
  P3 — 1 agent, score ≥ 90, SOLO flag enabled. OMNI independently evaluates.
  (P4 is OMNI-initiated, not triggered here.)

Echo chamber rule:
  If submissions look identical (same reasoning keywords from ≥2 agents) → P1 auto-downgrade to P2.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from config import (
    AGENT_WEIGHTS,
    GO_THRESHOLD_P1,
    MIN_SCORE_P2,
    MIN_SCORE_SOLO_P3,
    PATHWAY_SIZING,
    STRONG_GO_THRESHOLD,
    VALID_AGENTS,
)

logger = logging.getLogger("alpha_buffer.concordance")


@dataclass
class ConcordanceResult:
    """Outcome of a concordance evaluation."""

    ticker:          str
    direction:       str
    window_id:       str
    pathway:         str            # P1, P2, P3
    agent_count:     int
    weighted_score:  float
    sizing_mult:     float
    verdict:         str            # GO or STRONG_GO
    agents_involved: list[str]
    scores:          dict[str, float]
    echo_chamber:    bool           # True if P1 was downgraded to P2
    notes:           list[str]      # Audit notes about the decision

    def to_dict(self) -> dict:
        """Serialize to dict for API responses and OMNI dispatch."""
        return {
            "ticker":          self.ticker,
            "direction":       self.direction,
            "window_id":       self.window_id,
            "pathway":         self.pathway,
            "agent_count":     self.agent_count,
            "weighted_score":  round(self.weighted_score, 2),
            "sizing_mult":     self.sizing_mult,
            "verdict":         self.verdict,
            "agents_involved": self.agents_involved,
            "scores":          self.scores,
            "echo_chamber":    self.echo_chamber,
            "notes":           self.notes,
            "system":          "alpha",
        }


def evaluate_concordance(
    window_id:            str,
    ticker:               str,
    direction:            str,
    submissions:          list[dict],
    solo_entries_enabled: bool,
) -> Optional[ConcordanceResult]:
    """
    Evaluate concordance for a ticker/direction/window after a new submission arrives.

    Args:
        window_id:            Current 15-minute window ID.
        ticker:               Stock ticker symbol.
        direction:            'bullish' or 'bearish'.
        submissions:          All submissions for this ticker/direction/window.
        solo_entries_enabled: Whether P3 solo high-conviction entries are enabled.

    Returns:
        ConcordanceResult if a pathway is triggered, None otherwise.
    """
    if not submissions:
        return None

    # Build agent→score map from submissions (last submission per agent wins)
    agent_scores: dict[str, float] = {}
    agent_reasoning: dict[str, str] = {}
    for sub in submissions:
        agent = sub["agent"]
        if agent in VALID_AGENTS:
            agent_scores[agent]    = float(sub["score"])
            agent_reasoning[agent] = sub.get("reasoning") or ""

    participating_agents = set(agent_scores.keys())
    count                = len(participating_agents)

    notes: list[str] = []

    # ── P1: All 3 agents agree ────────────────────────────────────────────────
    if participating_agents == VALID_AGENTS:
        weighted = _weighted_score(agent_scores)
        echo     = _detect_echo_chamber(agent_reasoning)

        if echo:
            notes.append("Echo chamber detected — P1 downgraded to P2")
            logger.warning("Echo chamber on %s/%s — downgrading P1 → P2", ticker, direction)
            # Fall through to P2 logic with all 3 agents
            return _try_p2(
                window_id, ticker, direction,
                agent_scores, echo_chamber=True, notes=notes,
            )

        if weighted >= GO_THRESHOLD_P1:
            verdict = "STRONG_GO" if weighted >= STRONG_GO_THRESHOLD else "GO"
            return ConcordanceResult(
                ticker          = ticker,
                direction       = direction,
                window_id       = window_id,
                pathway         = "P1",
                agent_count     = 3,
                weighted_score  = weighted,
                sizing_mult     = PATHWAY_SIZING["P1"],
                verdict         = verdict,
                agents_involved = sorted(participating_agents),
                scores          = dict(agent_scores),
                echo_chamber    = False,
                notes           = notes,
            )
        else:
            notes.append(
                f"P1 failed: 3/3 agents present but weighted score {weighted:.1f} < {GO_THRESHOLD_P1} "
                f"— falling through to P2 check"
            )
            logger.info(
                "P1 failed for %s/%s (score %.1f < %.1f) — attempting P2",
                ticker, direction, weighted, GO_THRESHOLD_P1,
            )
            # Fall through: try P2 with any qualifying 2-agent subset

    # ── P2: Any 2 agents both meet threshold ──────────────────────────────────
    if count >= 2:
        return _try_p2(
            window_id, ticker, direction,
            agent_scores, echo_chamber=False, notes=notes,
        )

    # ── P3: Solo high-conviction ──────────────────────────────────────────────
    if count == 1 and solo_entries_enabled:
        agent = next(iter(participating_agents))
        score = agent_scores[agent]

        if score >= MIN_SCORE_SOLO_P3:
            verdict = "STRONG_GO" if score >= STRONG_GO_THRESHOLD else "GO"
            notes.append(f"Solo P3 entry — {agent} score {score:.1f} ≥ {MIN_SCORE_SOLO_P3}")
            return ConcordanceResult(
                ticker          = ticker,
                direction       = direction,
                window_id       = window_id,
                pathway         = "P3",
                agent_count     = 1,
                weighted_score  = score,
                sizing_mult     = PATHWAY_SIZING["P3"],
                verdict         = verdict,
                agents_involved = [agent],
                scores          = {agent: score},
                echo_chamber    = False,
                notes           = notes,
            )

    return None


def _try_p2(
    window_id:    str,
    ticker:       str,
    direction:    str,
    agent_scores: dict[str, float],
    echo_chamber: bool,
    notes:        list[str],
) -> Optional[ConcordanceResult]:
    """
    Attempt to form a P2 concordance from available agent scores.

    Finds the best 2-agent pair where both scores meet the P2 threshold.
    When called from echo chamber downgrade, accepts any 2+ agent subset.

    Args:
        window_id:    15-minute window ID.
        ticker:       Stock ticker symbol.
        direction:    'bullish' or 'bearish'.
        agent_scores: All available agent scores.
        echo_chamber: True if this was downgraded from P1.
        notes:        Audit notes list (mutated in place).

    Returns:
        ConcordanceResult for P2, or None if no valid pair found.
    """
    qualifying = {
        agent: score
        for agent, score in agent_scores.items()
        if score >= MIN_SCORE_P2
    }

    if len(qualifying) < 2:
        logger.debug(
            "P2 not formed for %s/%s — only %d agents with score ≥ %.1f",
            ticker, direction, len(qualifying), MIN_SCORE_P2,
        )
        return None

    # Use best 2 qualifying agents by score
    best_two = dict(sorted(qualifying.items(), key=lambda x: x[1], reverse=True)[:2])
    weighted = _weighted_score(best_two)
    verdict  = "STRONG_GO" if weighted >= STRONG_GO_THRESHOLD else "GO"

    notes.append(
        f"P2 formed — {list(best_two.keys())} | scores: {best_two} | weighted: {weighted:.1f}"
    )

    return ConcordanceResult(
        ticker          = ticker,
        direction       = direction,
        window_id       = window_id,
        pathway         = "P2",
        agent_count     = 2,
        weighted_score  = weighted,
        sizing_mult     = PATHWAY_SIZING["P2"],
        verdict         = verdict,
        agents_involved = sorted(best_two.keys()),
        scores          = best_two,
        echo_chamber    = echo_chamber,
        notes           = notes,
    )


def _weighted_score(agent_scores: dict[str, float]) -> float:
    """
    Calculate the weighted conviction score from agent submissions.

    Weights: Cipher 45% / Atlas 30% / Sage 25%.
    Normalizes when not all 3 agents are present.

    Args:
        agent_scores: Dict mapping agent name to score.

    Returns:
        Weighted score as float.
    """
    if not agent_scores:
        return 0.0

    total_weight = sum(AGENT_WEIGHTS.get(a, 0.0) for a in agent_scores)
    if total_weight == 0.0:
        return 0.0

    weighted_sum = sum(
        AGENT_WEIGHTS.get(agent, 0.0) * score
        for agent, score in agent_scores.items()
    )
    # Normalize to 0-100 range when not all agents present
    return (weighted_sum / total_weight)


def _detect_echo_chamber(agent_reasoning: dict[str, str]) -> bool:
    """
    Detect if agents are literally copying each other's analysis (true echo chamber).

    Two-stage detection:
      Stage 1 (structural) — Primary signal. If two reasoning strings are within
        10% of each other in length AND share the same first 8 words, that is a
        strong indicator of copied/templated analysis. Flags immediately.
      Stage 2 (vocabulary) — Secondary signal. If non-domain word overlap ≥80%
        across reasonings that each have ≥15 significant words. Threshold is 80%
        (not 60%) to avoid false positives from agents independently analyzing
        the same stock and naturally using the same financial vocabulary.

    Agents receive the same ORACLE-pre-warmed data from Axiom. Legitimate
    independent analysis will share domain vocabulary (ticker, sector, technical
    terms, macro context). This detector must NOT fire on vocabulary overlap alone
    — it must require structural evidence of copying.

    Note: OMNI's o3-mini does deeper echo chamber analysis during synthesis.
    This is a fast pre-filter for obvious cases only.

    Args:
        agent_reasoning: Dict mapping agent name to reasoning text.

    Returns:
        True if structural evidence of copied/echo analysis is detected.
    """
    reasonings = [r.lower() for r in agent_reasoning.values() if r and len(r) > 30]
    if len(reasonings) < 2:
        return False

    # ── Stage 1: Structural similarity (primary signal) ───────────────────────
    # If two strings have the same opening words AND similar length → likely copied
    for i in range(len(reasonings)):
        for j in range(i + 1, len(reasonings)):
            a_words = reasonings[i].split()
            b_words = reasonings[j].split()

            # Same first 8 words
            if len(a_words) >= 8 and len(b_words) >= 8:
                if a_words[:8] == b_words[:8]:
                    # Also check length similarity (within 10%)
                    longer  = max(len(reasonings[i]), len(reasonings[j]))
                    shorter = min(len(reasonings[i]), len(reasonings[j]))
                    if shorter / longer >= 0.90:
                        logger.warning(
                            "Echo chamber (structural): identical opening + similar length "
                            "between agent reasonings"
                        )
                        return True

    # ── Stage 2: Non-domain vocabulary overlap (secondary signal) ────────────
    # Expanded stopwords: generic English + comprehensive financial domain terms.
    # Any word that two agents will naturally share when analyzing the same stock
    # must be in this list so it doesn’t count as overlap evidence.
    stopwords = {
        # Generic English
        "with", "that", "this", "from", "have", "will", "been",
        "their", "which", "than", "when", "into", "also", "both",
        "about", "above", "after", "before", "being", "below",
        "could", "during", "other", "should", "these", "those",
        "through", "under", "while", "would", "there", "where",
        "given", "shows", "across",
        # Directional / technical
        "bullish", "bearish", "momentum", "support", "resistance",
        "trend", "breakout", "oversold", "overbought", "upside",
        "downside", "reversal", "consolidation", "technical",
        "moving", "average", "signal", "setup", "pattern", "channel",
        "range", "highs", "lows", "pivot", "target", "level", "levels",
        "price", "volume", "above", "below", "recent",
        # Fundamental
        "earnings", "revenue", "growth", "margin", "catalyst",
        "guidance", "estimate", "quarter", "annual", "sector",
        "industry", "analyst", "rating", "upgrade", "downgrade",
        "report", "results", "outlook", "forecast", "valuation",
        # Macro / market structure
        "rates", "yields", "inflation", "federal", "reserve",
        "economic", "volatility", "uncertainty", "regime",
        "conditions", "market", "markets", "macro", "broader",
        # Option-specific
        "strike", "expiry", "premium", "spread", "credit", "debit",
        "theta", "delta", "gamma", "vega", "contracts", "options",
        "calls", "expiration", "implied",
        # Generic finance
        "strong", "position", "entry", "trade", "stock", "equity",
        "index", "score", "conviction", "confidence", "thesis",
        "ticker", "option", "shares", "trading", "currently",
        "continue", "remains", "showing", "suggest", "suggests",
    }

    def significant_words(text: str) -> set[str]:
        """Extract non-domain content words that indicate unique analysis."""
        return {
            w for w in text.split()
            if len(w) > 4 and w.isalpha() and w not in stopwords
        }

    word_sets = [significant_words(r) for r in reasonings]

    for i in range(len(word_sets)):
        for j in range(i + 1, len(word_sets)):
            a, b = word_sets[i], word_sets[j]

            # Minimum vocabulary guard: skip if either set is too small.
            # Small sets produce false positives (e.g. 3 shared words out of 5 = 60%).
            if len(a) < 15 or len(b) < 15:
                continue

            overlap = len(a & b) / min(len(a), len(b))
            if overlap >= 0.80:  # 80% threshold (was 60% — too aggressive for domain vocab)
                logger.warning(
                    "Echo chamber (vocabulary): %.0f%% non-domain word overlap "
                    "between agent reasonings", overlap * 100
                )
                return True

    return False
