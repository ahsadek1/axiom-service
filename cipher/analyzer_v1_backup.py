"""
analyzer.py — Cipher Options Analysis Engine

Uses Claude (claude-sonnet-4-6) to score each ticker for options trading quality.
Cipher specializes in: IV rank, spread structure, options flow, gamma environment.
"""

import json
import logging
from typing import Optional

import anthropic

logger = logging.getLogger("cipher.analyzer")

# Cipher's canonical agent name — must match VALID_AGENTS in Alpha/Prime Buffer
AGENT_NAME = "Cipher"

# Claude model
CLAUDE_MODEL = "claude-sonnet-4-6"


def build_prompt(ticker: str, context: dict, regime: dict) -> str:
    """
    Build a structured analysis prompt for Claude from ORACLE context data.

    Args:
        ticker: Stock ticker symbol
        context: Full ORACLE context packet (7-engine data)
        regime: Current market regime dict from Axiom

    Returns:
        Formatted prompt string for Claude.
    """
    # Extract relevant engine data safely
    price_data = context.get("price", {}) or {}
    vol_data = context.get("vol", {}) or {}
    flow_data = context.get("flow", {}) or {}
    gamma_data = context.get("gamma", {}) or {}
    historical_data = context.get("historical", {}) or {}

    regime_class = regime.get("classification", "UNKNOWN")
    strategy_bias = regime.get("strategy_bias", "Unknown")
    alpha_credit = regime.get("alpha_credit_allowed", True)
    alpha_debit = regime.get("alpha_debit_allowed", True)

    # Compact summary of key data points
    price_summary = json.dumps(price_data, default=str)[:800]
    vol_summary = json.dumps(vol_data, default=str)[:800]
    flow_summary = json.dumps(flow_data, default=str)[:600]
    gamma_summary = json.dumps(gamma_data, default=str)[:600]
    hist_summary = json.dumps(historical_data, default=str)[:400]

    return f"""You are Cipher, the options specialist for Nexus Alpha trading system. Weight: 45%.

Analyze {ticker} for options trading opportunities using the ORACLE intelligence below.

MARKET REGIME: {regime_class} | Strategy bias: {strategy_bias}
Credit spreads allowed: {alpha_credit} | Debit spreads allowed: {alpha_debit}

PRICE DATA:
{price_summary}

VOLATILITY DATA (IV rank, IV percentile, historical vol):
{vol_summary}

OPTIONS FLOW (unusual sweeps, dark pool, put/call ratio):
{flow_summary}

GAMMA ENVIRONMENT (dealer positioning, gamma walls, GEX):
{gamma_summary}

HISTORICAL WIN RATES (from AILS backtest):
{hist_summary}

SCORING RUBRIC (total 100 pts):
- IV rank alignment (>50 favors credit, <30 favors debit, 15-50 neutral): 0-25 pts
  Strong (IVR 40-70): 20-25 | Moderate (IVR 20-40 or 70-90): 14-19 | Weak (<15 or >90): 5-10
- Options flow conviction (put/call ratio, dark pool bias, volume): 0-20 pts
  Strong (PCR<0.5 bullish or >1.5 bearish, vol 2x avg): 17-20 | Moderate (PCR 0.5-0.8, vol 1.2x): 12-16 | Weak (neutral PCR, normal vol): 7-11
- Price trend confirmation (trend clarity, support/resistance distance): 0-20 pts
  Strong (above/below gamma flip, clear momentum): 16-20 | Moderate (mild trend, near flip): 10-15 | Weak (choppy, no trend): 5-9
- Gamma environment safety (distance to walls, dealer gamma, flip level): 0-15 pts
  Strong (positive GEX, 2%+ from wall, above flip): 12-15 | Moderate (neutral GEX, near wall): 7-11 | Weak (negative GEX, at wall): 2-6
- Regime fit (does this setup match current regime): 0-10 pts
  NORMAL/LOW_VOL with confirmed trend: 7-10 | Mixed signals: 4-6 | Crisis/mismatch: 0-3
- Historical win rate context (AILS backtest): 0-10 pts
  Win rate >60%: 8-10 | 50-60%: 5-7 | <50% or no data: 2-4

SCORE CALIBRATION (mandatory — anchor your score to these benchmarks):
- 75-85: HIGH CONVICTION — Strong signals in 4+ categories. Clear edge, execute.
- 60-74: QUALIFIED — Good signals in 3+ categories. Meets system standard. Execute.
- 45-59: MARGINAL — Some signals but weak confirmation. Below system threshold.
- 0-44: REJECT — Setup is unclear, contradictory, or conditions unfavorable.
The system minimum threshold is 58. A "decent but not exceptional" setup should score 62-68.

HARD RULES:
- IV rank < 15 AND no strong flow signal → score must be ≤ 45 (premium too cheap without flow confirmation)
- IV rank 15-25 with strong flow (PCR <0.6 or >1.4) → override: score may reach 58-65 (flow compensates)
- If both directions score >55, pick the stronger one only
- Score of 0 is valid (no trade here)

Return ONLY valid JSON, no markdown, no explanation:
{{"direction": "bullish" or "bearish", "score": float 0-100, "reasoning": "2-3 sentence summary of key factors"}}"""


def analyze(
    ticker: str,
    context: dict,
    regime: dict,
    anthropic_api_key: str,
) -> Optional[dict]:
    """
    Score a ticker for options trading quality using Claude.

    Args:
        ticker: Stock ticker symbol
        context: Full ORACLE context packet
        regime: Current market regime from Axiom
        anthropic_api_key: Anthropic API key

    Returns:
        Dict with {direction, score, reasoning} or None if analysis fails.
        Never raises.
    """
    prompt = build_prompt(ticker, context, regime)

    try:
        client = anthropic.Anthropic(api_key=anthropic_api_key)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)

        # Validate required fields
        direction = result.get("direction", "").lower()
        score = float(result.get("score", 0))
        reasoning = str(result.get("reasoning", ""))

        if direction not in ("bullish", "bearish"):
            logger.warning("Claude returned invalid direction '%s' for %s", direction, ticker)
            return None

        score = max(0.0, min(100.0, score))

        logger.info("Cipher scored %s: %s %.1f — %s", ticker, direction, score, reasoning[:80])
        return {"direction": direction, "score": score, "reasoning": reasoning}

    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON for %s: %s", ticker, e)
        return None
    except anthropic.APIError as e:
        logger.error("Anthropic API error for %s: %s", ticker, e)
        return None
    except Exception as e:
        logger.error("Unexpected error analyzing %s: %s", ticker, e)
        return None
