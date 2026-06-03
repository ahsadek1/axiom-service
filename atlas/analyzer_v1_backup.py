"""
analyzer.py — Atlas Technical Analysis Engine

Uses Gemini (gemini-2.5-pro) to score each ticker for technical trade quality.
Atlas specializes in: chart patterns, momentum, sector rotation, volume confirmation.

Timeout / retry policy:
  - Primary:  gemini-2.5-pro, 45 s timeout, 1 retry on timeout/5xx
  - Fallback: gemini-2.5-flash, 30 s timeout, no retry
  This keeps Atlas alive even when 2.5-pro is slow without dropping the pick.
"""

import json
import logging
import time
from typing import Optional

import google.generativeai as genai

logger = logging.getLogger("atlas.analyzer")

AGENT_NAME = "Atlas"
GEMINI_MODEL_PRIMARY  = "gemini-2.5-pro"
GEMINI_MODEL_FALLBACK = "gemini-2.5-flash"

# Timeout constants (seconds)
_TIMEOUT_PRIMARY  = 45
_TIMEOUT_FALLBACK = 30
_RETRY_DELAY_S    = 2


def build_prompt(ticker: str, context: dict, regime: dict, echo_chamber_risk: list) -> str:
    """
    Build a structured analysis prompt for Gemini from ORACLE context data.

    Args:
        ticker: Stock ticker symbol
        context: Full ORACLE context packet
        regime: Current market regime dict from Axiom
        echo_chamber_risk: List of tickers flagged as over-watched

    Returns:
        Formatted prompt string for Gemini.
    """
    price_data = context.get("price", {}) or {}
    flow_data = context.get("flow", {}) or {}
    gamma_data = context.get("gamma", {}) or {}
    historical_data = context.get("historical", {}) or {}

    regime_class = regime.get("classification", "UNKNOWN")
    strategy_bias = regime.get("strategy_bias", "Unknown")
    is_echo = ticker in echo_chamber_risk

    price_summary = json.dumps(price_data, default=str)[:900]
    flow_summary = json.dumps(flow_data, default=str)[:600]
    gamma_summary = json.dumps(gamma_data, default=str)[:400]
    hist_summary = json.dumps(historical_data, default=str)[:600]

    echo_note = "⚠️ THIS TICKER IS FLAGGED AS OVER-WATCHED (echo chamber risk — apply -10pt penalty)" if is_echo else "Not flagged as over-watched."

    return f"""You are Atlas, the technical analyst for Nexus trading system. Weight: 30%.

Analyze {ticker} for technical trade quality using the ORACLE intelligence below.

MARKET REGIME: {regime_class} | Strategy bias: {strategy_bias}
ECHO CHAMBER STATUS: {echo_note}

PRICE DATA (trend, RSI, momentum, support/resistance):
{price_summary}

OPTIONS FLOW (volume confirmation, institutional activity):
{flow_summary}

GAMMA LEVELS (key technical levels from dealer positioning):
{gamma_summary}

HISTORICAL PATTERNS (win rates, typical outcomes):
{hist_summary}

SCORING RUBRIC (total 100 pts):
- Trend strength and clarity (RSI trend, MA alignment, momentum): 0-30 pts
  Strong (RSI 55-70 bullish or 30-45 bearish, MA aligned, clear momentum): 24-30
  Moderate (RSI 45-55, mild MA alignment, tepid momentum): 15-23
  Weak (RSI contradictory, flat MAs, no momentum): 5-14
- Pattern quality (breakout setup, support/resistance clarity, setup risk): 0-25 pts
  Strong (clean break above resistance or below support, >1.5x R/R): 20-25
  Moderate (near breakout, identifiable S/R, decent R/R): 13-19
  Weak (no clear pattern, messy levels): 4-12
- Volume/flow confirmation (options PCR, price-volume relationship): 0-20 pts
  Strong (PCR confirms direction, vol expanding on move): 16-20
  Moderate (neutral PCR, volume on-trend): 10-15
  Weak (PCR contradicts, low volume, distribution): 2-9
- Sector rotation alignment (is this sector in current favor): 0-15 pts
  Favorable (sector in leadership, regime confirms): 11-15
  Neutral (sector mixed, no strong headwind): 6-10
  Unfavorable (sector lagging, rotation away): 1-5
- Echo chamber discount: if ticker is over-watched, deduct 10 pts from final score

SCORE CALIBRATION (mandatory — anchor your score to these benchmarks):
- 75-85: HIGH CONVICTION — Strong technical setup. Multiple confirming signals.
- 60-74: QUALIFIED — Good technical read. Meets system standard. Execute.
- 45-59: MARGINAL — Some signals present but insufficient confirmation.
- 0-44: REJECT — Ambiguous, contradictory, or clearly unfavorable.
The system minimum threshold is 58. A "solid but unexceptional" setup = 62-68.

RULES:
- Score reflects TECHNICAL quality only — not fundamental or macro
- Atlas is particularly important for Prime (swing equity) picks
- If trend is ambiguous OR contradictory, score cannot exceed 65 (not 55 — ambiguous ≠ bad)

Return ONLY valid JSON, no markdown, no explanation:
{{"direction": "bullish" or "bearish", "score": float 0-100, "reasoning": "2-3 sentence technical analysis summary"}}"""


def _parse_gemini_response(raw: str) -> dict:
    """
    Parse and validate a raw Gemini text response into a scored result dict.

    Args:
        raw: Raw text from Gemini response.

    Returns:
        Validated dict with {direction, score, reasoning}.

    Raises:
        json.JSONDecodeError: If JSON cannot be parsed.
        ValueError: If required fields are missing or invalid.
    """
    raw = raw.strip()
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
        raise ValueError(f"Invalid direction: {direction!r}")

    score = max(0.0, min(100.0, score))
    return {"direction": direction, "score": score, "reasoning": reasoning}


def _call_gemini(
    prompt: str,
    api_key: str,
    model_name: str,
    timeout_s: int,
) -> Optional[str]:
    """
    Call Gemini with an explicit timeout. Returns raw text or None on failure.

    Args:
        prompt:     Analysis prompt.
        api_key:    Google Gemini API key.
        model_name: Gemini model identifier.
        timeout_s:  Request timeout in seconds.

    Returns:
        Raw response text, or None if the call fails.
    """
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(
            prompt,
            request_options={"timeout": timeout_s},
        )
        return response.text
    except Exception as e:
        logger.warning("Gemini call failed (model=%s timeout=%ds): %s", model_name, timeout_s, e)
        return None


def analyze(
    ticker: str,
    context: dict,
    regime: dict,
    echo_chamber_risk: list,
    gemini_api_key: str,
) -> Optional[dict]:
    """
    Score a ticker for technical trade quality using Gemini.

    Retry policy:
      1. gemini-2.5-pro with 45 s timeout — 1 retry after 2 s delay
      2. gemini-2.5-flash with 30 s timeout — final attempt

    Args:
        ticker:            Stock ticker symbol.
        context:           Full ORACLE context packet.
        regime:            Current market regime from Axiom.
        echo_chamber_risk: List of over-watched tickers.
        gemini_api_key:    Google Gemini API key.

    Returns:
        Dict with {direction, score, reasoning} or None if all attempts fail.
        Never raises.
    """
    prompt = build_prompt(ticker, context, regime, echo_chamber_risk)

    # --- Attempt 1 & 2: primary model with one retry ---
    for attempt in range(1, 3):
        raw = _call_gemini(prompt, gemini_api_key, GEMINI_MODEL_PRIMARY, _TIMEOUT_PRIMARY)
        if raw is not None:
            try:
                result = _parse_gemini_response(raw)
                logger.info(
                    "Atlas scored %s: %s %.1f (primary attempt %d) — %s",
                    ticker, result["direction"], result["score"], attempt, result["reasoning"][:80],
                )
                return result
            except (json.JSONDecodeError, ValueError) as e:
                logger.error("Primary model bad response for %s (attempt %d): %s", ticker, attempt, e)
        if attempt == 1:
            logger.info("Retrying primary model for %s in %ds …", ticker, _RETRY_DELAY_S)
            time.sleep(_RETRY_DELAY_S)

    # --- Attempt 3: fallback model ---
    logger.warning("Primary model exhausted for %s — falling back to %s", ticker, GEMINI_MODEL_FALLBACK)
    raw = _call_gemini(prompt, gemini_api_key, GEMINI_MODEL_FALLBACK, _TIMEOUT_FALLBACK)
    if raw is not None:
        try:
            result = _parse_gemini_response(raw)
            logger.info(
                "Atlas scored %s: %s %.1f (fallback) — %s",
                ticker, result["direction"], result["score"], result["reasoning"][:80],
            )
            return result
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Fallback model bad response for %s: %s", ticker, e)

    logger.error("All Gemini attempts failed for %s — dropping ticker", ticker)
    return None
