"""
alpaca_client.py — Alpaca Paper Trading API Client

Wraps the Alpaca REST API for paper trading.
Options and equity support. All calls include proper auth headers.
Raises AlpacaError on API failures — callers handle gracefully.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from config import ALPACA_DATA_URL, ALPACA_PAPER_URL

logger = logging.getLogger("alpha_exec.alpaca")


class AlpacaError(Exception):
    """Raised on Alpaca API errors with status code and body."""
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body        = body
        super().__init__(f"Alpaca API error {status_code}: {body[:200]}")


class AlpacaClient:
    """
    Paper trading Alpaca REST API client.

    Args:
        api_key:    Alpaca API key ID.
        secret_key: Alpaca secret key.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        self.api_key    = api_key
        self.secret_key = secret_key
        self._headers   = {
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type":        "application/json",
        }

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        """
        Fetch Alpaca account info (equity, cash, buying power).

        Returns:
            Account dict with equity, cash, buying_power fields.

        Raises:
            AlpacaError: On non-2xx response.
        """
        return self._get(f"{ALPACA_PAPER_URL}/v2/account")

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """
        Fetch all open positions.

        Returns:
            List of position dicts.
        """
        return self._get(f"{ALPACA_PAPER_URL}/v2/positions")

    def get_position(self, symbol: str) -> Optional[dict]:
        """
        Fetch a single open position by symbol.

        Args:
            symbol: Asset symbol or option contract symbol.

        Returns:
            Position dict or None if not found.
        """
        try:
            return self._get(f"{ALPACA_PAPER_URL}/v2/positions/{symbol}")
        except AlpacaError as e:
            if e.status_code == 404:
                return None
            raise

    def close_position(self, symbol: str, qty: Optional[float] = None) -> dict:
        """
        Close a position (full or partial).

        Args:
            symbol: Asset symbol.
            qty:    Quantity to close. None = close all.

        Returns:
            Order dict from Alpaca.
        """
        url    = f"{ALPACA_PAPER_URL}/v2/positions/{symbol}"
        params = {}
        if qty is not None:
            params["qty"] = str(qty)
        resp = requests.delete(url, headers=self._headers, params=params, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol:      str,
        qty:         float,
        side:        str,       # 'buy' or 'sell'
        order_type:  str = "market",
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
    ) -> dict:
        """
        Place an equity order.

        Args:
            symbol:        Ticker symbol.
            qty:           Number of shares.
            side:          'buy' or 'sell'.
            order_type:    'market' or 'limit'.
            limit_price:   Required if order_type='limit'.
            time_in_force: 'day', 'gtc', etc.

        Returns:
            Order dict with id, status, and fill info.
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

        resp = requests.post(
            f"{ALPACA_PAPER_URL}/v2/orders",
            json    = body,
            headers = self._headers,
            timeout = 10,
        )
        self._raise_for_status(resp)
        return resp.json()

    def place_option_order(
        self,
        contract_symbol: str,
        qty:             int,
        side:            str,       # 'buy' or 'sell'
        order_type:      str = "market",
        limit_price:     Optional[float] = None,
    ) -> dict:
        """
        Place a single-leg options order.

        Args:
            contract_symbol: Option contract symbol (e.g. 'NVDA250522P00900000').
            qty:             Number of contracts.
            side:            'buy' or 'sell'.
            order_type:      'market' or 'limit'.
            limit_price:     Required for limit orders.

        Returns:
            Order dict from Alpaca.
        """
        body: dict = {
            "symbol":        contract_symbol,
            "qty":           str(qty),
            "side":          side,
            "type":          order_type,
            "time_in_force": "day",
        }
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))

        resp = requests.post(
            f"{ALPACA_PAPER_URL}/v2/orders",
            json    = body,
            headers = self._headers,
            timeout = 10,
        )
        self._raise_for_status(resp)
        return resp.json()

    def place_spread_order(
        self,
        legs:        list[dict],
        qty:         int = 1,
        order_type:  str = "limit",
        limit_debit: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """
        Place a multi-leg options spread order.

        Args:
            legs:             List of leg dicts with symbol, side, ratio_qty.
            qty:              Number of spread contracts to trade.
            order_type:       'limit' (recommended for spreads).
            limit_debit:      Net debit/credit limit for the spread.
            client_order_id:  Optional idempotency key for order recovery.

        Returns:
            Order dict from Alpaca.
        """
        body: dict = {
            "order_class":   "mleg",
            "type":          order_type,
            "time_in_force": "day",
            "qty":           str(qty),
            "legs":          legs,
        }
        if limit_debit is not None:
            body["limit_price"] = str(round(limit_debit, 2))
        if client_order_id:
            body["client_order_id"] = client_order_id

        resp = requests.post(
            f"{ALPACA_PAPER_URL}/v2/orders",
            json    = body,
            headers = self._headers,
            timeout = 10,
        )
        self._raise_for_status(resp)
        return resp.json()

    # ── Market Data ───────────────────────────────────────────────────────────

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """
        Get the latest trade price for a stock or ETF.

        Args:
            symbol: Ticker symbol.

        Returns:
            Latest trade price as float, or None if unavailable.
        """
        try:
            data  = self._get(
                f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest"
            )
            return float(data.get("trade", {}).get("p", 0))
        except Exception as e:
            logger.warning("Latest price failed for %s: %s", symbol, e)
            return None

    def get_option_contracts(
        self,
        underlying:      str,
        expiration_date: Optional[str] = None,
        option_type:     str = "put",   # 'call' or 'put'
        strike_price_lte: Optional[float] = None,
        strike_price_gte: Optional[float] = None,
        limit:           int = 20,
    ) -> list[dict]:
        """
        Fetch available option contracts for an underlying symbol.

        Args:
            underlying:       Stock or ETF symbol.
            expiration_date:  Target expiry in 'YYYY-MM-DD' format. Optional —
                              if omitted, returns all available expirations.
            option_type:      'call' or 'put'.
            strike_price_lte: Maximum strike filter.
            strike_price_gte: Minimum strike filter.
            limit:            Max results.

        Returns:
            List of option contract dicts.
        """
        params: dict = {
            "underlying_symbols": underlying,
            "type":               option_type,
            "limit":              limit,
        }
        if expiration_date is not None:
            params["expiration_date"] = expiration_date
        if strike_price_lte:
            params["strike_price_lte"] = str(strike_price_lte)
        if strike_price_gte:
            params["strike_price_gte"] = str(strike_price_gte)

        try:
            data = self._get(f"{ALPACA_PAPER_URL}/v2/options/contracts", params=params)
            return data.get("option_contracts", [])
        except Exception as e:
            logger.warning("Options chain fetch failed for %s: %s", underlying, e)
            return []

    def get_orders(self, status: str = "open") -> list[dict]:
        """
        Fetch orders filtered by status.

        Args:
            status: 'open', 'closed', or 'all'.

        Returns:
            List of order dicts.
        """
        return self._get(f"{ALPACA_PAPER_URL}/v2/orders", params={"status": status, "limit": 100})

    def get_order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        """
        Look up an order by its client_order_id (idempotency key).

        Used in the network-error recovery path: after a Timeout or ConnectionError
        on order submission, we don't know if Alpaca actually received and placed
        the order. This method checks directly so we can recover the order_id and
        avoid duplicate submissions.

        Args:
            client_order_id: The COID we passed to place_spread_order.

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

    # ── Private alias for backward-compat with recovery path call ─────────────
    _get_order_by_client_id = get_order_by_client_id

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get(self, url: str, params: Optional[dict] = None):
        """Execute a GET request and return parsed JSON."""
        resp = requests.get(url, headers=self._headers, params=params, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        """Raise AlpacaError for non-2xx responses."""
        if resp.status_code not in (200, 201, 202, 204):
            raise AlpacaError(resp.status_code, resp.text)
