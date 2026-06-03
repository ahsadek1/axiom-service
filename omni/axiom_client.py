"""
axiom_client.py — OMNI → Axiom Risk Assessment Client

Calls Axiom /assess before every synthesis.
Axiom's risk score and hard stops are passed to all 4 brains.
Hard stops block execution regardless of brain votes.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("omni.axiom_client")

AXIOM_TIMEOUT_SECONDS = 8


def assess_ticker(
    axiom_url:    str,
    axiom_secret: str,
    ticker:       str,
) -> Optional[dict]:
    """
    Call Axiom /assess for a single ticker's risk assessment.

    Args:
        axiom_url:    Base URL of Axiom service (e.g. http://localhost:8001).
        axiom_secret: X-Axiom-Secret header value.
        ticker:       Stock ticker symbol.

    Returns:
        Risk assessment dict from Axiom, or None if Axiom is unreachable.
        None means OMNI proceeds without Axiom data — logged as warning.
    """
    try:
        resp = requests.post(
            f"{axiom_url}/assess",
            json    = {"ticker": ticker},
            headers = {"X-Axiom-Secret": axiom_secret, "Content-Type": "application/json"},
            timeout = AXIOM_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        result = resp.json()
        logger.info(
            "Axiom assess: %s | risk=%.1f | sizing=%.2f | hard_stops=%s",
            ticker,
            result.get("risk_score", 0),
            result.get("sizing_mult", 1.0),
            result.get("hard_stops", []),
        )
        return result

    except requests.exceptions.Timeout:
        # Pass B fix (V9): fail-safe on Axiom unreachable — hard stop blocks execution.
        # Original returned hard_stops=[] which allowed trades to fire without regime check.
        logger.error(
            "Axiom /assess timed out for %s — BLOCKING execution (fail-safe). "
            "Hard stop: AXIOM_UNREACHABLE", ticker
        )
        return {
            "ticker":      ticker,
            "error":       "timeout",
            "hard_stops":  ["AXIOM_UNREACHABLE"],
            "sizing_mult": 0.0,
            "risk_score":  None,
        }

    except Exception as e:
        # Pass B fix (V9): any Axiom failure = hard stop, not silent pass-through.
        logger.error(
            "Axiom /assess failed for %s: %s — BLOCKING execution (fail-safe). "
            "Hard stop: AXIOM_UNREACHABLE", ticker, e
        )
        return {
            "ticker":      ticker,
            "error":       str(e)[:100],
            "hard_stops":  ["AXIOM_UNREACHABLE"],
            "sizing_mult": 0.0,
            "risk_score":  None,
        }


def get_regime(axiom_url: str, axiom_secret: str) -> Optional[dict]:
    """
    Fetch the current market regime from Axiom.

    Args:
        axiom_url:    Base URL of Axiom service.
        axiom_secret: X-Axiom-Secret header value.

    Returns:
        Regime dict or None if Axiom unreachable.
    """
    try:
        resp = requests.get(
            f"{axiom_url}/regime",
            headers = {"X-Axiom-Secret": axiom_secret},
            timeout = AXIOM_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Axiom /regime fetch failed: %s", e)
        return None
