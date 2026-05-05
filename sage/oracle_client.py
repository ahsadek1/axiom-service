"""
oracle_client.py — ORACLE Intelligence Client

Queries ORACLE for full 7-engine context packets.
Never raises — returns None on any failure so callers can skip the ticker.
"""

import logging
import requests
from typing import Optional

logger = logging.getLogger("sage.oracle")

ORACLE_TIMEOUT_S = 30


def fetch_context(
    ticker: str, oracle_url: str, oracle_headers: dict
) -> Optional[dict]:
    """
    Fetch the full 7-engine context packet for a ticker from ORACLE.

    Args:
        ticker: Stock ticker symbol (e.g. 'AAPL')
        oracle_url: Base URL for ORACLE service
        oracle_headers: Auth headers dict with X-Oracle-Secret

    Returns:
        Context packet as dict, or None if ORACLE is unavailable/times out.
        Never raises.
    """
    url = f"{oracle_url}/oracle/context/{ticker}"
    try:
        resp = requests.get(url, headers=oracle_headers, timeout=ORACLE_TIMEOUT_S)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(
            "ORACLE returned %d for %s — skipping ticker", resp.status_code, ticker
        )
        return None
    except requests.exceptions.Timeout:
        logger.warning("ORACLE timeout (>%ds) for %s — skipping ticker", ORACLE_TIMEOUT_S, ticker)
        return None
    except Exception as e:
        logger.warning("ORACLE fetch error for %s: %s — skipping ticker", ticker, e)
        return None
