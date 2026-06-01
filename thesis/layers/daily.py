"""
DailyBriefGenerator — Layer 2 of THESIS.

Runs Monday-Friday at 7:00 AM ET.  Reads the current weekly thesis from
CHRONICLE, checks for overnight developments via ORACLE and Brave, and
produces a daily addendum.

If the weekly thesis is BROKEN (a critical overnight development invalidates
it), DailyBriefGenerator triggers a full weekly thesis regeneration.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import anthropic

from models import ThesisContext
from thesis_engine import ThesisEngine
from layers.weekly import WeeklyThesisGenerator

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_DAILY_MODEL = "claude-sonnet-4-6"


class DailyBriefGenerator:
    """Generates the daily thesis status brief every weekday at 7 AM ET.

    Args:
        engine: Shared ThesisEngine instance.
        chronicle: CHRONICLE SQLite client (already on engine, also held directly).
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
        self._weekly_gen = WeeklyThesisGenerator(engine, anthropic_client)

    async def generate(self) -> ThesisContext:
        """Generate and persist the daily brief.

        Steps:
          1. Fetch current weekly thesis from CHRONICLE.
          2. Fetch overnight ORACLE data.
          3. Brave search for overnight developments.
          4. Call Claude to assess thesis status.
          5. If BROKEN, trigger full weekly regen.
          6. Save daily ThesisContext.
          7. Notify SOVEREIGN.

        Returns:
            Saved daily ThesisContext.
        """
        logger.info("DailyBriefGenerator: starting daily brief generation")

        weekly = self.engine.chronicle.get_current_thesis(layer="weekly")
        oracle_data = await self.engine.oracle.fetch_market_data()
        overnight_news = await self.engine.brave.search(
            "stock market overnight premarket developments", count=5
        )

        status, summary, key_levels = await self._assess_thesis(
            weekly, oracle_data, overnight_news
        )

        if status == "BROKEN":
            logger.warning(
                "DailyBriefGenerator: weekly thesis BROKEN — triggering full regen"
            )
            try:
                weekly = await self._weekly_gen.generate()
                status = "REGENERATED"
                summary = f"Weekly thesis regenerated. {summary}"
            except Exception as exc:
                logger.error(
                    "DailyBriefGenerator: weekly regen failed: %s", exc, exc_info=True
                )
                status = "BROKEN_REGEN_FAILED"

        now = datetime.now(tz=_ET)
        # Daily brief valid until 4 PM ET today
        valid_until = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if valid_until < now:
            valid_until = valid_until.replace(
                day=valid_until.day + 1
            )

        # Inherit posture + sizing from weekly thesis (or safe defaults)
        base = weekly or ThesisContext(
            layer="weekly",
            valid_from=now.isoformat(),
            valid_until=now.isoformat(),
            trading_posture="NEUTRAL",
            sizing_multiplier=1.0,
            macro_gate="PASS",
            risk_reward_gate="PASS",
            thesis_sentence="No weekly thesis — neutral posture.",
            confidence_adjustment=0,
        )

        daily_context = ThesisContext(
            layer="daily",
            valid_from=now.isoformat(),
            valid_until=valid_until.isoformat(),
            trading_posture=base.trading_posture,
            sizing_multiplier=base.sizing_multiplier,
            favored_sectors=base.favored_sectors,
            avoid_sectors=base.avoid_sectors,
            favored_strategies=base.favored_strategies,
            confidence_adjustment=base.confidence_adjustment,
            macro_gate=base.macro_gate,
            risk_reward_gate=base.risk_reward_gate,
            thesis_sentence=f"[{status}] {summary}",
            primary_authority=base.primary_authority,
            full_thesis=json.dumps({
                "weekly_thesis_status": status,
                "overnight_summary": summary,
                "key_levels": key_levels,
                "oracle_snapshot": oracle_data.model_dump(),
            }),
        )

        saved = self.engine.chronicle.save_thesis(daily_context)
        if not saved:
            logger.error("DailyBriefGenerator: failed to save daily brief to CHRONICLE")

        await self.engine.sovereign.send(
            f"Daily brief ready — thesis {status}. {summary}",
            event_type="status",
        )

        logger.info("DailyBriefGenerator: complete — status=%s", status)
        return daily_context

    async def _assess_thesis(
        self, weekly, oracle_data, overnight_news
    ):
        """Use Claude to assess whether the weekly thesis remains valid.

        Args:
            weekly: Current weekly ThesisContext (may be None).
            oracle_data: Fresh ORACLE snapshot.
            overnight_news: Brave search results for overnight developments.

        Returns:
            Tuple of (status_str, summary_str, key_levels_dict).
        """
        if weekly is None:
            return "NO_THESIS", "No weekly thesis available.", {}

        weekly_summary = f"{weekly.thesis_sentence} Posture: {weekly.trading_posture}."
        news_block = "\n".join(f"  - {h}" for h in overnight_news) if overnight_news else "  (none)"
        oracle_json = oracle_data.model_dump_json(indent=2)

        prompt = f"""You are THESIS reviewing the morning market open.

Weekly thesis: {weekly_summary}

Current market data:
{oracle_json}

Overnight news:
{news_block}

Assess: is the weekly thesis INTACT, MODIFIED, or BROKEN?

INTACT   = thesis still valid, no significant changes needed
MODIFIED = thesis needs minor adjustment but core view holds
BROKEN   = overnight developments fundamentally invalidate the thesis

Respond ONLY with this JSON:
{{
  "status": "INTACT" | "MODIFIED" | "BROKEN",
  "summary": "<2 sentences: what changed and what it means>",
  "key_levels": {{
    "spy_support": <float or null>,
    "spy_resistance": <float or null>,
    "vix_threshold": <float or null>
  }}
}}
"""

        try:
            resp = await self._client.messages.create(
                model=_DAILY_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                data = json.loads(raw[start:end])

            return (
                data.get("status", "INTACT"),
                data.get("summary", "No summary available."),
                data.get("key_levels", {}),
            )
        except Exception as exc:
            logger.error(
                "DailyBriefGenerator._assess_thesis() error: %s", exc, exc_info=True
            )
            return "INTACT", f"Assessment unavailable ({exc}). Assuming intact.", {}
