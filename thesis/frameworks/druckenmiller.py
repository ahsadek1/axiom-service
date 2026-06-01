"""
THESIS — Druckenmiller Framework (HARD GATE).

Stanley Druckenmiller's macro/liquidity framework.  A FAIL result is an
unconditional NO-GO regardless of all other framework signals.

Key signals evaluated:
  - Fed direction (hiking / cutting / pausing)
  - Liquidity expansion or contraction
  - HY credit spread (> 600 bp = danger)
  - Yield curve shape (inversion = stress)
  - Dollar strength (DXY > 106 = headwind for risk assets)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

import anthropic

from models import FrameworkAnalysis, GateResult, OracleData

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are Stanley Druckenmiller's macro/liquidity analytical engine embedded inside
the THESIS trading intelligence service.

Your sole purpose is to evaluate whether current macro-liquidity conditions
constitute a PASS or FAIL for risk-asset exposure.

Druckenmiller's philosophy:
- Liquidity is the primary driver of asset prices; Fed policy direction matters more
  than the absolute level of rates.
- HY credit spreads above 600 bp signal systemic stress — avoid risk.
- An inverted yield curve (2Y > 10Y) signals a recessionary environment.
- A strong dollar (DXY > 106) acts as a headwind for global risk assets and
  commodities.
- When macro conditions are hostile, no individual trade thesis can overcome the tide.

Respond ONLY with a single JSON object — no markdown fences, no commentary.
"""


class DruckenmillerFramework:
    """Hard-gate macro/liquidity framework based on Stanley Druckenmiller's approach.

    A FAIL result acts as an unconditional veto: no trade should proceed
    regardless of the signals from the four remaining frameworks.
    """

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", "")
        )

    async def analyze(
        self, oracle_data: OracleData, news: List[str]
    ) -> FrameworkAnalysis:
        """
        DETERMINISTIC Druckenmiller macro/liquidity gate.
        Ahmed directive 2026-05-26: replace Claude Opus with explicit rules.
        AI in hard execution path = guaranteed blocking. No more.

        Rules (Druckenmiller's actual published criteria):
          HY spread  > 600 bp  → FAIL (systemic stress)
          VIX        > 30      → FAIL (fear regime)
          Both inverted + VIX > 22 → FAIL (dual hostile signal)
          Default              → PASS
        """
        vix       = oracle_data.vix_level or 0.0
        hy_spread = oracle_data.hy_spread or 0.0
        dxy       = oracle_data.dxy or 0.0
        inverted  = oracle_data.yield_curve_inverted

        # Hard FAIL conditions
        fail_reason = None
        if hy_spread > 600:
            fail_reason = f"HY spread {hy_spread:.0f}bp > 600bp — systemic stress"
        elif vix > 30:
            fail_reason = f"VIX {vix:.1f} > 30 — fear regime"
        elif inverted and vix > 22:
            fail_reason = f"Yield curve inverted + VIX {vix:.1f} > 22 — dual hostile signal"

        if fail_reason:
            logger.info("[DRUCKENMILLER] FAIL — %s", fail_reason)
            return FrameworkAnalysis(
                name="Druckenmiller",
                gate_type="HARD",
                gate_result=GateResult.FAIL,
                confidence_adjustment=0,
                size_veto=False,
                is_veto=True,
                analysis=fail_reason,
                key_signal=fail_reason,
                signals={
                    "vix": vix, "hy_spread": hy_spread,
                    "dxy": dxy, "yield_curve_inverted": inverted,
                },
            )

        # PASS — build analysis summary
        signals_summary = (
            f"VIX={vix:.1f} HY={hy_spread:.0f}bp "
            f"DXY={dxy:.1f} inverted={inverted}"
        )
        # Soft warnings (not blocking)
        warnings = []
        if dxy > 106:
            warnings.append(f"DXY {dxy:.1f} > 106 (dollar headwind)")
        if inverted:
            warnings.append("yield curve inverted (monitor)")
        if hy_spread > 400:
            warnings.append(f"HY spread {hy_spread:.0f}bp elevated")

        analysis = f"Macro conditions PASS. {signals_summary}."
        if warnings:
            analysis += f" Soft warnings: {'; '.join(warnings)}."

        logger.info("[DRUCKENMILLER] PASS — %s", signals_summary)
        return FrameworkAnalysis(
            name="Druckenmiller",
            gate_type="HARD",
            gate_result=GateResult.PASS,
            confidence_adjustment=-5 if warnings else 0,
            size_veto=False,
            is_veto=False,
            analysis=analysis,
            key_signal=signals_summary,
            signals={
                "vix": vix, "hy_spread": hy_spread,
                "dxy": dxy, "yield_curve_inverted": inverted,
                "warnings": warnings,
            },
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, oracle_data: OracleData, news: List[str]) -> str:
        """Construct the Claude prompt for Druckenmiller analysis.

        Args:
            oracle_data: Current market snapshot.
            news: Recent news headlines.

        Returns:
            Fully formatted prompt string.
        """
        market_json = oracle_data.model_dump_json(indent=2)
        news_block = "\n".join(f"  - {h}" for h in news) if news else "  (none)"

        return f"""{_SYSTEM_PROMPT}

## Current Market Data (ORACLE snapshot)
```json
{market_json}
```

## Recent News Headlines
{news_block}

## Your Task
Evaluate whether current macro-liquidity conditions PASS or FAIL the
Druckenmiller hard gate.  Consider each of the following signals explicitly:

1. **Fed direction** — hiking / cutting / pausing and what it implies for liquidity.
2. **Liquidity** — is the system expanding or contracting? (use yield curve, spreads, DXY).
3. **HY credit spread** — current level vs. 600 bp danger threshold.
4. **Yield curve** — is it inverted (2Y > 10Y)?  Severity of inversion.
5. **Dollar (DXY)** — is it above 106?  What does that imply for risk assets?

Respond with ONLY the following JSON (no markdown, no explanation outside the JSON):
{{
  "gate": "PASS" | "FAIL",
  "analysis": "<2-4 sentence synthesis of the macro-liquidity environment>",
  "key_signal": "<single most important signal driving the gate decision>",
  "signals": {{
    "fed_direction": "<hiking|cutting|pausing>",
    "liquidity": "<expanding|contracting|neutral>",
    "hy_spread_danger": <true|false>,
    "yield_curve_inverted": <true|false>,
    "dxy_headwind": <true|false>
  }}
}}
"""

    def _parse_response(self, raw_text: str) -> FrameworkAnalysis:
        """Parse Claude's JSON response into a FrameworkAnalysis.

        Falls back to a conservative FAIL on any parse error.

        Args:
            raw_text: The raw text returned by Claude.

        Returns:
            FrameworkAnalysis populated from the parsed JSON.
        """
        try:
            data: Dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError:
            # Attempt to extract JSON substring (Claude sometimes adds preamble)
            try:
                start = raw_text.index("{")
                end = raw_text.rindex("}") + 1
                data = json.loads(raw_text[start:end])
            except (ValueError, json.JSONDecodeError) as parse_exc:
                logger.error(
                    "DruckenmillerFramework failed to parse Claude response: %s\n"
                    "Raw text: %s",
                    parse_exc,
                    raw_text[:500],
                )
                return FrameworkAnalysis(
                    name="Druckenmiller",
                    gate_type="HARD",
                    gate_result=GateResult.FAIL,
                    confidence_adjustment=0,
                    size_veto=False,
                    is_veto=True,
                    analysis="JSON parse error — defaulting to FAIL (conservative).",
                    key_signal="Parse error",
                    signals={},
                )

        gate_str = str(data.get("gate", "FAIL")).upper()
        gate_result = GateResult.PASS if gate_str == "PASS" else GateResult.FAIL
        is_veto = gate_result == GateResult.FAIL

        return FrameworkAnalysis(
            name="Druckenmiller",
            gate_type="HARD",
            gate_result=gate_result,
            confidence_adjustment=0,
            size_veto=False,
            is_veto=is_veto,
            analysis=data.get("analysis", ""),
            key_signal=data.get("key_signal", ""),
            signals=data.get("signals", {}),
        )
