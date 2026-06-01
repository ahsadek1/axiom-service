"""
THESIS — Soros Framework (SOFT GATE).

George Soros's narrative/reflexivity framework.  Evaluates whether the current
market narrative is aligned with, neutral to, or opposed to the proposed trade.

Soft gate: adjusts confidence but never issues an unconditional veto.
  Strong alignment:   +15
  Neutral:              0
  Soft opposition:    -10
  Strong opposition:  -20
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
You are George Soros's narrative/reflexivity analytical engine embedded inside
the THESIS trading intelligence service.

Soros's philosophy:
- Markets are not efficient; they operate on collective beliefs that diverge
  from reality and then correct, often violently.
- Find where belief and reality are about to diverge — that divergence IS the trade.
- Riding a reflexive trend (self-fulfilling belief) can be highly profitable;
  fading a fragile narrative at the point of maximum extension is even more so.
- When everyone agrees, something unexpected is already forming.

Your job: assess whether the current market narrative is aligned with,
neutral to, or opposed to a bullish risk-taking posture.

Respond ONLY with a single JSON object — no markdown fences, no commentary.
"""


class SorosFramework:
    """Soft-gate narrative/reflexivity framework based on George Soros's approach.

    Returns a confidence adjustment in [-20, -10, 0, 15] rather than a
    binary gate result.  A negative adjustment indicates soft opposition.
    """

    def __init__(self) -> None:
        """Initialize the Anthropic async client from environment."""
        self._client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", "")
        )

    async def analyze(
        self, oracle_data: OracleData, news: List[str]
    ) -> FrameworkAnalysis:
        """Run Soros narrative/reflexivity analysis.

        Args:
            oracle_data: Current market snapshot from ORACLE.
            news: Recent news headlines relevant to current narrative.

        Returns:
            FrameworkAnalysis with gate_type="SOFT" and a confidence_adjustment.
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
                "SorosFramework.analyze() error: %s", exc, exc_info=True
            )
            return FrameworkAnalysis(
                name="Soros",
                gate_type="SOFT",
                gate_result=None,
                confidence_adjustment=-10,
                size_veto=False,
                is_veto=True,
                analysis=f"Analysis unavailable: {exc}",
                key_signal="Error — conservative -10 adjustment applied",
                signals={},
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, oracle_data: OracleData, news: List[str]) -> str:
        """Construct the Claude prompt for Soros narrative analysis.

        Args:
            oracle_data: Current market snapshot.
            news: Recent news headlines.

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
Evaluate the narrative environment. Consider:
1. **Current narrative** — what story is the market telling itself right now?
2. **Narrative fragility** — what single data point or event breaks it?
3. **Reflexive opportunity** — is there a self-reinforcing trend in play?
4. **Consensus vs. contrarian** — is this a consensus trade or a contrarian bet?
5. **Alignment** — does the narrative support or oppose bullish risk-taking?

Respond ONLY with this JSON:
{{
  "narrative_alignment": "strongly_aligned" | "aligned" | "neutral" | "opposed" | "strongly_opposed",
  "confidence_adjustment": 15 | 0 | -10 | -20,
  "analysis": "<2-4 sentence synthesis>",
  "current_narrative": "<one sentence: what the market believes right now>",
  "fragility_signal": "<what single event breaks this narrative>"
}}

Alignment → adjustment mapping:
  strongly_aligned  → +15
  aligned           → +15
  neutral           →   0
  opposed           → -10
  strongly_opposed  → -20
"""

    def _parse_response(self, raw_text: str) -> FrameworkAnalysis:
        """Parse Claude's JSON response into a FrameworkAnalysis.

        Falls back to a neutral 0 adjustment on any parse error.

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
                    "SorosFramework failed to parse response: %s\nRaw: %s",
                    exc, raw_text[:500],
                )
                return FrameworkAnalysis(
                    name="Soros",
                    gate_type="SOFT",
                    gate_result=None,
                    confidence_adjustment=0,
                    size_veto=False,
                    is_veto=False,
                    analysis="JSON parse error — neutral adjustment applied.",
                    key_signal="Parse error",
                    signals={},
                )

        raw_adj = int(data.get("confidence_adjustment", 0))
        # Clamp to valid values
        if raw_adj >= 15:
            adj = 15
        elif raw_adj >= 0:
            adj = 0
        elif raw_adj >= -10:
            adj = -10
        else:
            adj = -20

        return FrameworkAnalysis(
            name="Soros",
            gate_type="SOFT",
            gate_result=None,
            confidence_adjustment=adj,
            size_veto=False,
            is_veto=adj < 0,
            analysis=data.get("analysis", ""),
            key_signal=data.get("current_narrative", ""),
            signals={
                "narrative_alignment": data.get("narrative_alignment", "neutral"),
                "fragility_signal": data.get("fragility_signal", ""),
            },
        )
