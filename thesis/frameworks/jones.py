"""
THESIS — Jones Framework (GRADUATED GATE).

Paul Tudor Jones's risk/reward framework — recalibrated May 2026.

Gate behaviour (Part B):
  - VIX > 35            → size_multiplier = 0.0  (hard stop, destruction regime)
  - FAIL (other reason) → size_multiplier = 0.25 (reduced size, not blocked)
  - PASS                → size_multiplier = 1.0  (full size)

Credit-spread awareness (Part A):
  - VIX 14-22 is a viable environment for credit spreads and iron condors.
    Jones does NOT fail a trade solely because VIX is moderate; he evaluates
    whether the trade structure is appropriate for the current regime.
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
You are Paul Tudor Jones's risk/reward analytical engine embedded inside the
THESIS trading intelligence service.

Your sole purpose is to assess whether current market conditions offer an
acceptable risk-to-reward entry point, and to set the appropriate size multiplier.

Jones's philosophy (recalibrated):
- Never enter a trade unless the expected reward is at least 3x the defined risk.
  A ratio below 3:1 degrades but does not automatically block — it reduces size.
- VIX REGIMES:
    * VIX < 15  : Low volatility — excellent for directional momentum trades.
    * VIX 14-22 : Moderate volatility — IDEAL for credit spreads, iron condors,
                  and premium-selling structures. Do NOT penalize these setups
                  for moderate VIX. Evaluate the trade structure, not just VIX.
    * VIX 22-35 : Elevated volatility — favor defined-risk structures, reduce
                  directional exposure, require higher R:R.
    * VIX > 35  : Destruction regime — NO new positions. size_multiplier = 0.0.
- Entry quality matters — chasing a move after it has already occurred
  dramatically degrades R:R.
- In choppy, directionless markets the probability of a clean trend
  continuation is low.
- The gate is GRADUATED, not binary. A weak setup trades smaller, not zero
  (unless VIX > 35).

SIZE MULTIPLIER RULES:
  - PASS  → size_multiplier: 1.0
  - FAIL (VIX > 35) → size_multiplier: 0.0
  - FAIL (any other reason) → size_multiplier: 0.25

Respond ONLY with a single JSON object — no markdown fences, no commentary.
"""


class JonesFramework:
    """Graduated risk/reward framework based on Paul Tudor Jones's approach.

    Gate is GRADUATED: FAIL reduces size rather than blocking entirely,
    except when VIX > 35 (destruction regime) which hard-stops at 0.0x.
    """

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", "")
        )

    async def analyze(
        self, oracle_data: OracleData, news: List[str]
    ) -> FrameworkAnalysis:
        """Run Jones risk/reward analysis against current market data.

        Returns:
            FrameworkAnalysis with gate_type="GRADUATED".
            size_multiplier in signals: 1.0 (PASS), 0.25 (FAIL), 0.0 (VIX>35).
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
                "JonesFramework.analyze() error: %s", exc, exc_info=True
            )
            return FrameworkAnalysis(
                name="Jones",
                gate_type="GRADUATED",
                gate_result=GateResult.FAIL,
                confidence_adjustment=-25,
                size_veto=False,
                is_veto=False,
                analysis=f"Analysis unavailable: {exc}",
                key_signal="Error — defaulting to 0.25x size",
                signals={"size_multiplier": 0.25},
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, oracle_data: OracleData, news: List[str]) -> str:
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
Evaluate whether current market conditions offer an acceptable risk/reward entry.
Assess each signal explicitly:

1. **R:R ratio** — estimate expected reward vs. defined risk. Target >= 3:1.
2. **VIX regime** — classify: <15 (low), 14-22 (moderate/credit-spread-viable),
   22-35 (elevated), >35 (destruction/hard-stop).
3. **Trade structure fit** — does the trade type match the VIX regime?
   Credit spreads and iron condors are APPROPRIATE in VIX 14-22 — do not
   penalize them. Directional trades need cleaner momentum.
4. **Entry quality** — optimal setup or chasing a move?
5. **Market structure** — trending or choppy?
6. **Credit spread viable** — is VIX in the 14-22 range where premium-selling
   structures make sense?

Then apply the SIZE MULTIPLIER:
  - PASS  → 1.0
  - FAIL because VIX > 35 → 0.0
  - FAIL for any other reason → 0.25

Respond with ONLY the following JSON (no markdown, no explanation):
{{
  "gate": "PASS" | "FAIL",
  "fail_reason": "vix_destruction" | "rr_insufficient" | "entry_quality" | "market_structure" | null,
  "size_multiplier": <0.0 | 0.25 | 1.0>,
  "rr_ratio": <float>,
  "vix_regime": "low" | "moderate" | "elevated" | "destruction",
  "analysis": "<2-4 sentence synthesis>",
  "key_signal": "<single most important signal>",
  "signals": {{
    "rr_above_minimum": <true|false>,
    "vix_dangerous": <true|false>,
    "vix_regime": "<low|moderate|elevated|destruction>",
    "credit_spread_viable": <true|false>,
    "trade_structure_fits_regime": <true|false>,
    "volatility_regime": "<supportive|elevated|destructive>",
    "entry_quality": "<optimal|chasing>",
    "market_structure": "<trending|choppy>",
    "size_multiplier": <0.0 | 0.25 | 1.0>
  }}
}}
"""

    def _parse_response(self, raw_text: str) -> FrameworkAnalysis:
        """Parse Claude's JSON into a FrameworkAnalysis with graduated sizing."""
        try:
            data: Dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError:
            try:
                start = raw_text.index("{")
                end = raw_text.rindex("}") + 1
                data = json.loads(raw_text[start:end])
            except (ValueError, json.JSONDecodeError) as parse_exc:
                logger.error("JonesFramework parse error: %s", parse_exc)
                return FrameworkAnalysis(
                    name="Jones",
                    gate_type="GRADUATED",
                    gate_result=GateResult.FAIL,
                    confidence_adjustment=-25,
                    size_veto=False,
                    is_veto=False,
                    analysis="JSON parse error — defaulting to 0.25x size.",
                    key_signal="Parse error",
                    signals={"size_multiplier": 0.25},
                )

        gate_str = str(data.get("gate", "FAIL")).upper()
        gate_result = GateResult.PASS if gate_str == "PASS" else GateResult.FAIL
        fail_reason = data.get("fail_reason")

        # Graduated multiplier
        if gate_result == GateResult.PASS:
            size_multiplier = 1.0
            confidence_adjustment = 0
            is_veto = False
        elif fail_reason == "vix_destruction":
            size_multiplier = 0.0
            confidence_adjustment = -100
            is_veto = True   # only hard veto remaining
        else:
            size_multiplier = 0.25
            confidence_adjustment = -25
            is_veto = False  # trade proceeds at 0.25x

        signals: Dict[str, Any] = data.get("signals", {})
        signals["size_multiplier"] = size_multiplier
        if "rr_ratio" in data:
            signals["rr_ratio"] = data["rr_ratio"]
        if "vix_regime" in data:
            signals["vix_regime"] = data["vix_regime"]

        return FrameworkAnalysis(
            name="Jones",
            gate_type="GRADUATED",
            gate_result=gate_result,
            confidence_adjustment=confidence_adjustment,
            size_veto=False,
            is_veto=is_veto,
            analysis=data.get("analysis", ""),
            key_signal=data.get("key_signal", ""),
            signals=signals,
        )
