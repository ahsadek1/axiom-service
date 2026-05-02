"""
execution_router.py — OMNI Execution Routing

Routes GO verdicts to the correct execution engine.
Alpha: options/debit/credit spreads (port 8005)
Prime: swing equity positions (port 8006)
Fire-and-forget: OMNI's responsibility ends at dispatch.
"""

import logging
from typing import Optional

import requests

from config import BASE_POSITION_SIZE  # noqa: F401 — re-exported for callers

logger = logging.getLogger("omni.execution_router")

ROUTE_TIMEOUT_SECONDS = 10


def route_to_execution(
    system:           str,
    alpha_exec_url:   str,
    prime_exec_url:   str,
    auth_secret:      str,
    concordance:      dict,
    synthesis_verdict: dict,
    position_size:    float,
) -> tuple[bool, Optional[str]]:
    """
    Route a GO verdict to the appropriate execution engine.

    Alpha execution receives options trade parameters.
    Prime execution receives swing equity parameters.
    Both receive the full synthesis verdict for their audit trail.

    Args:
        system:            'alpha' or 'prime'.
        alpha_exec_url:    Alpha execution engine URL.
        prime_exec_url:    Prime execution engine URL.
        auth_secret:       Secret for execution engine auth header.
        concordance:       Original concordance payload.
        synthesis_verdict: OMNI synthesis verdict dict.
        position_size:     Final dollar position size.

    Returns:
        Tuple of (success: bool, response_summary: str or None).
    """
    execution_url = alpha_exec_url if system == "alpha" else prime_exec_url
    auth_header   = "X-Nexus-Secret" if system == "alpha" else "X-Nexus-Prime-Secret"

    payload = {
        "ticker":            concordance.get("ticker"),
        "direction":         concordance.get("direction"),
        "system":            system,
        "pathway":           concordance.get("pathway"),
        "agent_scores":      concordance.get("scores", {}),
        "weighted_score":    concordance.get("weighted_score"),
        "verdict":           synthesis_verdict.get("verdict"),
        "votes_go":          synthesis_verdict.get("votes_go"),
        "sizing_mult":       synthesis_verdict.get("sizing_mult"),
        "position_size_usd": position_size,
        "window_id":         concordance.get("window_id"),
        "echo_chamber":      synthesis_verdict.get("echo_chamber_flagged"),
        "brain_summary":     synthesis_verdict.get("brain_summary", {}),
        "axiom_risk_score":  synthesis_verdict.get("axiom_risk_score"),
        # CONTRACT-1: include auto_execute for observability. Execution services
        # gate on their own NEXUS_AUTO_EXECUTE env var -- this field is informational only.
        "auto_execute":      True,
    }

    try:
        resp = requests.post(
            f"{execution_url}/execute",
            json    = payload,
            headers = {auth_header: auth_secret, "Content-Type": "application/json"},
            timeout = ROUTE_TIMEOUT_SECONDS,
        )

        if resp.status_code in (200, 201, 202):
            logger.info(
                "Execution routed to %s: %s/%s | size=$%.0f | HTTP %d",
                system.upper(),
                payload["ticker"],
                payload["direction"],
                position_size,
                resp.status_code,
            )
            try:
                return True, resp.json().get("status", f"HTTP {resp.status_code}")
            except Exception:
                return True, f"HTTP {resp.status_code}"

        logger.warning(
            "Execution engine returned %d for %s/%s (%s)",
            resp.status_code,
            payload["ticker"],
            payload["direction"],
            system,
        )
        return False, f"HTTP {resp.status_code}"

    except requests.exceptions.Timeout:
        logger.warning(
            "Execution routing timed out for %s/%s (%s) after %ds",
            payload["ticker"],
            payload["direction"],
            system,
            ROUTE_TIMEOUT_SECONDS,
        )
        return False, "timeout"

    except Exception as e:
        logger.error(
            "Execution routing failed for %s/%s (%s): %s",
            payload["ticker"],
            payload["direction"],
            system,
            e,
        )
        return False, str(e)[:100]


def calculate_position_size(
    base_size:     float,
    sizing_mult:   float,
    pathway:       str,
) -> float:
    """
    Calculate final dollar position size.

    Args:
        base_size:   Base position size in dollars (e.g. $2,000).
        sizing_mult: Combined sizing multiplier (concordance × Axiom).
        pathway:     Trade pathway (P1/P2/P3/P4) — for logging.

    Returns:
        Final position size in dollars.
    """
    size = round(base_size * sizing_mult, 2)
    logger.info(
        "Position size: $%.0f × %.2f (%s) = $%.0f",
        base_size, sizing_mult, pathway, size,
    )
    return size
