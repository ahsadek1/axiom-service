"""
trading_gate.py — NSP Trading State Gate (hardcoded, not configurable).

Spec requirement:
  Before restarting ANY of: Alpha-Execution, Prime-Execution, OMNI,
  Prime-Buffer, Axiom:

    if open_positions > 0 OR pending_orders > 0:
        defer intervention
        notify SOVEREIGN immediately
        DO NOT PROCEED

This gate CANNOT be bypassed by config. It CANNOT be disabled.
If the gate check itself fails: abort intervention, escalate.

Public API:
    check_trading_gate(service)  → raises TradingGateBlocked if positions open
    GATED_SERVICES               → frozenset of service names this gate covers
"""

import logging
import os
from typing import Optional, Tuple

import requests

logger = logging.getLogger("nsp.trading_gate")

# Services that require trading gate clearance before restart
GATED_SERVICES: frozenset = frozenset({
    "alpha-exec",
    "prime-exec",
    "omni",
    "prime-buffer",
    "axiom",
})

# Endpoints and auth
ALPHA_EXEC_URL: str = os.environ.get("ALPHA_EXEC_URL", "http://localhost:8005")
PRIME_EXEC_URL: str = os.environ.get("PRIME_EXEC_URL", "http://localhost:8006")
NEXUS_SECRET: str = os.environ.get("NEXUS_SECRET", "")
NEXUS_PRIME_SECRET: str = os.environ.get(
    "NEXUS_PRIME_SECRET",
    os.environ.get("NEXUS_SECRET", ""),
)


class TradingGateBlocked(Exception):
    """Raised when an intervention is blocked because positions are open."""

    def __init__(self, reason: str, open_positions: int, pending_orders: int) -> None:
        """Initialise with reason and position counts."""
        self.reason = reason
        self.open_positions = open_positions
        self.pending_orders = pending_orders
        super().__init__(reason)


class TradingGateError(Exception):
    """Raised when the gate check itself fails — intervention must be aborted."""

    def __init__(self, reason: str) -> None:
        """Initialise with failure reason."""
        self.reason = reason
        super().__init__(reason)


def _get_positions_alpha() -> Tuple[int, int]:
    """
    Query Alpha-Execution for open positions and pending orders.

    Returns:
        (open_positions, pending_orders) tuple.

    Raises:
        TradingGateError: if the endpoint is unreachable or returns unexpected data.
    """
    try:
        resp = requests.get(
            f"{ALPHA_EXEC_URL}/positions",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=5,
        )
        if resp.status_code != 200:
            raise TradingGateError(
                f"Alpha-Exec /positions returned HTTP {resp.status_code} — cannot verify trading state"
            )
        data = resp.json()
        return int(data.get("open_positions", 0)), int(data.get("pending_orders", 0))
    except TradingGateError:
        raise
    except Exception as exc:
        raise TradingGateError(
            f"Alpha-Exec /positions unreachable: {exc} — aborting intervention"
        ) from exc


def _get_positions_prime() -> Tuple[int, int]:
    """
    Query Prime-Execution for open positions and pending orders.

    Returns:
        (open_positions, pending_orders) tuple.

    Raises:
        TradingGateError: if the endpoint is unreachable or returns unexpected data.
    """
    try:
        resp = requests.get(
            f"{PRIME_EXEC_URL}/positions",
            headers={"X-Nexus-Secret": NEXUS_PRIME_SECRET},
            timeout=5,
        )
        if resp.status_code != 200:
            raise TradingGateError(
                f"Prime-Exec /positions returned HTTP {resp.status_code} — cannot verify trading state"
            )
        data = resp.json()
        return int(data.get("open_positions", 0)), int(data.get("pending_orders", 0))
    except TradingGateError:
        raise
    except Exception as exc:
        raise TradingGateError(
            f"Prime-Exec /positions unreachable: {exc} — aborting intervention"
        ) from exc


def check_trading_gate(service: str) -> None:
    """
    Enforce the trading state gate for gated services.

    For non-gated services this is a no-op. For gated services, queries
    both execution endpoints and blocks if any positions or orders exist.

    Parameters:
        service: The service name about to be restarted.

    Raises:
        TradingGateBlocked: if open_positions > 0 or pending_orders > 0.
        TradingGateError:   if the gate check itself fails (endpoint unreachable, etc.)
                            — caller must abort and escalate when this is raised.
    """
    if service not in GATED_SERVICES:
        logger.debug("Trading gate: %s is not gated — skipping check", service)
        return

    logger.info("Trading gate: checking positions before %s restart", service)

    try:
        alpha_pos, alpha_orders = _get_positions_alpha()
        prime_pos, prime_orders = _get_positions_prime()
    except TradingGateError:
        # Gate check failed — caller must abort intervention and escalate
        raise

    total_positions = alpha_pos + prime_pos
    total_orders = alpha_orders + prime_orders

    if total_positions > 0 or total_orders > 0:
        reason = (
            f"Trading gate BLOCKED restart of {service}: "
            f"open_positions={total_positions} (alpha={alpha_pos}, prime={prime_pos}), "
            f"pending_orders={total_orders} (alpha={alpha_orders}, prime={prime_orders}). "
            "Intervention deferred — SOVEREIGN notified."
        )
        logger.warning(reason)
        raise TradingGateBlocked(
            reason=reason,
            open_positions=total_positions,
            pending_orders=total_orders,
        )

    logger.info(
        "Trading gate: CLEAR — no open positions or pending orders (service=%s)", service
    )


def is_gated(service: str) -> bool:
    """Return True if the given service requires trading gate clearance."""
    return service in GATED_SERVICES
