"""
request_executor.py — Universal Alpaca Request Executor
========================================================
Every Alpaca API call goes through here. Zero direct calls allowed.

Responsibilities:
  - Circuit breaker integration
  - Retry with exponential backoff
  - Rate limit handling (429 → wait Retry-After)
  - SSL error recovery (new session)
  - DNS failure handling
  - Request throttling (never exceed 200 req/min)
  - Idempotency via client_order_id
  - Full request/response logging for audit trail
"""
from __future__ import annotations
import logging
import os
import threading
import time
import uuid
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from .error_taxonomy import (
    AlpacaError, AlpacaErrorCode,
    classify_http_error, classify_exception,
    ALPACA_ERROR_REGISTRY,
)

log = logging.getLogger("nexus.alpaca.executor")
_ET = ZoneInfo("America/New_York")

PAPER_BASE  = "https://paper-api.alpaca.markets"
DATA_BASE   = "https://data.alpaca.markets"

# Rate limiting: Alpaca allows ~200 req/min
MAX_REQUESTS_PER_MINUTE = 180   # conservative
REQUEST_WINDOW_S        = 60
DEFAULT_TIMEOUT         = 12    # seconds


def _build_session() -> requests.Session:
    """Build requests session with connection pooling and retry adapter."""
    session = requests.Session()
    retry   = Retry(
        total=0,        # We handle retries ourselves
        backoff_factor=1,
        status_forcelist=[],
    )
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=retry,
    )
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


class RequestExecutor:
    """
    Universal Alpaca request executor with full error handling.

    One instance per AlpacaGuardian (per account).
    Thread-safe — multiple threads can call simultaneously.
    """

    def __init__(
        self,
        system_id:   str,
        api_key:     str,
        secret_key:  str,
        circuit_breaker: CircuitBreaker,
        notify_fn:   Optional[Any] = None,
    ):
        self.system_id      = system_id
        self.api_key        = api_key
        self.secret_key     = secret_key
        self.cb             = circuit_breaker
        self.notify_fn      = notify_fn

        self._session       = _build_session()
        self._session_lock  = threading.Lock()
        self._request_times: list[float] = []
        self._rate_lock     = threading.Lock()

    @property
    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID":     self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type":        "application/json",
        }

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def _throttle(self) -> None:
        """Enforce rate limit — never exceed MAX_REQUESTS_PER_MINUTE."""
        with self._rate_lock:
            now = time.time()
            window_start = now - REQUEST_WINDOW_S
            self._request_times = [t for t in self._request_times
                                   if t > window_start]
            if len(self._request_times) >= MAX_REQUESTS_PER_MINUTE:
                oldest = self._request_times[0]
                wait   = REQUEST_WINDOW_S - (now - oldest) + 0.1
                if wait > 0:
                    log.warning("[%s] Rate throttle: waiting %.1fs", self.system_id, wait)
                    time.sleep(wait)
            self._request_times.append(time.time())

    # ── Session management ────────────────────────────────────────────────────

    def _refresh_session(self) -> None:
        """Create fresh session (SSL recovery)."""
        with self._session_lock:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = _build_session()
            log.info("[%s] Session refreshed", self.system_id)

    # ── Core request ──────────────────────────────────────────────────────────

    def _execute_once(
        self,
        method:   str,
        url:      str,
        params:   Optional[dict] = None,
        json:     Optional[dict] = None,
        timeout:  int = DEFAULT_TIMEOUT,
    ) -> dict:
        """Execute one HTTP request — no retry logic here."""
        self._throttle()

        with self._session_lock:
            resp = self._session.request(
                method  = method,
                url     = url,
                headers = self._headers,
                params  = params,
                json    = json,
                timeout = timeout,
            )

        # Log for audit
        log.debug("[%s] %s %s → %d", self.system_id, method, url, resp.status_code)

        if resp.status_code in (200, 201, 202, 204):
            if resp.content:
                return resp.json()
            return {}

        # Error — classify and raise
        error_code   = classify_http_error(resp.status_code, resp.text)
        retry_after  = None
        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After", 60))
            except (ValueError, TypeError):
                retry_after = 60

        raise AlpacaError(
            status_code  = resp.status_code,
            body         = resp.text,
            error_code   = error_code,
            retry_after  = retry_after,
        )

    def execute(
        self,
        method:   str,
        url:      str,
        params:   Optional[dict] = None,
        json:     Optional[dict] = None,
        timeout:  int = DEFAULT_TIMEOUT,
    ) -> dict:
        """
        Execute request with full error handling:
        - Circuit breaker check
        - Retry with backoff per error type
        - SSL recovery (new session)
        - Rate limit waiting
        - Full error classification
        """
        def _call():
            return self._execute_once(method, url, params, json, timeout)

        last_exc: Optional[Exception] = None

        for attempt in range(1, 4):   # max 3 attempts
            try:
                return self.cb.call(_call)

            except CircuitBreakerOpen as exc:
                # API is known down — don't retry
                raise

            except AlpacaError as exc:
                last_exc = exc
                error_def = ALPACA_ERROR_REGISTRY.get(exc.error_code)
                backoffs  = error_def.backoff_seconds if error_def else [5, 15, 30]

                if exc.error_code in (
                    AlpacaErrorCode.EAL006,   # invalid credentials — don't retry
                    AlpacaErrorCode.EAL020,   # margin call — don't retry
                ):
                    raise

                if exc.error_code == AlpacaErrorCode.EAL002:
                    # Rate limited — wait Retry-After
                    wait = exc.retry_after or 60
                    log.warning("[%s] Rate limited — waiting %ds", self.system_id, wait)
                    time.sleep(wait)
                    continue

                if attempt <= len(backoffs):
                    wait = backoffs[attempt - 1]
                    log.warning(
                        "[%s] %s attempt %d/%d — waiting %ds",
                        self.system_id, exc.error_code, attempt, 3, wait
                    )
                    time.sleep(wait)
                else:
                    break

            except requests.exceptions.SSLError as exc:
                last_exc = exc
                log.error("[%s] SSL error — refreshing session: %s", self.system_id, exc)
                self._refresh_session()
                time.sleep(5)

            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                error_code = classify_exception(exc)
                backoffs   = ALPACA_ERROR_REGISTRY.get(
                    error_code, ALPACA_ERROR_REGISTRY[AlpacaErrorCode.EAL001]
                ).backoff_seconds
                wait = backoffs[min(attempt-1, len(backoffs)-1)]
                log.warning(
                    "[%s] Network error attempt %d/3 — waiting %ds: %s",
                    self.system_id, attempt, wait, exc
                )
                time.sleep(wait)

            except Exception as exc:
                last_exc = exc
                log.error("[%s] Unexpected error: %s", self.system_id, exc)
                raise

        # All retries exhausted
        if last_exc:
            raise last_exc
        raise RuntimeError(f"{self.system_id}: All retries exhausted")

    # ── Convenience methods ───────────────────────────────────────────────────

    def get(self, path: str, params: dict = None, base: str = PAPER_BASE) -> dict:
        return self.execute("GET", f"{base}{path}", params=params)

    def post(self, path: str, json: dict = None, base: str = PAPER_BASE) -> dict:
        return self.execute("POST", f"{base}{path}", json=json)

    def delete(self, path: str, params: dict = None, base: str = PAPER_BASE) -> dict:
        return self.execute("DELETE", f"{base}{path}", params=params)

    def get_data(self, path: str, params: dict = None) -> dict:
        return self.execute("GET", f"{DATA_BASE}{path}", params=params)

    # ── Account ───────────────────────────────────────────────────────────────

    def probe(self) -> Optional[dict]:
        """Probe Alpaca account — never raises."""
        try:
            return self.get("/v2/account")
        except Exception as exc:
            log.warning("[%s] Probe failed: %s", self.system_id, exc)
            return None

    def get_account(self) -> dict:
        return self.get("/v2/account")

    def get_positions(self) -> list:
        result = self.get("/v2/positions")
        return result if isinstance(result, list) else []

    def get_orders(self, status: str = "open", limit: int = 100) -> list:
        result = self.get("/v2/orders", params={"status": status, "limit": limit})
        return result if isinstance(result, list) else []

    def get_order(self, order_id: str) -> Optional[dict]:
        try:
            return self.get(f"/v2/orders/{order_id}")
        except AlpacaError as exc:
            if exc.status_code == 404:
                return None
            raise

    def get_order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        try:
            return self.get(
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_order_id},
            )
        except AlpacaError as exc:
            if exc.status_code == 404:
                return None
            raise

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol:          str,
        qty:             int,
        side:            str,
        order_type:      str = "limit",
        limit_price:     Optional[float] = None,
        time_in_force:   str = "day",
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Place order with automatic idempotency check."""
        coid = client_order_id or str(uuid.uuid4())[:48]

        # Check idempotency — order already placed?
        existing = self.get_order_by_client_id(coid)
        if existing:
            status = existing.get("status", "")
            log.info("[%s] Idempotency: order %s already exists (status=%s)",
                    self.system_id, coid[:8], status)
            return existing

        body: dict = {
            "symbol":          symbol,
            "qty":             str(qty),
            "side":            side,
            "type":            order_type,
            "time_in_force":   time_in_force,
            "client_order_id": coid,
        }
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))

        return self.post("/v2/orders", json=body)

    def place_option_order(
        self,
        contract_symbol: str,
        qty:             int,
        side:            str,
        order_type:      str = "limit",
        limit_price:     Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Place options order with idempotency."""
        coid = client_order_id or str(uuid.uuid4())[:48]

        existing = self.get_order_by_client_id(coid)
        if existing:
            log.info("[%s] Idempotency: option order %s already exists",
                    self.system_id, coid[:8])
            return existing

        body: dict = {
            "symbol":          contract_symbol,
            "qty":             str(qty),
            "side":            side,
            "type":            order_type,
            "time_in_force":   "day",
            "client_order_id": coid,
        }
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))

        return self.post("/v2/orders", json=body)

    def cancel_order_safe(self, order_id: str) -> bool:
        """Cancel only if in cancellable state."""
        order = self.get_order(order_id)
        if not order:
            return False
        status = order.get("status", "")
        if status in ("filled", "partially_filled", "canceled",
                      "cancelled", "expired", "rejected"):
            log.debug("[%s] cancel_safe: %s already %s — skip",
                     self.system_id, order_id[:8], status)
            return False
        try:
            self.delete(f"/v2/orders/{order_id}")
            return True
        except AlpacaError as exc:
            if exc.status_code in (404, 422):
                return False
            raise

    def wait_for_fill(
        self,
        order_id: str,
        timeout_s: int = 30,
        poll_interval: float = 2.0,
    ) -> Optional[dict]:
        """Poll until filled, canceled, or timeout."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            order = self.get_order(order_id)
            if not order:
                return None
            status = order.get("status", "")
            if status == "filled":
                return order
            if status in ("canceled", "cancelled", "expired", "rejected"):
                log.warning("[%s] Order %s: %s", self.system_id, order_id[:8], status)
                return None
            time.sleep(poll_interval)
        log.error("[%s] Order %s: fill timeout after %ds",
                 self.system_id, order_id[:8], timeout_s)
        return None
