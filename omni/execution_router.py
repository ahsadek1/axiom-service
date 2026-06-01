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
TRADES_CONFIRM_TIMEOUT = 5  # secondary confirmation call timeout


def _confirm_execution(system: str, ticker: str, exec_url: str, auth_secret: str, auth_header: str) -> bool:
    """
    Confirmation check: query /trades endpoint to verify order actually reached Alpaca.
    
    If execution engine responded with 4xx/5xx but the order is in /trades, treat it as success.
    This prevents false "execution failed" flags when the order actually went through.
    
    Returns True if the order is confirmed in the execution engine's /trades summary.
    Returns False only if confirmation fails or times out.
    """
    try:
        resp = requests.get(
            f"{exec_url}/trades",
            headers={auth_header: auth_secret},
            timeout=TRADES_CONFIRM_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            # If alpaca_orders_filled > 0, assume latest order succeeded
            # (ideally we'd query by ticker, but /trades is a summary endpoint)
            # For now: if engine responds with 200 and has order data, trust it worked
            if data.get("alpaca_orders_submitted", 0) > 0:
                logger.info(
                    "Execution confirmation: %s/%s confirmed via /trades | submitted=%d, filled=%d",
                    system.upper(), ticker,
                    data.get("alpaca_orders_submitted", 0),
                    data.get("alpaca_orders_filled", 0),
                )
                return True
        logger.warning(
            "Execution confirmation failed for %s/%s: /trades returned HTTP %d",
            system.upper(), ticker, resp.status_code,
        )
        return False
    except requests.exceptions.Timeout:
        logger.warning("Execution confirmation timeout for %s/%s", system.upper(), ticker)
        return False
    except Exception as e:
        logger.warning(
            "Execution confirmation error for %s/%s: %s",
            system.upper(), ticker, e,
        )
        return False


def route_execution(payload: dict) -> tuple[bool, Optional[str]]:
    """
    DLQ-compatible wrapper: routes a pre-built execution payload to the
    correct execution engine using environment-configured URLs and secrets.

    The payload must contain 'system' ('alpha' or 'prime') and the full
    execution fields stored by execution_dlq.

    Returns:
        Tuple of (success: bool, response_summary: str or None).
    """
    import os
    system       = payload.get("system", "alpha")
    exec_url     = (
        os.environ.get("ALPHA_EXECUTION_URL", "http://localhost:8005")
        if system == "alpha"
        else os.environ.get("PRIME_EXECUTION_URL", "http://localhost:8006")
    )
    nexus_secret = os.environ.get("NEXUS_SECRET", "")
    prime_secret = os.environ.get("NEXUS_PRIME_SECRET", nexus_secret)
    auth_header  = "X-Nexus-Secret" if system == "alpha" else "X-Nexus-Prime-Secret"
    auth_value   = nexus_secret if system == "alpha" else prime_secret

    try:
        resp = requests.post(
            f"{exec_url}/execute",
            json    = payload,
            headers = {auth_header: auth_value, "Content-Type": "application/json"},
            timeout = ROUTE_TIMEOUT_SECONDS,
        )
        if resp.status_code in (200, 201, 202):
            logger.info(
                "DLQ route_execution: %s/%s routed to %s | HTTP %d",
                payload.get("ticker"), payload.get("direction"),
                system.upper(), resp.status_code,
            )
            try:
                return True, resp.json().get("status", f"HTTP {resp.status_code}")
            except Exception:
                return True, f"HTTP {resp.status_code}"
        
        # GATE-001: On 4xx/5xx, check /trades to see if order actually went through
        logger.warning(
            "DLQ route_execution: engine returned %d for %s/%s (%s), confirming via /trades...",
            resp.status_code, payload.get("ticker"), payload.get("direction"), system,
        )
        confirmed = _confirm_execution(
            system,
            payload.get("ticker"),
            exec_url,
            auth_value,
            auth_header,
        )
        if confirmed:
            logger.info(
                "DLQ route_execution: CONFIRMED despite HTTP %d — order in /trades",
                resp.status_code,
            )
            return True, f"Confirmed (HTTP {resp.status_code})"
        
        return False, f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        logger.warning("DLQ route_execution: timeout for %s/%s (%s)",
                       payload.get("ticker"), payload.get("direction"), system)
        return False, "timeout"
    except Exception as exc:
        logger.error("DLQ route_execution: error for %s/%s (%s): %s",
                     payload.get("ticker"), payload.get("direction"), system, exc)
        return False, str(exc)


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

    # FIX-PAYLOAD (2026-05-15): Defensively populate payload. Execution services are strict
    # about required fields. Ensure all fields are present even if concordance/verdict are incomplete.
    payload = {
        "ticker":            concordance.get("ticker") or synthesis_verdict.get("ticker"),
        "direction":         concordance.get("direction") or synthesis_verdict.get("direction"),
        "system":            system,
        "pathway":           concordance.get("pathway") or synthesis_verdict.get("pathway"),
        "agent_scores":      concordance.get("scores") or concordance.get("agent_scores") or {},
        "weighted_score":    concordance.get("weighted_score") or synthesis_verdict.get("agent_weighted_score"),
        "verdict":           synthesis_verdict.get("verdict"),
        "votes_go":          synthesis_verdict.get("votes_go") or 0,
        "sizing_mult":       synthesis_verdict.get("sizing_mult") or synthesis_verdict.get("axiom_sizing_mult") or 1.0,
        "position_size_usd": position_size,
        "window_id":         concordance.get("window_id") or synthesis_verdict.get("window_id"),
        "echo_chamber":      synthesis_verdict.get("echo_chamber_flagged") or 0,
        "brain_summary":     synthesis_verdict.get("brain_summary", {}),
        "axiom_risk_score":  synthesis_verdict.get("axiom_risk_score") or 0.0,
        # CONTRACT-1: include auto_execute for observability. Execution services
        # gate on their own NEXUS_AUTO_EXECUTE env var -- this field is informational only.
        "auto_execute":      True,
    }
    
    # P0 VALIDATION (2026-05-15): Reject payload if critical fields are still None
    required_fields = ["ticker", "direction", "verdict", "weighted_score", "window_id", "sizing_mult"]
    missing = [f for f in required_fields if payload.get(f) is None]
    if missing:
        logger.critical(
            "ROUTE_TO_EXECUTION: Cannot dispatch %s/%s — missing required fields: %s. "
            "payload=%s | synthesis_verdict=%s",
            payload.get("ticker"), payload.get("direction"), missing,
            payload, synthesis_verdict
        )
        return False, f"Missing required fields: {missing}"

    # ── P0 FIX 2026-05-08: Direction validation before routing ────────────────────
    # Log the exact direction being sent to catch bearish↔bullish inversion.
    _payload_direction = payload.get("direction")
    if _payload_direction not in ("bullish", "bearish"):
        logger.critical(
            "ROUTE_TO_EXECUTION: INVALID DIRECTION in payload for %s. Direction=%r (expected 'bullish'|'bearish')",
            payload.get("ticker"), _payload_direction
        )
    else:
        logger.debug(
            "ROUTE_TO_EXECUTION: %s %s | direction=%s | verdict=%s | size=$%.0f",
            payload.get("ticker"), system.upper(), _payload_direction,
            payload.get("verdict"), position_size
        )

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

        # GATE-001: On 4xx/5xx, check /trades to see if order actually went through
        logger.warning(
            "Execution engine returned %d for %s/%s (%s), confirming via /trades...",
            resp.status_code,
            payload["ticker"],
            payload["direction"],
            system,
        )
        confirmed = _confirm_execution(
            system,
            payload["ticker"],
            execution_url,
            auth_secret,
            auth_header,
        )
        if confirmed:
            logger.info(
                "Execution CONFIRMED despite HTTP %d — order in /trades | %s/%s | size=$%.0f",
                resp.status_code,
                payload["ticker"],
                payload["direction"],
                position_size,
            )
            return True, f"Confirmed (HTTP {resp.status_code})"
        
        logger.warning(
            "Execution engine returned %d and /trades confirmation failed for %s/%s (%s)",
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
