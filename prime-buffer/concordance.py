"""
concordance.py — Prime Concordance Engine

Core concordance evaluation for swing equity (R2 agent submissions).
Same pathway logic as Alpha; higher conviction thresholds.

Thresholds (Prime-specific):
  Submission minimum: 63 (vs 58 for Alpha)
  P1 GO floor:        70 (vs 65 for Alpha)
  P2 per-agent min:   78 (same)
  P3 solo min:        90 (same)

System label: 'prime' — included in all concordance result dicts sent to OMNI.
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

logger = logging.getLogger("prime_buffer.concordance")


@dataclass
class ConcordanceResult:
    """Outcome of a Prime concordance evaluation."""

    ticker:          str
    direction:       str
    window_id:       str
    pathway:         str
    agent_count:     int
    weighted_score:  float
    sizing_mult:     float
    verdict:         str
    agents_involved: list[str]
    scores:          dict[str, float]
    echo_chamber:    bool
    notes:           list[str]

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
            "system":          "prime",
        }


def evaluate_concordance(
    window_id:            str,
    ticker:               str,
    direction:            str,
    submissions:          list[dict],
    solo_entries_enabled: bool,
) -> Optional[ConcordanceResult]:
    """
    Evaluate concordance for a ticker/direction/window (Prime swing equity).

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

    agent_scores: dict[str, float] = {}
    agent_reasoning: dict[str, str] = {}
    for sub in submissions:
        agent = sub["agent"]
        if agent in VALID_AGENTS:
            agent_scores[agent]    = float(sub["score"])
            agent_reasoning[agent] = sub.get("reasoning") or ""

    participating_agents = set(agent_scores.keys())
    count                = len(participating_agents)
    notes: list[str]     = []

    # ── P1: All 3 agents agree ────────────────────────────────────────────────
    if participating_agents == VALID_AGENTS:
        weighted = _weighted_score(agent_scores)
        echo     = _detect_echo_chamber(agent_reasoning)

        if echo:
            notes.append("Echo chamber detected — P1 downgraded to P2")
            logger.warning("Echo chamber on %s/%s (Prime) — downgrading P1 → P2", ticker, direction)
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
                "P1 failed for %s/%s (Prime, score %.1f < %.1f) — attempting P2",
                ticker, direction, weighted, GO_THRESHOLD_P1,
            )
            # Fall through: try P2 with any qualifying 2-agent subset

    # ── P2: Any 2 agents both meet individual threshold ───────────────────────
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

    Finds the best qualifying 2-agent pair where both scores meet MIN_SCORE_P2.

    Args:
        window_id:    15-minute window ID.
        ticker:       Stock ticker symbol.
        direction:    'bullish' or 'bearish'.
        agent_scores: All available agent scores.
        echo_chamber: True if downgraded from P1.
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
            "P2 not formed for %s/%s (Prime) — only %d agents with score ≥ %.1f",
            ticker, direction, len(qualifying), MIN_SCORE_P2,
        )
        return None

    best_two = dict(sorted(qualifying.items(), key=lambda x: x[1], reverse=True)[:2])
    weighted = _weighted_score(best_two)
    verdict  = "STRONG_GO" if weighted >= STRONG_GO_THRESHOLD else "GO"

    notes.append(
        f"P2 formed (Prime) — {list(best_two.keys())} | scores: {best_two} | weighted: {weighted:.1f}"
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
    Calculate weighted conviction score. Normalizes for subsets of agents.

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
        AGENT_WEIGHTS.get(a, 0.0) * s
        for a, s in agent_scores.items()
    )
    return weighted_sum / total_weight


def _detect_echo_chamber(agent_reasoning: dict[str, str]) -> bool:
    """
    Detect potential echo chamber via reasoning overlap heuristic.

    Args:
        agent_reasoning: Dict mapping agent name to reasoning text.

    Returns:
        True if ≥ 60% word overlap found between any two agent reasonings.
    """
    reasonings = [r.lower() for r in agent_reasoning.values() if r and len(r) > 30]
    if len(reasonings) < 2:
        return False

    stopwords = {
        "with", "that", "this", "from", "have", "will", "been",
        "their", "which", "than", "when", "into", "also", "both",
        "stock", "trade", "ticker", "bullish", "bearish", "option",
        "swing", "equity", "position", "entry", "setup", "trend",
    }

    def significant_words(text: str) -> set[str]:
        return {
            w for w in text.split()
            if len(w) > 4 and w.isalpha() and w not in stopwords
        }

    word_sets = [significant_words(r) for r in reasonings]
    for i in range(len(word_sets)):
        for j in range(i + 1, len(word_sets)):
            a, b = word_sets[i], word_sets[j]
            if not a or not b:
                continue
            overlap = len(a & b) / min(len(a), len(b))
            if overlap >= 0.60:
                logger.warning(
                    "Echo chamber (Prime): %.0f%% word overlap between agent reasonings",
                    overlap * 100,
                )
                return True

    return False
