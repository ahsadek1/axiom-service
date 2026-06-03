"""
ORACLE — Gemini Client
Used by the cross-ticker pattern detection intelligence layer.
Receives all Full Cards for a cycle and identifies sector-level patterns.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

import rate_limiter
import config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You analyze cross-ticker patterns in trading market data. Given multiple ticker "
    "context packets from the same trading cycle, identify sector-level positioning events, "
    "correlated flow patterns, and echo chamber risks in the data. "
    "Return ONLY valid JSON with these exact fields: "
    "patterns_detected (array of objects with type str, tickers_involved array of str, "
    "description str, implication str), "
    "echo_chamber_risk_tickers (array of str — tickers whose signals may be correlated). "
    "Valid pattern types: SECTOR_FLOW_EVENT, CORRELATED_POSITIONING, MACRO_DRIVEN_CLUSTER, "
    "ECHO_CHAMBER_RISK. "
    "Never make directional trading recommendations. Never say what to buy or sell."
)


def detect_patterns(cycle_id: str, context_packets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Run cross-ticker pattern detection on all packets in a cycle.

    Args:
        cycle_id:        Unique identifier for this Axiom cycle.
        context_packets: List of simplified ticker summaries for the full pool.

    Returns:
        Dict with patterns_detected and echo_chamber_risk_tickers. None on failure.
    """
    if not rate_limiter.acquire("gemini"):
        logger.warning("Gemini rate limit timeout for cycle %s", cycle_id)
        return None

    try:
        import google.generativeai as genai
        genai.configure(api_key=config.GEMINI_API_KEY)
    except ImportError:
        logger.error("google-generativeai not installed — Gemini unavailable")
        return None
    except Exception as e:
        logger.error("Gemini configuration error: %s", e)
        return None

    user_content = (
        f"Cycle ID: {cycle_id}\n"
        f"Pool size: {len(context_packets)} tickers\n"
        f"Context packets: {json.dumps(context_packets, indent=2)}\n"
        "Identify cross-ticker patterns. Return JSON only."
    )

    start = time.monotonic()
    try:
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            system_instruction=_SYSTEM_PROMPT,
        )
        response = model.generate_content(
            user_content,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                max_output_tokens=600,
            ),
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        content = response.text.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        result = json.loads(content)
        result["latency_ms"] = latency_ms
        result["cycle_id"] = cycle_id
        return result

    except Exception as e:
        logger.error("Gemini pattern detection error for cycle %s: %s", cycle_id, e)
        return None
