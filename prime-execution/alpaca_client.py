"""
alpaca_client.py — Prime Execution Alpaca Client

Equity-only Alpaca client for Prime swing trades.
Paper trading. No options.
"""

import logging
from typing import Optional

import requests

from config import ALPACA_DATA_URL, ALPACA_PAPER_URL

logger = logging.getLogger("prime_exec.alpaca")


class AlpacaError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body        = body
        super().__init__(f"Alpaca API error {status_code}: {body[:200]}")


class AlpacaClient:
    """
    Paper trading Alpaca client for Prime equity swing trades.

    Args:
        api_key:    Alpaca API key ID.
        secret_key: Alpaca secret key.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        self.api_key    = api_key
        self.secret_key = secret_key
        self.base_url   = ALPACA_PAPER_URL  # Add base_url for position gate checks
        self._headers   = {
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type":        "application/json",
        }

    def get_account(self) -> dict:
        """Fetch account info (equity, cash, buying power)."""
        return self._get(f"{ALPACA_PAPER_URL}/v2/account")

    def get_positions(self) -> list[dict]:
        """Fetch all open equity positions."""
        return self._get(f"{ALPACA_PAPER_URL}/v2/positions")

    def get_position(self, symbol: str) -> Optional[dict]:
        """Fetch a single open position. Returns None if not found."""
        try:
            return self._get(f"{ALPACA_PAPER_URL}/v2/positions/{symbol}")
        except AlpacaError as e:
            if e.status_code == 404:
                return None
            raise

    def close_position(self, symbol: str, qty: Optional[float] = None) -> dict:
        """Close a position (full or partial by qty)."""
        url    = f"{ALPACA_PAPER_URL}/v2/positions/{symbol}"
        params = {"qty": str(qty)} if qty is not None else {}
        resp   = requests.delete(url, headers=self._headers, params=params, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    def place_order(
        self,
        symbol:           str,
        qty:              float,
        side:             str,
        order_type:       str = "market",
        limit_price:      Optional[float] = None,
        time_in_force:    str = "day",
        client_order_id:  Optional[str] = None,
    ) -> dict:
        """
        Place an equity buy or sell order.

        Args:
            symbol:           Ticker symbol.
            qty:              Number of shares (fractional allowed).
            side:             'buy' or 'sell'.
            order_type:       'market' or 'limit'.
            limit_price:      Required for limit orders.
            time_in_force:    'day' or 'gtc'.
            client_order_id:  Optional idempotency key for order recovery.

        Returns:
            Order dict with id and status.
        """
        body: dict = {
            "symbol":        symbol,
            "qty":           str(qty),
            "side":          side,
            "type":          order_type,
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        if client_order_id:
            body["client_order_id"] = client_order_id
        resp = requests.post(f"{ALPACA_PAPER_URL}/v2/orders", json=body,
                             headers=self._headers, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    def get_orders(self, status: str = "open") -> list[dict]:
        """
        Fetch orders filtered by status.
        Added 2026-04-28 by OMNI — called by reconciler but was missing.

        Args:
            status: 'open', 'closed', or 'all'.

        Returns:
            List of order dicts.
        """
        try:
            data = self._get(
                f"{ALPACA_PAPER_URL}/v2/orders",
                params={"status": status, "limit": 100},
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning("get_orders failed (status=%s): %s", status, e)
            return []

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get latest trade price for a stock."""
        try:
            data = self._get(f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest")
            return float(data.get("trade", {}).get("p", 0))
        except Exception as e:
            logger.warning("Latest price failed for %s: %s", symbol, e)
            return None

    def get_order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        """
        Look up an order by its client_order_id (idempotency key).

        Used in the network-error recovery path after a Timeout or ConnectionError
        on order submission, to check whether Alpaca received and placed the order.

        Args:
            client_order_id: The COID we passed to place_order.

        Returns:
            Order dict if found, None if not found or on error.
        """
        try:
            url = f"{ALPACA_PAPER_URL}/v2/orders:by_client_order_id"
            resp = requests.get(
                url,
                headers=self._headers,
                params={"client_order_id": client_order_id},
                timeout=10,
            )
            if resp.status_code == 404:
                return None
            self._raise_for_status(resp)
            return resp.json()
        except AlpacaError as e:
            if e.status_code == 404:
                return None
            logger.warning("get_order_by_client_id failed for %s: %s", client_order_id, e)
            return None
        except Exception as e:
            logger.warning("get_order_by_client_id error for %s: %s", client_order_id, e)
            return None

    # Private alias for backward-compat with any callers using the _ prefix
    _get_order_by_client_id = get_order_by_client_id

    def _get(self, url: str, params: Optional[dict] = None):
        resp = requests.get(url, headers=self._headers, params=params, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code not in (200, 201, 202, 204):
            raise AlpacaError(resp.status_code, resp.text)
