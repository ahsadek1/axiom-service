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
        """Run Druckenmiller macro/liquidity analysis against current market data.

        Args:
            oracle_data: Current market snapshot from ORACLE.
            news: List of recent news headlines relevant to the trade thesis.

        Returns:
            FrameworkAnalysis with gate_type="HARD".  FAIL is a hard veto.
        """
        try:
            prompt = self._build_prompt(oracle_data, news)
            response = await self._client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text: str = response.content[0].text
            return self._parse_response(raw_text)
        except Exception as exc:
            logger.error(
                "DruckenmillerFramework.analyze() encountered an error: %s", exc,
                exc_info=True,
            )
            return FrameworkAnalysis(
                name="Druckenmiller",
                gate_type="HARD",
                gate_result=GateResult.FAIL,
                confidence_adjustment=0,
                size_veto=False,
                is_veto=True,
                analysis=f"Analysis unavailable due to error: {exc}",
                key_signal="Error — defaulting to FAIL (conservative)",
                signals={},
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
