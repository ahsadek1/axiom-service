"""
WeeklyThesisGenerator — Layer 1 of THESIS.

Runs every Sunday at 6:00 PM ET.  Fetches current market data, runs all
five frameworks, resolves conflict, and uses Claude Opus 4 to write a
polished weekly thesis document stored in CHRONICLE.

The thesis is idempotent: if called more than once in the same week, the
existing record is updated rather than a duplicate created.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

import anthropic

from models import ThesisContext, TradingPosture
from thesis_engine import ThesisEngine

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_THESIS_MODEL = "claude-opus-4-5"
_BRAVE_QUERIES = [
    "Federal Reserve policy outlook interest rates",
    "S&P 500 technical levels support resistance",
    "high yield credit spread trends",
    "sector rotation stock market week ahead",
]


class WeeklyThesisGenerator:
    """Generates the weekly THESIS document every Sunday at 6 PM ET.

    Args:
        engine: Shared ThesisEngine instance.
        anthropic_client: Configured AsyncAnthropic client.
    """

    def __init__(
        self,
        engine: ThesisEngine,
        anthropic_client: anthropic.AsyncAnthropic,
    ) -> None:
        """Store engine and AI client references."""
        self.engine = engine
        self._client = anthropic_client

    async def generate(self) -> ThesisContext:
        """Generate and persist the weekly thesis.

        Steps:
          1. Fetch ORACLE market data.
          2. Run four Brave searches for macro context.
          3. Run all five frameworks concurrently.
          4. Resolve conflict.
          5. Call Claude Opus 4 to write the thesis narrative.
          6. Build ThesisContext and save to CHRONICLE.
          7. Notify SOVEREIGN.

        Returns:
            Saved ThesisContext record.
        """
        logger.info("WeeklyThesisGenerator: starting weekly thesis generation")

        oracle_data = await self.engine.oracle.fetch_market_data()

        news_results: List[str] = []
        for query in _BRAVE_QUERIES:
            results = await self.engine.brave.search(query, count=4)
            news_results.extend(results)

        analyses = await self.engine.run_all_frameworks(oracle_data, news_results)
        resolution = self.engine.resolve_conflict(analyses)
        posture = self.engine.determine_posture(resolution, analyses)
        primary_authority = self.engine.determine_primary_authority(
            oracle_data, resolution
        )

        full_thesis, thesis_sentence, directives = await self._write_thesis(
            oracle_data, analyses, resolution, posture, primary_authority, news_results
        )

        now = datetime.now(tz=_ET)
        next_sunday = now + timedelta(days=(6 - now.weekday()) % 7 + 1)
        valid_until = next_sunday.replace(
            hour=18, minute=0, second=0, microsecond=0
        ).isoformat()

        context = ThesisContext(
            layer="weekly",
            valid_from=now.isoformat(),
            valid_until=valid_until,
            trading_posture=posture.value,
            sizing_multiplier=resolution.size_multiplier,
            favored_sectors=directives.get("favored_sectors", []),
            avoid_sectors=directives.get("avoid_sectors", []),
            favored_strategies=directives.get("favored_strategies", []),
            confidence_adjustment=resolution.confidence_adjustment,
            macro_gate=resolution.macro_gate.value,
            risk_reward_gate=resolution.risk_reward_gate.value,
            thesis_sentence=thesis_sentence,
            primary_authority=primary_authority,
            full_thesis=full_thesis,
        )

        # Idempotency: check if a thesis already exists for this week
        existing = self.engine.chronicle.get_current_thesis(layer="weekly")
        if existing and self._is_same_week(existing, now):
            logger.info(
                "WeeklyThesisGenerator: updating existing weekly thesis (same week)"
            )
            context.id = existing.id

        saved = self.engine.chronicle.save_thesis(context)
        if not saved:
            logger.error("WeeklyThesisGenerator: failed to save thesis to CHRONICLE")

        await self.engine.sovereign.send(
            f"Weekly thesis ready — posture: {posture.value}, "
            f"macro gate: {resolution.macro_gate.value}, "
            f"sizing: {int(resolution.size_multiplier * 100)}%. "
            f"Thesis: {thesis_sentence}",
            event_type="status",
        )

        logger.info(
            "WeeklyThesisGenerator: complete — posture=%s macro=%s",
            posture.value,
            resolution.macro_gate.value,
        )
        return context

    def _is_same_week(self, existing: ThesisContext, now: datetime) -> bool:
        """Check whether an existing thesis was generated in the current week.

        Args:
            existing: Previously saved ThesisContext.
            now: Current datetime (ET).

        Returns:
            True if existing thesis is from the same ISO week as now.
        """
        try:
            existing_dt = datetime.fromisoformat(existing.valid_from)
            if existing_dt.tzinfo is None:
                existing_dt = existing_dt.replace(tzinfo=_ET)
            return existing_dt.isocalendar()[1] == now.isocalendar()[1]
        except Exception:
            return False

    async def _write_thesis(
        self,
        oracle_data,
        analyses,
        resolution,
        posture: TradingPosture,
        primary_authority: str,
        news: List[str],
    ):
        """Call Claude Opus 4 to write the weekly thesis narrative.

        Args:
            oracle_data: Current market snapshot.
            analyses: All five FrameworkAnalysis objects.
            resolution: Completed ConflictResolution.
            posture: Determined trading posture.
            primary_authority: Leading framework name.
            news: News headlines used in framework analysis.

        Returns:
            Tuple of (full_thesis_text, thesis_sentence, directives_dict).
        """
        analyses_json = json.dumps(
            [a.model_dump() for a in analyses], indent=2, default=str
        )
        resolution_json = resolution.model_dump_json(indent=2)
        oracle_json = oracle_data.model_dump_json(indent=2)
        news_block = "\n".join(f"  - {h}" for h in news[:20]) if news else "  (none)"

        prompt = f"""You are THESIS — the strategic intelligence layer for a professional options trading operation.

You think like the greatest traders in history. The five frameworks have already been evaluated:

## Framework Analyses
```json
{analyses_json}
```

## Conflict Resolution
```json
{resolution_json}
```

## Market Data
```json
{oracle_json}
```

## Recent News
{news_block}

## Your Task
Write the weekly strategic thesis. Be precise. Be direct. No hedging.
One verdict. The deliberation is done — state the conclusion.

Respond ONLY with this JSON (no markdown fences):
{{
  "thesis_sentence": "<one sentence: the forest, stated plainly>",
  "full_thesis": "<the complete weekly thesis in plain text, 300-500 words, structured with clear sections>",
  "favored_sectors": ["<sector1>", "<sector2>"],
  "avoid_sectors": ["<sector1>"],
  "favored_strategies": ["<credit_spreads|debit|neutral|iron_condor|etc>"],
  "avoid_strategies": ["<strategy>"],
  "what_would_surprise_everyone": "<one sentence>",
  "highest_conviction_setup": "<one paragraph>"
}}
"""

        try:
            # Use SDK-native per-request timeout (300s) — avoids asyncio.wait_for
            # cancellation propagation issues in Python 3.9.
            resp = await self._client.messages.create(
                model=_THESIS_MODEL,
                max_tokens=2048,
                system=(
                    "You are THESIS, the strategic intelligence layer for a "
                    "professional trading operation. You write with conviction "
                    "and precision. One verdict. No hedging."
                ),
                messages=[{"role": "user", "content": prompt}],
                timeout=300.0,
            )
            raw = resp.content[0].text
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                data = json.loads(raw[start:end])

            return (
                data.get("full_thesis", raw),
                data.get("thesis_sentence", "Thesis generated — see full text."),
                {
                    "favored_sectors": data.get("favored_sectors", []),
                    "avoid_sectors": data.get("avoid_sectors", []),
                    "favored_strategies": data.get("favored_strategies", []),
                },
            )
        except Exception as exc:
            logger.error(
                "WeeklyThesisGenerator._write_thesis() error: %s", exc, exc_info=True
            )
            fallback = (
                f"Thesis generation failed: {exc}. "
                f"Posture: {posture.value}. "
                f"Macro gate: {resolution.macro_gate.value}."
            )
            return (fallback, "Thesis generation failed — using framework outputs.", {})
