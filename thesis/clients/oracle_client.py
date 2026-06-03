"""
OracleClient — HTTP client for the ORACLE intelligence hub (port 8007).

Fetches current market data and converts it into an OracleData snapshot.
All methods catch exceptions and return safe defaults — callers never
need to handle OracleClient errors.

Fixed 2026-05-04: /oracle/market_data never existed (404 since day 1).
Correct endpoint is /oracle/macro (macro data) + /oracle/context/SPY (price).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx

from models import OracleData

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0  # seconds


class OracleClient:
    """Async HTTP client for ORACLE market data fetching.

    Args:
        base_url: Base URL of the ORACLE service (e.g. http://192.168.1.146:8007).
        auth_token: Value for the X-Oracle-Secret header.
    """

    def __init__(self, base_url: str, auth_token: str) -> None:
        """Store connection parameters; no connection opened yet."""
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Oracle-Secret": auth_token}

    async def fetch_market_data(self) -> OracleData:
        """Fetch current market snapshot from ORACLE.

        Calls GET /oracle/macro for macro indicators (VIX, yields, spreads).
        Calls GET /oracle/context/SPY for SPY price data.
        On any network or parsing error, returns an empty OracleData() so
        callers always receive a valid object.

        Returns:
            OracleData populated with whatever ORACLE provides, or empty on error.
        """
        macro_data: Dict[str, Any] = {}
        spy_price: float | None = None

        # Step 1: fetch macro indicators
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._base_url}/oracle/macro",
                    headers=self._headers,
                )
                if resp.status_code == 200:
                    raw = resp.json()
                    # Unwrap nested 'macro' key
                    macro_data = raw.get("macro", raw)
                    logger.debug("OracleClient: /oracle/macro fetched OK")
                else:
                    logger.warning(
                        "OracleClient: /oracle/macro returned HTTP %s", resp.status_code
                    )
        except Exception as exc:
            logger.warning("OracleClient.fetch_market_data() /oracle/macro failed: %s", exc)

        # Step 2: fetch SPY price from context endpoint
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._base_url}/oracle/context/SPY",
                    headers=self._headers,
                )
                if resp.status_code == 200:
                    ctx = resp.json()
                    price_block = ctx.get("price", {})
                    spy_price = price_block.get("last") or price_block.get("price")
                    logger.debug("OracleClient: /oracle/context/SPY fetched OK, price=%s", spy_price)
                else:
                    logger.warning(
                        "OracleClient: /oracle/context/SPY returned HTTP %s", resp.status_code
                    )
        except Exception as exc:
            logger.warning("OracleClient.fetch_market_data() /oracle/context/SPY failed: %s", exc)

        if not macro_data and spy_price is None:
            logger.error(
                "OracleClient: all endpoints failed — returning empty OracleData"
            )
            return OracleData()

        return self._parse_response(macro_data, spy_price)

    async def check_health(self) -> bool:
        """Ping ORACLE health endpoint.

        Returns:
            True if ORACLE responds with 200, False otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._base_url}/oracle/health",
                    headers=self._headers,
                )
                return resp.status_code == 200
        except Exception as exc:
            logger.warning("OracleClient.check_health() failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_response(self, macro: Dict[str, Any], spy_price: float | None) -> OracleData:
        """Map ORACLE macro dict + SPY price to OracleData model.

        /oracle/macro response (unwrapped from 'macro' key):
          vix, fed_funds_rate, yield_2y, yield_10y, yield_spread, hy_spread

        Tolerates missing or extra fields gracefully.

        Args:
            macro: Unwrapped macro dict from /oracle/macro response.
            spy_price: SPY last price from /oracle/context/SPY, or None.

        Returns:
            OracleData with whatever fields are present.
        """
        try:
            # yield_spread in ORACLE is basis points (e.g. 52.0 = 52bp = 0.52%)
            yield_spread = macro.get("yield_spread")

            return OracleData(
                spy_price=spy_price,
                spy_change_pct=None,  # not available from macro endpoint
                vix_level=macro.get("vix"),
                vix_change=None,
                ten_year_yield=macro.get("yield_10y"),
                two_year_yield=macro.get("yield_2y"),
                yield_curve_spread=yield_spread,
                hy_spread=macro.get("hy_spread"),
                dxy=macro.get("dxy"),
                fed_funds_rate=macro.get("fed_funds_rate"),
                sp500_pe=None,
                put_call_ratio=None,
                aaii_bull_pct=None,
                timestamp=str(macro.get("timestamp", "")),
            )
        except Exception as exc:
            logger.error(
                "OracleClient._parse_response() failed: %s — returning empty OracleData",
                exc,
                exc_info=True,
            )
            return OracleData()
