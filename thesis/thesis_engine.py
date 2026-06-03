"""
ThesisEngine — Orchestrates the five trading frameworks and conflict resolution.

Runs all frameworks concurrently, applies the hard-gate hierarchy and soft-gate
sizing matrix, and produces a final ConflictResolution that drives posture and
position sizing decisions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from models import (
    ConflictResolution,
    FrameworkAnalysis,
    GateResult,
    OracleData,
    TradingPosture,
)
from frameworks.druckenmiller import DruckenmillerFramework
from frameworks.jones import JonesFramework
from frameworks.soros import SorosFramework
from frameworks.cohen import CohenFramework
from frameworks.buffett import BuffettFramework
from clients.oracle_client import OracleClient
from clients.brave_client import BraveClient
from clients.chronicle_client import ChronicleClient
from clients.sovereign_client import SovereignClient

logger = logging.getLogger(__name__)

_BASE_CONFIDENCE = 50  # Starting confidence before soft-gate adjustments


class ThesisEngine:
    """Central orchestrator for the THESIS five-framework analysis pipeline.

    Args:
        oracle: ORACLE market data client.
        brave: Brave Search client for web intelligence.
        chronicle: CHRONICLE SQLite client.
        sovereign: SOVEREIGN message bus client.
    """

    def __init__(
        self,
        oracle: OracleClient,
        brave: BraveClient,
        chronicle: ChronicleClient,
        sovereign: SovereignClient,
    ) -> None:
        """Initialise engine with shared clients and instantiate all frameworks."""
        self.oracle = oracle
        self.brave = brave
        self.chronicle = chronicle
        self.sovereign = sovereign

        self.druckenmiller = DruckenmillerFramework()
        self.jones = JonesFramework()
        self.soros = SorosFramework()
        self.cohen = CohenFramework()
        self.buffett = BuffettFramework()

    async def run_all_frameworks(
        self, oracle_data: OracleData, news: List[str]
    ) -> List[FrameworkAnalysis]:
        """Run all five frameworks concurrently.

        Each framework receives the same oracle_data and news snapshot.
        Framework errors are caught internally — this method always returns
        a list of exactly five FrameworkAnalysis objects.

        Args:
            oracle_data: Current market snapshot.
            news: Recent news headlines.

        Returns:
            List of five FrameworkAnalysis objects in order:
            [Druckenmiller, Jones, Soros, Cohen, Buffett].
        """
        # B3 — Per-framework 90s timeout guard (THESIS_RESILIENCE_SPEC v1.0)
        # Each framework gets a hard 90s deadline. Timeout returns a DEGRADED
        # FrameworkAnalysis so gather always returns exactly 5 results.
        FRAMEWORK_TIMEOUT = 90

        async def _run_with_timeout(framework, name: str) -> FrameworkAnalysis:
            try:
                return await asyncio.wait_for(
                    framework.analyze(oracle_data, news),
                    timeout=FRAMEWORK_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.error("Framework %s timed out after %ds", name, FRAMEWORK_TIMEOUT)
                return FrameworkAnalysis(
                    name=name,
                    gate_type="HARD",
                    gate_result=GateResult.FAIL,
                    confidence_adjustment=0,
                    analysis=f"[DEGRADED] Framework timed out after {FRAMEWORK_TIMEOUT}s",
                    size_veto=True,
                    is_veto=True,
                )
            except Exception as e:
                logger.error("Framework %s raised: %s", name, e)
                return FrameworkAnalysis(
                    name=name,
                    gate_type="HARD",
                    gate_result=GateResult.FAIL,
                    confidence_adjustment=0,
                    analysis=f"[DEGRADED] Framework error: {str(e)[:200]}",
                    size_veto=True,
                    is_veto=True,
                )

        results = await asyncio.gather(
            _run_with_timeout(self.druckenmiller, "druckenmiller"),
            _run_with_timeout(self.jones, "jones"),
            _run_with_timeout(self.soros, "soros"),
            _run_with_timeout(self.cohen, "cohen"),
            _run_with_timeout(self.buffett, "buffett"),
        )

        # If Druckenmiller (Level 1 gate) is degraded → log critical warning
        if results[0].analysis.startswith("[DEGRADED]"):
            logger.error("CRITICAL: Druckenmiller framework degraded — thesis will emit NEUTRAL posture")
        # If >= 3 frameworks degraded → log for SOVEREIGN alert
        degraded_count = sum(1 for r in results if r.analysis.startswith("[DEGRADED]"))
        if degraded_count >= 3:
            logger.error(
                "CRITICAL: %d/5 frameworks degraded — thesis synthesis unreliable", degraded_count
            )

        return list(results)

    def resolve_conflict(
        self, analyses: List[FrameworkAnalysis]
    ) -> ConflictResolution:
        """Apply the framework hierarchy to produce a final trading verdict.

        Hierarchy:
          Level 1 — Druckenmiller macro gate (HARD, absolute veto on FAIL)
          Level 2 — Jones risk/reward gate (HARD, NO-GO on FAIL)
          Levels 3-5 — Soros/Cohen/Buffett soft gates (confidence + size)

        Sizing matrix (soft vetoes):
          0 vetoes → 1.0x
          1 veto   → 0.75x
          2 vetoes → 0.50x
          3 vetoes → 0.25x
          3+ vetoes AND net confidence < -30 → is_go=False

        Size multiplier is further reduced by Cohen/Buffett size_veto flags.
        Prosecutor/Defender runs when is_go=True AND final_confidence > 85.

        Args:
            analyses: List of five FrameworkAnalysis objects.

        Returns:
            ConflictResolution with final verdict.
        """
        # Index frameworks by name for clarity
        by_name = {a.name: a for a in analyses}
        druckenmiller = by_name.get("Druckenmiller")
        jones = by_name.get("Jones")

        # ---- Hard gates ------------------------------------------------
        macro_gate = (
            druckenmiller.gate_result
            if druckenmiller and druckenmiller.gate_result
            else GateResult.FAIL
        )
        rr_gate = (
            jones.gate_result
            if jones and jones.gate_result
            else GateResult.FAIL
        )

        if macro_gate == GateResult.FAIL:
            return ConflictResolution(
                macro_gate=macro_gate,
                risk_reward_gate=rr_gate,
                soft_veto_count=0,
                confidence_adjustment=0,
                final_confidence=0,
                size_multiplier=0.0,
                is_go=False,
                no_go_reason="Druckenmiller macro gate FAILED — unconditional NO-GO.",
                verdict="NO-GO: macro/liquidity environment is hostile.",
                run_prosecutor_defender=False,
            )

        if rr_gate == GateResult.FAIL:
            return ConflictResolution(
                macro_gate=macro_gate,
                risk_reward_gate=rr_gate,
                soft_veto_count=0,
                confidence_adjustment=0,
                final_confidence=0,
                size_multiplier=0.0,
                is_go=False,
                no_go_reason="Jones risk/reward gate FAILED — wait for better entry.",
                verdict="NO-GO: risk/reward conditions do not meet minimum 3:1 threshold.",
                run_prosecutor_defender=False,
            )

        # ---- Soft gates ------------------------------------------------
        soft_analyses = [
            a for a in analyses if a.gate_type == "SOFT"
        ]

        soft_veto_count = sum(1 for a in soft_analyses if a.is_veto)
        net_confidence_adj = sum(a.confidence_adjustment for a in soft_analyses)
        final_confidence = max(0, min(100, _BASE_CONFIDENCE + net_confidence_adj))

        # Sizing matrix
        if soft_veto_count == 0:
            size_multiplier = 1.0
        elif soft_veto_count == 1:
            size_multiplier = 0.75
        elif soft_veto_count == 2:
            size_multiplier = 0.50
        else:
            size_multiplier = 0.25

        # Additional size reductions from Cohen/Buffett size vetoes
        for a in soft_analyses:
            if a.size_veto:
                size_adj = a.signals.get("size_adjustment", 0.0)
                if isinstance(size_adj, (int, float)) and size_adj < 0:
                    size_multiplier = max(0.1, size_multiplier + size_adj)

        size_multiplier = round(size_multiplier, 2)

        # 3+ vetoes + confidence < -30 → absolute NO-GO
        is_go = True
        no_go_reason: Optional[str] = None
        if soft_veto_count >= 3 and net_confidence_adj < -30:
            is_go = False
            no_go_reason = (
                f"{soft_veto_count} soft vetoes + confidence adjustment "
                f"{net_confidence_adj} below -30 threshold."
            )

        # Prosecutor/Defender trigger
        run_pd = is_go and final_confidence > 85
        prosecutor: Optional[str] = None
        defender: Optional[str] = None
        if run_pd:
            sorted_by_adj = sorted(
                soft_analyses, key=lambda a: a.confidence_adjustment, reverse=True
            )
            if sorted_by_adj:
                prosecutor = sorted_by_adj[0].name
                defender = sorted_by_adj[-1].name

        verdict = self._build_verdict(
            is_go, soft_veto_count, final_confidence, size_multiplier, no_go_reason
        )

        return ConflictResolution(
            macro_gate=macro_gate,
            risk_reward_gate=rr_gate,
            soft_veto_count=soft_veto_count,
            confidence_adjustment=net_confidence_adj,
            final_confidence=final_confidence,
            size_multiplier=size_multiplier,
            is_go=is_go,
            no_go_reason=no_go_reason,
            verdict=verdict,
            run_prosecutor_defender=run_pd,
            prosecutor_framework=prosecutor,
            defender_framework=defender,
        )

    def determine_posture(
        self,
        resolution: ConflictResolution,
        analyses: List[FrameworkAnalysis],
    ) -> TradingPosture:
        """Map conflict resolution outcome to a trading posture.

        Args:
            resolution: Completed conflict resolution.
            analyses: All five framework analyses.

        Returns:
            TradingPosture enum value.
        """
        if not resolution.is_go:
            return TradingPosture.DEFENSIVE

        conf = resolution.final_confidence
        size = resolution.size_multiplier

        if conf >= 65 and size >= 1.0:
            return TradingPosture.AGGRESSIVE
        elif conf >= 55 and size >= 0.75:
            return TradingPosture.NEUTRAL
        elif size <= 0.50:
            return TradingPosture.SELECTIVE
        else:
            return TradingPosture.NEUTRAL

    def determine_primary_authority(
        self,
        oracle_data: OracleData,
        resolution: ConflictResolution,
    ) -> str:
        """Select the primary authority framework based on market environment.

        Uses VIX level and yield curve to infer which framework's reasoning
        should be most weighted in the written thesis narrative.

        Args:
            oracle_data: Current market data snapshot.
            resolution: Completed conflict resolution.

        Returns:
            Framework name string (lowercase).
        """
        vix = oracle_data.vix_level or 0.0
        inverted = oracle_data.yield_curve_inverted

        if inverted or (oracle_data.hy_spread and oracle_data.hy_spread > 500):
            return "druckenmiller"
        if vix > 30:
            return "jones"
        if vix < 14:
            return "buffett"
        if vix >= 20:
            return "cohen"
        return "equal"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_verdict(
        is_go: bool,
        soft_vetoes: int,
        confidence: int,
        size: float,
        no_go_reason: Optional[str],
    ) -> str:
        """Construct a human-readable verdict string.

        Args:
            is_go: Whether a trade is cleared.
            soft_vetoes: Number of soft-gate vetoes.
            confidence: Final confidence score (0-100).
            size: Final size multiplier (0.0-1.0).
            no_go_reason: Reason string if is_go is False.

        Returns:
            One-sentence verdict.
        """
        if not is_go:
            return f"NO-GO: {no_go_reason}"
        pct = int(size * 100)
        return (
            f"GO at {pct}% size — confidence {confidence}/100 — "
            f"{soft_vetoes} soft veto(s)."
        )
