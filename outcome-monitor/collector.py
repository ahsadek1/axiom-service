"""
collector.py — Pulls data from Nexus service /health endpoints and Alpaca API.

All collection is read-only. Any unreachable endpoint returns a DOWN snapshot
and never blocks the rest of the cycle.
"""

import logging
from typing import Dict

import requests

from config import (
    ALPACA_BASE_URL,
    ALPACA_V1_KEY,
    ALPACA_V1_SECRET,
    ALPACA_V2_KEY,
    ALPACA_V2_SECRET,
    HTTP_TIMEOUT,
    SERVICE_URLS,
)
from models import AlpacaSnapshot, ServiceSnapshot

logger = logging.getLogger(__name__)


def collect_service(name: str, url: str) -> ServiceSnapshot:
    """
    Poll a single service /health endpoint.

    Args:
        name: Service name for logging and identification.
        url:  Full URL to the /health endpoint.

    Returns:
        ServiceSnapshot with status="UP" on HTTP 200, "DOWN" on any error.
    """
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return ServiceSnapshot(name=name, status="UP", data=resp.json())
        return ServiceSnapshot(
            name=name, status="DOWN", error=f"HTTP {resp.status_code}"
        )
    except Exception as e:
        logger.warning("Service %s unreachable: %s", name, e)
        return ServiceSnapshot(name=name, status="DOWN", error=str(e))


def collect_all_services() -> Dict[str, ServiceSnapshot]:
    """
    Collect health snapshots from all configured Nexus services.

    Returns:
        Dict mapping service name → ServiceSnapshot. Every configured service
        is represented; DOWN snapshots stand in for unreachable services.
    """
    return {name: collect_service(name, url) for name, url in SERVICE_URLS.items()}


def collect_alpaca_account(account_id: str, key: str, secret: str) -> AlpacaSnapshot:
    """
    Collect account data from Alpaca paper API.

    Args:
        account_id: Human label ("v1" or "v2") for identification.
        key:        Alpaca API key ID.
        secret:     Alpaca API secret key.

    Returns:
        AlpacaSnapshot with buying_power and open_positions on success,
        status="DOWN" on any network or parse error.
    """
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    try:
        acct_resp = requests.get(
            f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=HTTP_TIMEOUT
        )
        acct = acct_resp.json()
        pos_resp = requests.get(
            f"{ALPACA_BASE_URL}/v2/positions", headers=headers, timeout=HTTP_TIMEOUT
        )
        positions = pos_resp.json()
        return AlpacaSnapshot(
            account_id=account_id,
            status="UP",
            buying_power=float(acct.get("buying_power", 0)),
            portfolio_value=float(acct.get("portfolio_value", 0)),
            open_positions=len(positions) if isinstance(positions, list) else 0,
        )
    except Exception as e:
        logger.warning("Alpaca %s unreachable: %s", account_id, e)
        return AlpacaSnapshot(account_id=account_id, status="DOWN", error=str(e))


def collect_alpaca() -> Dict[str, AlpacaSnapshot]:
    """
    Collect from both Alpaca V1 and V2 paper accounts.

    Returns:
        Dict with keys "v1" and "v2", each holding an AlpacaSnapshot.
    """
    return {
        "v1": collect_alpaca_account("v1", ALPACA_V1_KEY, ALPACA_V1_SECRET),
        "v2": collect_alpaca_account("v2", ALPACA_V2_KEY, ALPACA_V2_SECRET),
    }
