"""
synthesis.py — OMNI Synthesis Engine

Combines quad intelligence results into a final verdict.
Fully autonomous — no CONDITIONAL verdict.
Verdict ladder: STRONG_GO (4/4) → GO (3/4) → NO_GO (≤ 2/4 or insufficient brains).
Applies Axiom hard stop check before any GO verdict.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from config import (
    MIN_BRAINS_REQUIRED,
    P3_P4_MIN_VOTES_GO,
    VOTES_REQUIRED_GO,
    VOTES_REQUIRED_STRONG_GO,
)

logger = logging.getLogger("omni.synthesis")


@dataclass
class SynthesisVerdict:
    """Final OMNI verdict after quad intelligence voting."""

    verdict:              str        # STRONG_GO, GO, CONDITIONAL, NO_GO, BLOCKED
    votes_go:             int
    brains_responded:     int
    echo_chamber_flagged: bool
    axiom_blocked:        bool       # True if Axiom hard stop triggered
    axiom_hard_stops:     list[str]
    sizing_mult:          float      # final sizing including Axiom adjustment
    notes:                list[str]
    brain_summary:        dict[str, str]  # brain_name → vote label

    def can_execute(self) -> bool:
        """Return True if this verdict allows execution."""
        return self.verdict in ("STRONG_GO", "GO") and not self.axiom_blocked

    def to_dict(self) -> dict:
        return {
            "verdict":              self.verdict,
            "votes_go":             self.votes_go,
            "brains_responded":     self.brains_responded,
            "echo_chamber_flagged": self.echo_chamber_flagged,
            "axiom_blocked":        self.axiom_blocked,
            "axiom_hard_stops":     self.axiom_hard_stops,
            "sizing_mult":          round(self.sizing_mult, 2),
            "notes":                self.notes,
            "brain_summary":        self.brain_summary,
            "can_execute":          self.can_execute(),
        }


def compute_verdict(
    brain_results:         dict[str, dict],
    pathway:               str,
    concordance_sizing:    float,
    axiom_result:          Optional[dict],
) -> SynthesisVerdict:
    """
    Compute the final OMNI verdict from brain votes, pathway rules, and Axiom.

    Args:
        brain_results:       Dict of brain_name → result dict from quad_intelligence.
        pathway:             Concordance pathway (P1, P2, P3, P4).
        concordance_sizing:  Base sizing multiplier from concordance pathway.
        axiom_result:        Axiom risk assessment dict or None.

    Returns:
        SynthesisVerdict with final verdict and execution permissions.
    """
    notes:         list[str] = []
    brain_summary: dict[str, str] = {}

    # Count valid responses and GO votes
    votes_go         = 0
    brains_responded = 0
    echo_detections  = 0

    for brain_name, result in brain_results.items():
        if "error" in result and result["error"]:
            brain_summary[brain_name] = f"ERROR: {result['error'][:50]}"
            continue

        brains_responded += 1
        vote = result.get("vote", "NO_GO")
        brain_summary[brain_name] = vote

        if vote == "GO":
            votes_go += 1
        elif vote == "CONDITIONAL":
            # Brain CONDITIONAL = not GO. System is fully autonomous — no escalation.
            notes.append(f"{brain_name} voted CONDITIONAL (counted as NO_GO)")

        if result.get("echo_chamber"):
            echo_detections += 1
            logger.warning("Echo chamber flagged by brain: %s", brain_name)

    echo_chamber_flagged = echo_detections >= 1  # any brain detection triggers flag

    # ── Axiom Hard Stop Check ─────────────────────────────────────────────────
    axiom_blocked   = False
    axiom_hard_stops: list[str] = []
    axiom_sizing    = 1.0

    if axiom_result:
        hard_stops = axiom_result.get("hard_stops", [])
        if hard_stops:
            axiom_blocked    = True
            axiom_hard_stops = hard_stops
            notes.append(f"AXIOM HARD STOP: {'; '.join(hard_stops)}")
            logger.warning("Axiom hard stop triggered: %s", hard_stops)

        raw_sizing = axiom_result.get("sizing_mult", 1.0)
        axiom_sizing = float(raw_sizing) if raw_sizing is not None else 1.0

    # Final sizing = concordance pathway sizing × Axiom sizing adjustment
    final_sizing = round(concordance_sizing * axiom_sizing, 2)

    # ── Insufficient Brain Responses → CONDITIONAL ───────────────────────────
    # CONDITIONAL = no execution, but a named signal (not silent like NO_GO).
    # Fires when the quad intelligence layer itself is degraded.
    # main.py sends Ahmed an alert on CONDITIONAL so he knows the AI layer is impaired.
    if brains_responded < MIN_BRAINS_REQUIRED:
        notes.append(
            f"Only {brains_responded}/{len(brain_results)} brains responded "
            f"(minimum {MIN_BRAINS_REQUIRED} required) — CONDITIONAL (system degraded)"
        )
        return SynthesisVerdict(
            verdict              = "CONDITIONAL",
            votes_go             = votes_go,
            brains_responded     = brains_responded,
            echo_chamber_flagged = echo_chamber_flagged,
            axiom_blocked        = axiom_blocked,
            axiom_hard_stops     = axiom_hard_stops,
            sizing_mult          = 0.0,
            notes                = notes,
            brain_summary        = brain_summary,
        )

    # ── Axiom blocked — verdict is BLOCKED regardless of votes ────────────────
    if axiom_blocked:
        return SynthesisVerdict(
            verdict              = "NO_GO",
            votes_go             = votes_go,
            brains_responded     = brains_responded,
            echo_chamber_flagged = echo_chamber_flagged,
            axiom_blocked        = True,
            axiom_hard_stops     = axiom_hard_stops,
            sizing_mult          = 0.0,
            notes                = notes,
            brain_summary        = brain_summary,
        )

    # ── P3 / P4 require minimum 3/4 GO ───────────────────────────────────────
    if pathway in ("P3", "P4") and votes_go < P3_P4_MIN_VOTES_GO:
        notes.append(
            f"P3/P4 requires {P3_P4_MIN_VOTES_GO}/4 GO votes — "
            f"only {votes_go} received"
        )
        return SynthesisVerdict(
            verdict              = "NO_GO",
            votes_go             = votes_go,
            brains_responded     = brains_responded,
            echo_chamber_flagged = echo_chamber_flagged,
            axiom_blocked        = False,
            axiom_hard_stops     = axiom_hard_stops,
            sizing_mult          = 0.0,
            notes                = notes,
            brain_summary        = brain_summary,
        )

    # ── Standard Voting ───────────────────────────────────────────────────────
    if votes_go >= VOTES_REQUIRED_STRONG_GO:
        verdict = "STRONG_GO"
        notes.append(f"{votes_go}/{brains_responded} brains GO — STRONG_GO")  # Cipher Finding 17: use variables

    elif votes_go >= VOTES_REQUIRED_GO:
        verdict = "GO"
        notes.append(f"3/4 brains GO")

    elif votes_go == 2:
        # 2/4 GO — borderline, not enough to execute, but named signal (not silent)
        verdict = "CONDITIONAL"
        notes.append(f"2/4 brains GO — CONDITIONAL (borderline, minimum 3 required for GO)")
    else:  # 0 or 1 GO — clear no-conviction, silent drop
        verdict = "NO_GO"
        notes.append(f"Only {votes_go}/4 brains GO — NO_GO")

    # Echo chamber downgrade: STRONG_GO → GO
    if echo_chamber_flagged and verdict == "STRONG_GO":
        verdict = "GO"
        notes.append("Echo chamber detected — downgraded STRONG_GO → GO")

    return SynthesisVerdict(
        verdict              = verdict,
        votes_go             = votes_go,
        brains_responded     = brains_responded,
        echo_chamber_flagged = echo_chamber_flagged,
        axiom_blocked        = False,
        axiom_hard_stops     = axiom_hard_stops,
        sizing_mult          = final_sizing,
        notes                = notes,
        brain_summary        = brain_summary,
    )


def build_context(
    concordance:   dict,
    axiom_result:  Optional[dict],
    regime:        Optional[dict],
) -> dict:
    """
    Build the complete context dict sent to all 4 brains.

    Every brain receives identical context — no brain gets extra information.

    Args:
        concordance:  Concordance payload from Alpha or Prime buffer.
        axiom_result: Axiom risk assessment for the ticker.
        regime:       Current market regime from Axiom.

    Returns:
        Complete context dict for brain prompts.
    """
    return {
        "ticker":             concordance.get("ticker"),
        "direction":          concordance.get("direction"),
        "system":             concordance.get("system"),
        "pathway":            concordance.get("pathway"),
        "agent_weighted_score": concordance.get("weighted_score"),
        "agents_involved":    concordance.get("agents_involved", []),
        "agent_scores":       concordance.get("scores", {}),
        "verdict_from_agents": concordance.get("verdict"),
        "echo_chamber_flagged_at_buffer": concordance.get("echo_chamber", False),
        "window_id":          concordance.get("window_id"),
        "notes_from_buffer":  concordance.get("notes", []),
        "axiom": {
            "risk_score":   axiom_result.get("risk_score") if axiom_result else None,
            "sizing_mult":  axiom_result.get("sizing_mult") if axiom_result else None,
            "in_pool":      axiom_result.get("in_pool") if axiom_result else None,
            "concern_1":    axiom_result.get("concern_1") if axiom_result else None,
            "concern_2":    axiom_result.get("concern_2") if axiom_result else None,
            "hard_stops":   axiom_result.get("hard_stops", []) if axiom_result else [],
        },
        "regime": {
            "classification":       regime.get("classification") if regime else "UNKNOWN",
            "vix":                  regime.get("vix") if regime else None,
            "strategy_bias":        regime.get("strategy_bias") if regime else None,
            # Cipher Finding 13 fix: default to False (conservative) when regime is
            # None — brains must not see "all systems go" during Axiom/regime failure.
            # Note: Axiom hard stop provides a separate execution gate, but brain
            # context should also be conservative to avoid skewed GO votes.
            "alpha_debit_allowed":  regime.get("alpha_debit_allowed") if regime else False,
            "alpha_credit_allowed": regime.get("alpha_credit_allowed") if regime else False,
            "prime_allowed":        regime.get("prime_allowed") if regime else False,
        },
        "performance_targets": {
            "win_rate_target":     0.75,
            "avg_win_pct_target":  0.40,
            "loss_rate_ceiling":   0.25,
            "avg_loss_pct_ceiling": 0.20,
        },
    }
