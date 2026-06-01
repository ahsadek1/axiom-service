"""
THESIS — Cohen Framework (SOFT GATE).

Steve Cohen's positioning/flow framework.  Reads the tape for institutional
footprints: put/call ratio, AAII sentiment extremes, and VIX regime.

Soft gate: adjusts confidence AND can apply a size reduction.
  Confirming:     +10 confidence,   0% size reduction
  Neutral:          0 confidence,   0% size reduction
  Contradicting:  -15 confidence, -25% size reduction
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

import anthropic

from models import FrameworkAnalysis, OracleData

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are Steve Cohen's positioning/flow analytical engine embedded inside
the THESIS trading intelligence service.

Cohen's philosophy:
- Every price movement contains information about who is in control.
- Institutional footprints are visible if you know where to look:
  put/call ratio extremes, AAII sentiment extremes, unusual volume.
- The tape tells you whether buyers or sellers are in control right now.
- Act decisively on incomplete information when the pattern is clear.
- When flow contradicts price action, trust the flow.

Respond ONLY with a single JSON object — no markdown fences, no commentary.
"""


class CohenFramework:
    """Soft-gate positioning/flow framework based on Steve Cohen's approach.

    Can reduce position size by 25% when institutional flow contradicts
    the proposed trade direction.
    """

    def __init__(self) -> None:
        """Initialize the Anthropic async client from environment."""
        self._client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", "")
        )

    async def analyze(
        self, oracle_data: OracleData, news: List[str]
    ) -> FrameworkAnalysis:
        """Run Cohen positioning/flow analysis.

        Args:
            oracle_data: Current market snapshot from ORACLE.
            news: Recent news headlines.

        Returns:
            FrameworkAnalysis with gate_type="SOFT"; size_veto=True when
            flow contradicts the proposed direction.
        """
        try:
            prompt = self._build_prompt(oracle_data, news)
            response = await self._client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_response(response.content[0].text)
        except Exception as exc:
            logger.error(
                "CohenFramework.analyze() error: %s", exc, exc_info=True
            )
            return FrameworkAnalysis(
                name="Cohen",
                gate_type="SOFT",
                gate_result=None,
                confidence_adjustment=0,
                size_veto=False,
                is_veto=False,
                analysis=f"Analysis unavailable: {exc}",
                key_signal="Error — neutral posture applied",
                signals={},
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, oracle_data: OracleData, news: List[str]) -> str:
        """Construct the Cohen positioning/flow prompt.

        Args:
            oracle_data: Current market snapshot.
            news: Recent headlines.

        Returns:
            Fully formatted prompt string.
        """
        market_json = oracle_data.model_dump_json(indent=2)
        news_block = "\n".join(f"  - {h}" for h in news) if news else "  (none)"

        return f"""{_SYSTEM_PROMPT}

## Current Market Data
```json
{market_json}
```

## Recent News
{news_block}

## Your Task
Evaluate institutional positioning and tape quality. Consider:
1. **Put/Call ratio** — elevated (fear, potential contrarian buy) or low (complacency)?
2. **AAII sentiment** — extreme bullish or bearish readings are contrarian signals.
3. **VIX level** — above 20 indicates institutional hedging activity (defensive flow).
4. **Volume confirmation** — is price action backed by volume or is it thin?
5. **Flow direction** — are institutions positioning with or against the market trend?

Respond ONLY with this JSON:
{{
  "flow_direction": "CONFIRMING" | "NEUTRAL" | "CONTRADICTING",
  "confidence_adjustment": 10 | 0 | -15,
  "size_adjustment": 0.0 | -0.25,
  "analysis": "<2-4 sentence synthesis>",
  "key_signal": "<single most important positioning signal>"
}}

Rules:
  CONFIRMING    → confidence_adjustment=+10, size_adjustment=0.0
  NEUTRAL       → confidence_adjustment=0,   size_adjustment=0.0
  CONTRADICTING → confidence_adjustment=-15, size_adjustment=-0.25
"""

    def _parse_response(self, raw_text: str) -> FrameworkAnalysis:
        """Parse Claude's JSON response.

        Falls back to a neutral result on parse error.

        Args:
            raw_text: Raw text from Claude.

        Returns:
            FrameworkAnalysis populated from parsed JSON.
        """
        try:
            data: Dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError:
            try:
                start = raw_text.index("{")
                end = raw_text.rindex("}") + 1
                data = json.loads(raw_text[start:end])
            except (ValueError, json.JSONDecodeError) as exc:
                logger.error(
                    "CohenFramework failed to parse response: %s\nRaw: %s",
                    exc, raw_text[:500],
                )
                return FrameworkAnalysis(
                    name="Cohen",
                    gate_type="SOFT",
                    gate_result=None,
                    confidence_adjustment=0,
                    size_veto=False,
                    is_veto=False,
                    analysis="JSON parse error — neutral applied.",
                    key_signal="Parse error",
                    signals={},
                )

        flow = str(data.get("flow_direction", "NEUTRAL")).upper()
        if flow == "CONFIRMING":
            adj, size_adj, is_veto, size_veto = 10, 0.0, False, False
        elif flow == "CONTRADICTING":
            adj, size_adj, is_veto, size_veto = -15, -0.25, True, True
        else:
            adj, size_adj, is_veto, size_veto = 0, 0.0, False, False

        return FrameworkAnalysis(
            name="Cohen",
            gate_type="SOFT",
            gate_result=None,
            confidence_adjustment=adj,
            size_veto=size_veto,
            is_veto=is_veto,
            analysis=data.get("analysis", ""),
            key_signal=data.get("key_signal", ""),
            signals={
                "flow_direction": flow,
                "size_adjustment": size_adj,
            },
        )
