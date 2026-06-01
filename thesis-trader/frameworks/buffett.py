"""
THESIS — Buffett Framework (SOFT GATE).

Warren Buffett's fear/greed/IV framework.  Evaluates whether the current
emotional environment and options pricing are favorable for the proposed strategy.

Soft gate: adjusts confidence AND can apply a size reduction.
  Favorable IV:     +10 confidence,   0% size reduction
  Neutral IV:         0 confidence,   0% size reduction
  Unfavorable IV:   -10 confidence, -15% size reduction
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
You are Warren Buffett's fear/greed and IV analytical engine embedded inside
the THESIS trading intelligence service.

Buffett's philosophy:
- Be fearful when others are greedy. Be greedy when others are fearful.
- Price is what you pay. Value is what you get.
- In options trading: high IV (fear) creates premium-selling opportunities.
  Low IV (complacency/greed) creates premium-buying opportunities.
- Emotion disconnects price from value — emotion creates the edge.
- The market's emotional state is directly readable through VIX.

Your job: assess whether the current IV and fear/greed environment favors
the proposed options trade, is neutral, or is unfavorable.

Respond ONLY with a single JSON object — no markdown fences, no commentary.
"""


class BuffettFramework:
    """Soft-gate fear/greed/IV framework based on Warren Buffett's philosophy.

    Can reduce position size by 15% when IV environment is unfavorable for
    the proposed strategy type.
    """

    def __init__(self) -> None:
        """Initialize the Anthropic async client from environment."""
        self._client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", "")
        )

    async def analyze(
        self, oracle_data: OracleData, news: List[str]
    ) -> FrameworkAnalysis:
        """Run Buffett fear/greed/IV analysis.

        Args:
            oracle_data: Current market snapshot from ORACLE.
            news: Recent news headlines.

        Returns:
            FrameworkAnalysis with gate_type="SOFT"; size_veto=True when
            IV environment is unfavorable.
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
                "BuffettFramework.analyze() error: %s", exc, exc_info=True
            )
            return FrameworkAnalysis(
                name="Buffett",
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
        """Construct the Buffett fear/greed/IV prompt.

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
Evaluate the fear/greed and IV environment. Consider:
1. **VIX level** — <15 = complacency/greed, 15-25 = normal, 25-35 = elevated fear, >35 = extreme fear.
2. **IV environment for credit strategies** — VIX >20 = RICH (favorable for selling premium).
3. **IV environment for debit strategies** — VIX <18 = CHEAP (favorable for buying premium).
4. **Market emotion** — Fear creates underpricing opportunities; greed creates overpricing risks.
5. **Premium selling conditions** — FAVORABLE when VIX >20 and trending stable/lower.

Respond ONLY with this JSON:
{{
  "market_emotion": "FEAR" | "NEUTRAL" | "GREED",
  "iv_environment": "RICH" | "FAIR" | "CHEAP",
  "premium_selling_conditions": "FAVORABLE" | "NEUTRAL" | "UNFAVORABLE",
  "confidence_adjustment": 10 | 0 | -10,
  "size_adjustment": 0.0 | -0.15,
  "analysis": "<2-4 sentence synthesis>",
  "key_signal": "<primary IV/emotion signal>"
}}

Rules:
  FEAR  + RICH IV   → confidence=+10, size=0.0   (best premium selling environment)
  NEUTRAL + FAIR IV → confidence=0,   size=0.0
  GREED + CHEAP IV  → confidence=-10, size=-0.15  (premium too cheap to sell, too expensive to buy)
  Mixed signals     → use judgment, err conservative
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
                    "BuffettFramework failed to parse response: %s\nRaw: %s",
                    exc, raw_text[:500],
                )
                return FrameworkAnalysis(
                    name="Buffett",
                    gate_type="SOFT",
                    gate_result=None,
                    confidence_adjustment=0,
                    size_veto=False,
                    is_veto=False,
                    analysis="JSON parse error — neutral applied.",
                    key_signal="Parse error",
                    signals={},
                )

        raw_adj = int(data.get("confidence_adjustment", 0))
        if raw_adj >= 10:
            adj = 10
        elif raw_adj <= -10:
            adj = -10
        else:
            adj = 0

        raw_size = float(data.get("size_adjustment", 0.0))
        size_veto = raw_size < 0

        return FrameworkAnalysis(
            name="Buffett",
            gate_type="SOFT",
            gate_result=None,
            confidence_adjustment=adj,
            size_veto=size_veto,
            is_veto=adj < 0,
            analysis=data.get("analysis", ""),
            key_signal=data.get("key_signal", ""),
            signals={
                "market_emotion": data.get("market_emotion", "NEUTRAL"),
                "iv_environment": data.get("iv_environment", "FAIR"),
                "premium_selling_conditions": data.get("premium_selling_conditions", "NEUTRAL"),
                "size_adjustment": raw_size,
            },
        )
