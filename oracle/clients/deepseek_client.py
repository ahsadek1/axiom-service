"""
ORACLE — DeepSeek Client
Used by the coherence intelligence layer.
Reads assembled context packets and returns signal coherence scores.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

import requests

import config
import rate_limiter

logger = logging.getLogger(__name__)

TIMEOUT = (5, 20)

_SYSTEM_PROMPT = (
    "You analyze trading signal coherence. Given a ticker context packet with data "
    "from multiple sources (vol, flow, gamma, macro, fundamental), assess whether the "
    "signals are aligned or conflicted. Return ONLY valid JSON with these exact fields: "
    "coherence_score (int 0-100, higher=more aligned), "
    "coherence_level (str: HIGH/MEDIUM/LOW/CONFLICTED), "
    "flags (array of objects with type str, description str, severity str WARNING/CAUTION/INFO), "
    "aligned_signals (array of str), "
    "conflicting_signals (array of str). "
    "Never state directional opinions. Never say what to trade. Only assess signal alignment."
)


def get_coherence(ticker: str, context_summary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Ask DeepSeek to score signal coherence for an assembled context packet.

    Args:
        ticker:          Ticker symbol.
        context_summary: Simplified summary of key signals (not full packet).

    Returns:
        Dict with coherence_score, coherence_level, flags, aligned_signals,
        conflicting_signals. None on failure.
    """
    if not rate_limiter.acquire("deepseek"):
        logger.warning("DeepSeek rate limit timeout for %s", ticker)
        return None

    user_content = (
        f"Ticker: {ticker}\n"
        f"Signal summary: {json.dumps(context_summary, indent=2)}\n"
        "Assess signal coherence and return JSON only."
    )

    payload = {
        "model": config.DEEPSEEK_MODEL,
        "temperature": config.DEEPSEEK_TEMPERATURE,
        "max_tokens": config.DEEPSEEK_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }

    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    start = time.monotonic()
    try:
        resp = requests.post(
            config.DEEPSEEK_API_URL,
            json=payload,
            headers=headers,
            timeout=TIMEOUT,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            logger.error("DeepSeek error %s for %s: %s",
                         resp.status_code, ticker, resp.text[:200])
            return None

        content = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        result = json.loads(content)
        result["latency_ms"] = latency_ms
        return result

    except requests.Timeout:
        logger.error("DeepSeek timeout for %s", ticker)
        return None
    except requests.ConnectionError as e:
        logger.error("DeepSeek connection error for %s: %s", ticker, e)
        return None
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error("DeepSeek response parse error for %s: %s", ticker, e)
        return None
