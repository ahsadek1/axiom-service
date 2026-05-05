"""
analyzer.py — Sage Fundamental/Macro Analysis Engine

Uses DeepSeek (deepseek-chat) to score each ticker for fundamental trade quality.
Sage specializes in: earnings risk, fundamentals, macro calendar, insider signals.
"""

import json
import logging
from typing import Optional

from openai import OpenAI

logger = logging.getLogger("sage.analyzer")

AGENT_NAME = "Sage"
DEEPSEEK_MODEL = "deepseek-chat"

# Earnings within this many days = hard score cap
EARNINGS_HARD_BLOCK_DAYS = 7
EARNINGS_SCORE_CAP = 40.0


def _extract_earnings_days(fundamental_data: dict) -> Optional[int]:
    """
    Extract days until next earnings from fundamental engine data.
    Returns None if not available.
    """
    try:
        earnings_date = fundamental_data.get("earnings_date") or fundamental_data.get("next_earnings")
        if not earnings_date:
            return None
        from datetime import datetime
        import pytz
        ET = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        if isinstance(earnings_date, str):
            # Try common date formats
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
                try:
                    dt = datetime.strptime(earnings_date[:10], "%Y-%m-%d").replace(tzinfo=ET)
                    return max(0, (dt - now).days)
                except ValueError:
                    continue
        return None
    except Exception:
        return None


def build_prompt(ticker: str, context: dict, regime: dict, earnings_days: Optional[int]) -> str:
    """
    Build a structured analysis prompt for DeepSeek from ORACLE context data.

    Args:
        ticker: Stock ticker symbol
        context: Full ORACLE context packet
        regime: Current market regime dict from Axiom
        earnings_days: Days until next earnings (None if unknown)

    Returns:
        Formatted prompt string for DeepSeek.
    """
    fundamental_data = context.get("fundamental", {}) or {}
    macro_data = context.get("macro", {}) or {}
    flow_data = context.get("flow", {}) or {}

    regime_class = regime.get("classification", "UNKNOWN")
    strategy_bias = regime.get("strategy_bias", "Unknown")

    fundamental_summary = json.dumps(fundamental_data, default=str)[:900]
    macro_summary = json.dumps(macro_data, default=str)[:700]
    flow_summary = json.dumps(flow_data, default=str)[:500]

    earnings_note = (
        f"⚠️ EARNINGS IN {earnings_days} DAYS — SCORE MUST NOT EXCEED 40"
        if earnings_days is not None and earnings_days < EARNINGS_HARD_BLOCK_DAYS
        else f"Earnings in {earnings_days} days (safe)" if earnings_days is not None
        else "Earnings date unknown"
    )

    return f"""You are Sage, the fundamental and macro analyst for Nexus trading system. Weight: 25%.

Analyze {ticker} for fundamental and macro trade quality.

MARKET REGIME: {regime_class} | Strategy bias: {strategy_bias}
EARNINGS STATUS: {earnings_note}

FUNDAMENTAL DATA (revenue, earnings, margins, growth, insider activity, SEC filings):
{fundamental_summary}

MACRO CALENDAR (Fed meetings, CPI, NFP, macro risk events):
{macro_summary}

FLOW DATA (dark pool activity, institutional positioning):
{flow_summary}

SCORING RUBRIC (total 100 pts):
- Fundamental quality (revenue growth, margins, earnings trajectory, competitive position): 0-30 pts
- Macro calendar clearance (penalize: near Fed/CPI/NFP dates reduce by 15-20 pts): 0-25 pts
- Insider/institutional signal (SEC Form 4 insider buying = +15, selling = -10): 0-20 pts
- Sector macro alignment (sector fits current macro environment): 0-15 pts
- Earnings distance safety (< 7 days = score HARD CAP 40, < 14 days = -10 pts): 0-10 pts

HARD RULES:
- If earnings within {EARNINGS_HARD_BLOCK_DAYS} days: return score = {EARNINGS_SCORE_CAP} maximum, no exceptions
- Macro risk event within 3 days (Fed, CPI, NFP): deduct 15-20 pts
- Sage is particularly important for Prime (swing equity) picks
- Fundamental analysis only — not technical

Return ONLY valid JSON, no markdown, no explanation:
{{"direction": "bullish" or "bearish", "score": float 0-100, "reasoning": "2-3 sentence fundamental analysis summary"}}"""


def analyze(
    ticker: str,
    context: dict,
    regime: dict,
    deepseek_api_key: str,
    deepseek_base_url: str,
) -> Optional[dict]:
    """
    Score a ticker for fundamental trade quality using DeepSeek.

    Args:
        ticker: Stock ticker symbol
        context: Full ORACLE context packet
        regime: Current market regime from Axiom
        deepseek_api_key: DeepSeek API key
        deepseek_base_url: DeepSeek API base URL

    Returns:
        Dict with {direction, score, reasoning} or None if analysis fails.
        Never raises.
    """
    fundamental_data = context.get("fundamental", {}) or {}
    earnings_days = _extract_earnings_days(fundamental_data)

    prompt = build_prompt(ticker, context, regime, earnings_days)

    try:
        client = OpenAI(api_key=deepseek_api_key, base_url=deepseek_base_url)
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.3,
        )

        raw = response.choices[0].message.content.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)

        direction = result.get("direction", "").lower()
        score = float(result.get("score", 0))
        reasoning = str(result.get("reasoning", ""))

        if direction not in ("bullish", "bearish"):
            logger.warning("DeepSeek returned invalid direction '%s' for %s", direction, ticker)
            return None

        score = max(0.0, min(100.0, score))

        # Enforce earnings hard cap — even if DeepSeek ignored the rule
        if earnings_days is not None and earnings_days < EARNINGS_HARD_BLOCK_DAYS:
            if score > EARNINGS_SCORE_CAP:
                logger.warning(
                    "Sage enforcing earnings hard cap for %s: %.1f → %.1f (earnings in %d days)",
                    ticker, score, EARNINGS_SCORE_CAP, earnings_days,
                )
                score = EARNINGS_SCORE_CAP
                reasoning = f"[EARNINGS BLOCK: {earnings_days}d] " + reasoning

        logger.info("Sage scored %s: %s %.1f — %s", ticker, direction, score, reasoning[:80])
        return {"direction": direction, "score": score, "reasoning": reasoning}

    except json.JSONDecodeError as e:
        logger.error("DeepSeek returned invalid JSON for %s: %s", ticker, e)
        return None
    except Exception as e:
        logger.error("DeepSeek API error for %s: %s", ticker, e)
        return None
