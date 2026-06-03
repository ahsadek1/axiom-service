"""
thesis_client.py — Lightweight THESIS Context Client

Fetches the current weekly/daily strategic context from the THESIS service.
Used by agents (Cipher, Atlas, Sage) and OMNI to incorporate macro posture,
sizing multiplier, favored/avoid sectors, and gate results into their analysis.

Design:
    - Always returns a ThesisContext dict — never raises, never blocks
    - On any failure (network, timeout, malformed) returns a safe NEUTRAL default
    - Caches result for CACHE_TTL_SECONDS to avoid hammering THESIS on every ticker
    - Cache is process-level (no shared state needed — each agent is a separate process)

Safe default:
    trading_posture = NEUTRAL
    sizing_multiplier = 1.0
    confidence_adjustment = 0
    is_fallback = True
    All gates PASS
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Cache: single slot — one context, one timestamp
_cached_context: Optional[dict] = None
_cached_at: float = 0.0
CACHE_TTL_SECONDS: float = 300.0   # 5 minutes — refresh at most every 5 min
REQUEST_TIMEOUT_SECONDS: float = 4.0

_SAFE_DEFAULT: dict = {
    "macro_gate": "PASS",
    "risk_reward_gate": "PASS",
    "trading_posture": "NEUTRAL",
    "sizing_multiplier": 1.0,
    "favored_sectors": [],
    "avoid_sectors": [],
    "favored_strategies": [],
    "confidence_adjustment": 0,
    "thesis_sentence": "No thesis available — neutral posture",
    "primary_authority": "",
    "valid_until": "",
    "thesis_age_hours": 0.0,
    "is_fallback": True,
    "fallback_reason": "thesis_client_default",
}


def get_thesis_context(thesis_url: Optional[str] = None) -> dict:
    """Fetch current THESIS strategic context.

    Returns a cached result for up to CACHE_TTL_SECONDS to avoid excessive
    network calls when processing a full ticker pool.

    Args:
        thesis_url: Base URL for THESIS service.
            Defaults to THESIS_URL env var, then http://192.168.1.42:8060.

    Returns:
        Thesis context dict with trading_posture, sizing_multiplier,
        favored_sectors, avoid_sectors, confidence_adjustment, and gate results.
        Always returns a usable dict — never raises.
    """
    global _cached_context, _cached_at

    now = time.monotonic()
    if _cached_context is not None and (now - _cached_at) < CACHE_TTL_SECONDS:
        return _cached_context

    url = (
        thesis_url
        or os.environ.get("THESIS_URL", "http://192.168.1.42:8060")
    )
    endpoint = f"{url.rstrip('/')}/thesis/current-context"

    try:
        resp = requests.get(endpoint, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            data = resp.json()
            _cached_context = data
            _cached_at = now
            is_fallback = data.get("is_fallback", False)
            posture = data.get("trading_posture", "NEUTRAL")
            sizing = data.get("sizing_multiplier", 1.0)
            logger.info(
                "ThesisClient: fetched context — posture=%s sizing=%.2f fallback=%s",
                posture, sizing, is_fallback,
            )
            return data
        else:
            logger.warning(
                "ThesisClient: THESIS returned HTTP %d — using safe default",
                resp.status_code,
            )
    except requests.exceptions.Timeout:
        logger.warning("ThesisClient: timeout after %.1fs — using safe default", REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.ConnectionError:
        logger.warning("ThesisClient: connection error to %s — using safe default", endpoint)
    except Exception as exc:
        logger.warning("ThesisClient: unexpected error: %s — using safe default", exc)

    # Return safe default on any failure — do not cache failures (retry next call)
    return dict(_SAFE_DEFAULT)


def posture_to_score_adjustment(thesis_ctx: dict) -> float:
    """Convert THESIS trading posture to a score adjustment for agent scoring.

    AGGRESSIVE  → +5  (thesis says conditions are favorable — give picks a boost)
    NEUTRAL     →  0  (no adjustment)
    SELECTIVE   → -5  (thesis says be picky — slight penalty for marginal setups)
    DEFENSIVE   → -15 (thesis says conditions are hostile — meaningful penalty)

    Args:
        thesis_ctx: Thesis context dict from get_thesis_context().

    Returns:
        Float score adjustment to add to the raw agent score.
    """
    posture = str(thesis_ctx.get("trading_posture", "NEUTRAL")).upper()
    adjustments = {
        "AGGRESSIVE": 5.0,
        "NEUTRAL":    0.0,
        "SELECTIVE":  -5.0,
        "DEFENSIVE":  -15.0,
    }
    return adjustments.get(posture, 0.0)


def posture_to_sizing_cap(thesis_ctx: dict) -> float:
    """Convert THESIS posture to a maximum sizing multiplier cap.

    Prevents agents from submitting at full size when THESIS says to be defensive.

    AGGRESSIVE  → 1.0 (no cap — allow full or enhanced sizing from Axiom)
    NEUTRAL     → 1.0 (no cap)
    SELECTIVE   → 0.75 (cap — picks must be selective, smaller size)
    DEFENSIVE   → 0.50 (hard cap — defensive mode, half size maximum)

    Args:
        thesis_ctx: Thesis context dict from get_thesis_context().

    Returns:
        Maximum allowed sizing multiplier (0.0 to 1.0).
    """
    posture = str(thesis_ctx.get("trading_posture", "NEUTRAL")).upper()
    caps = {
        "AGGRESSIVE": 1.0,
        "NEUTRAL":    1.0,
        "SELECTIVE":  0.75,
        "DEFENSIVE":  0.50,
    }
    return caps.get(posture, 1.0)


def is_sector_favored(ticker_sector: Optional[str], thesis_ctx: dict) -> Optional[bool]:
    """Check if a ticker's sector is favored, avoided, or neutral per THESIS.

    Args:
        ticker_sector: Sector string (e.g., "Technology", "Energy"). None if unknown.
        thesis_ctx: Thesis context dict from get_thesis_context().

    Returns:
        True if favored, False if avoided, None if neutral or unknown.
    """
    if not ticker_sector:
        return None

    sector_lower = ticker_sector.lower()
    favored = [s.lower() for s in thesis_ctx.get("favored_sectors", [])]
    avoided = [s.lower() for s in thesis_ctx.get("avoid_sectors", [])]

    if any(sector_lower in f or f in sector_lower for f in favored):
        return True
    if any(sector_lower in a or a in sector_lower for a in avoided):
        return False
    return None


def macro_gate_allows_trading(thesis_ctx: dict) -> bool:
    """Check if THESIS macro gate permits trading.

    If the Druckenmiller hard gate FAILED, THESIS thesis posture is DEFENSIVE.
    Agents should still submit but scoring should reflect the hostile macro.

    Args:
        thesis_ctx: Thesis context dict from get_thesis_context().

    Returns:
        True if macro gate passed or thesis is fallback, False if macro gate failed.
    """
    if thesis_ctx.get("is_fallback", True):
        return True  # No thesis = assume allowed, log warning
    return thesis_ctx.get("macro_gate", "PASS") == "PASS"
