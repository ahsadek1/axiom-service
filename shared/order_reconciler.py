"""
order_reconciler.py — OSI-Aware Order Fill Reconciler

Polls Alpaca after order submission to confirm fill before DB write.
A trade is only written to the database after Alpaca confirms fill.

ROOT CAUSE FIX (2026-04-24):
The original implementation polled GET /v2/positions/{ticker} (e.g. /positions/NVDA)
to confirm fills. This always returned 404 for options spreads — Alpaca does not
create a position entry under the underlying equity ticker for multi-leg options.
Options positions are tracked per OSI contract symbol (e.g. NVDA260717P00145000).
The reconciler would poll for 120 seconds, find nothing, and VOID every options trade
even though Alpaca had fully filled the order.

FIX: Poll GET /v2/orders/{order_id} directly. When the order (or all legs for a
multi-leg spread) reaches status "filled", the trade is confirmed. The fill price
is taken from filled_avg_price on the parent order.

VOID reason codes:
  VOID:order_canceled        — Alpaca order status = cancelled/canceled
  VOID:order_rejected        — Alpaca order status = rejected
  VOID:confirmation_timeout  — MAX_WAIT_SEC elapsed without fill
  VOID:alpaca_api_error      — MAX_RETRIES exceeded on Alpaca poll
"""
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger("nexus.order_reconciler")

POLL_INTERVAL_SEC: float = 5.0
MAX_WAIT_SEC: float = 600.0   # 10 minutes — options spreads can take time to fill at limit price
MAX_API_RETRIES: int = 3

# Terminal order statuses that mean the order will never fill
_TERMINAL_VOID_STATUSES = {"canceled", "cancelled", "rejected", "expired", "replaced"}
# Filled statuses
_FILLED_STATUSES = {"filled", "partially_filled"}


class FillStatus(Enum):
    CONFIRMED = "confirmed"
    VOID = "void"


@dataclass
class FillResult:
    """Result of order fill reconciliation."""
    status: FillStatus
    void_reason: Optional[str]    # e.g. "VOID:order_canceled"
    fill_price: Optional[float]
    filled_qty: Optional[float]
    elapsed_sec: float
    order_id: str
    ticker: str


class OrderReconciler:
    """
    Polls Alpaca after order submission to confirm fill before DB write.

    Polls GET /v2/orders/{order_id} directly. Works for single-leg equity
    orders AND multi-leg options spreads. When the order status is "filled"
    (or all legs are "filled"), the trade is confirmed.

    On VOID (timeout/cancel/reject): automatically cancels the open Alpaca order
    so it does not remain as an orphaned live order on the account.

    Usage:
        reconciler = OrderReconciler(alpaca_base_url, alpaca_key, alpaca_secret)
        result = reconciler.confirm_fill(order_id, ticker, expected_qty)
        if result.status == FillStatus.CONFIRMED:
            write_to_db(result.fill_price, result.filled_qty)
        else:
            log_void_trade(result.void_reason)
    """

    def __init__(
        self,
        alpaca_base_url: str,
        alpaca_key: str,
        alpaca_secret: str,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
        max_wait_sec: float = MAX_WAIT_SEC,
        dry_run: bool = False,
    ) -> None:
        self.base_url = alpaca_base_url.rstrip("/")
        self.headers = {
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
        }
        self.poll_interval = poll_interval_sec
        self.max_wait = max_wait_sec
        self.dry_run = dry_run

    def confirm_fill(
        self,
        order_id: str,
        ticker: str,
        expected_qty: float,
    ) -> FillResult:
        """
        Poll Alpaca order status until fill is confirmed or timeout/void.

        Polls GET /v2/orders/{order_id} every POLL_INTERVAL_SEC seconds.
        For multi-leg (spread) orders, fill is confirmed when the parent
        order status is "filled". Fill price comes from filled_avg_price
        on the parent order (net debit/credit for the spread).

        In dry_run mode: returns CONFIRMED immediately without calling Alpaca.
        """
        if self.dry_run:
            logger.info("DRY RUN: Skipping fill confirmation for %s order %s", ticker, order_id)
            return FillResult(
                status=FillStatus.CONFIRMED,
                void_reason=None,
                fill_price=None,
                filled_qty=expected_qty,
                elapsed_sec=0.0,
                order_id=order_id,
                ticker=ticker,
            )

        if not order_id:
            logger.warning("confirm_fill called with empty order_id for %s", ticker)
            return FillResult(
                status=FillStatus.VOID,
                void_reason="VOID:no_order_id",
                fill_price=None, filled_qty=None,
                elapsed_sec=0.0,
                order_id=order_id, ticker=ticker,
            )

        start = time.time()
        api_errors = 0

        while time.time() - start < self.max_wait:
            try:
                order = self._get_order(order_id)
                if order is None:
                    # API error handled below
                    raise RuntimeError("Failed to fetch order")

                status = order.get("status", "unknown")

                # Terminal void conditions
                if status in _TERMINAL_VOID_STATUSES:
                    void_code = "VOID:order_canceled" if "cancel" in status else "VOID:order_rejected"
                    logger.warning(
                        "Order %s for %s reached terminal status: %s",
                        order_id[:8], ticker, status,
                    )
                    return FillResult(
                        status=FillStatus.VOID,
                        void_reason=void_code,
                        fill_price=None, filled_qty=None,
                        elapsed_sec=time.time() - start,
                        order_id=order_id, ticker=ticker,
                    )

                # Confirmed fill
                if status == "filled":
                    filled_qty = float(order.get("filled_qty") or expected_qty)
                    fill_price_raw = order.get("filled_avg_price")
                    fill_price = abs(float(fill_price_raw)) if fill_price_raw is not None else None

                    # Fallback: if filled_avg_price absent, check position for avg_entry_price
                    if fill_price is None:
                        position = self._get_position(ticker)
                        if position is not None:
                            price_raw = position.get("avg_entry_price")
                            if price_raw is not None:
                                fill_price = abs(float(price_raw))
                                logger.debug(
                                    "Fill price for %s order %s fetched from position fallback: %.4f",
                                    ticker, order_id[:8], fill_price,
                                )

                    elapsed = time.time() - start
                    logger.info(
                        "Fill confirmed for %s order %s: qty=%.2f price=%s (%.1fs)",
                        ticker, order_id[:8], filled_qty,
                        f"{fill_price:.4f}" if fill_price else "N/A", elapsed,
                    )
                    return FillResult(
                        status=FillStatus.CONFIRMED,
                        void_reason=None,
                        fill_price=fill_price,
                        filled_qty=filled_qty,
                        elapsed_sec=elapsed,
                        order_id=order_id,
                        ticker=ticker,
                    )

                # Partial fill detected: check qty deviation vs expected
                if status == "partially_filled":
                    position = self._get_position(ticker)
                    if position is not None:
                        actual_qty = float(position.get("qty") or 0)
                        deviation = abs(actual_qty - expected_qty) / expected_qty if expected_qty else 1.0
                        if deviation > 0.05:
                            elapsed = time.time() - start
                            logger.warning(
                                "Partial fill detected for %s order %s: expected=%.2f actual=%.2f "
                                "(%.0f%% deviation) → VOID:partial_fill",
                                ticker, order_id[:8], expected_qty, actual_qty, deviation * 100,
                            )
                            return FillResult(
                                status=FillStatus.VOID,
                                void_reason="VOID:partial_fill",
                                fill_price=None,
                                filled_qty=actual_qty,
                                elapsed_sec=elapsed,
                                order_id=order_id,
                                ticker=ticker,
                            )

                # Still pending/accepted/held — keep polling
                logger.debug(
                    "Order %s for %s status=%s — polling (%.0fs elapsed)",
                    order_id[:8], ticker, status, time.time() - start,
                )
                api_errors = 0
                time.sleep(self.poll_interval)

            except Exception as e:
                api_errors += 1
                logger.warning(
                    "Alpaca order poll error for %s (attempt %d/%d): %s",
                    ticker, api_errors, MAX_API_RETRIES, e,
                )
                if api_errors >= MAX_API_RETRIES:
                    return FillResult(
                        status=FillStatus.VOID,
                        void_reason="VOID:alpaca_api_error",
                        fill_price=None, filled_qty=None,
                        elapsed_sec=time.time() - start,
                        order_id=order_id, ticker=ticker,
                    )
                time.sleep(self.poll_interval)

        # Timeout — order did not fill within MAX_WAIT_SEC.
        # Rescue check: Alpaca paper options (mleg) orders can have fill-status lag
        # where the parent order status does not immediately reflect "filled" even
        # after the legs are confirmed. Do one final check before voiding —
        # if Alpaca now shows the order as filled, treat it as confirmed.
        try:
            rescue_order = self._get_order(order_id)
            if rescue_order is not None and rescue_order.get("status") == "filled":
                filled_qty  = float(rescue_order.get("filled_qty") or expected_qty)
                price_raw   = rescue_order.get("filled_avg_price")
                fill_price  = abs(float(price_raw)) if price_raw is not None else None
                elapsed     = time.time() - start
                logger.warning(
                    "Rescue: order %s for %s was filled on Alpaca but status lag delayed "
                    "confirmation. Treating as CONFIRMED (elapsed: %.0fs).",
                    order_id[:8], ticker, elapsed,
                )
                return FillResult(
                    status=FillStatus.CONFIRMED,
                    void_reason=None,
                    fill_price=fill_price,
                    filled_qty=filled_qty,
                    elapsed_sec=elapsed,
                    order_id=order_id,
                    ticker=ticker,
                )
        except Exception as _rescue_err:
            logger.warning(
                "Rescue check failed for %s order %s: %s",
                ticker, order_id[:8], _rescue_err,
            )

        elapsed = time.time() - start
        logger.error(
            "Fill confirmation timeout for %s order %s after %.0fs — cancelling orphaned order",
            ticker, order_id[:8], elapsed,
        )
        # CRITICAL: Cancel the live Alpaca order on timeout so it cannot fill
        # later as an orphan (untracked position on Alpaca, unknown to Nexus DB).
        self._cancel_order(order_id, ticker)
        return FillResult(
            status=FillStatus.VOID,
            void_reason="VOID:confirmation_timeout",
            fill_price=None, filled_qty=None,
            elapsed_sec=elapsed,
            order_id=order_id, ticker=ticker,
        )

    def _cancel_order(self, order_id: str, ticker: str) -> None:
        """
        Cancel an open Alpaca order. Best-effort — logs but does not raise.
        Called on VOID to prevent orphaned orders remaining live on Alpaca.
        """
        try:
            resp = requests.delete(
                f"{self.base_url}/v2/orders/{order_id}",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code in (200, 204):
                logger.info(
                    "Orphan cancel: order %s for %s cancelled on Alpaca",
                    order_id[:8], ticker,
                )
            elif resp.status_code == 422:
                # 422 = order already in a terminal state (filled/cancelled) — not an error
                logger.info(
                    "Orphan cancel: order %s for %s already terminal on Alpaca (HTTP 422)",
                    order_id[:8], ticker,
                )
            else:
                logger.warning(
                    "Orphan cancel: unexpected HTTP %d for order %s (%s): %s",
                    resp.status_code, order_id[:8], ticker, resp.text[:200],
                )
        except Exception as e:
            logger.warning(
                "Orphan cancel failed for order %s (%s): %s",
                order_id[:8], ticker, e,
            )

    def _get_order(self, order_id: str) -> Optional[dict]:
        """
        Fetch order from Alpaca by order ID. Returns None on error.
        """
        try:
            resp = requests.get(
                f"{self.base_url}/v2/orders/{order_id}",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(
                "Alpaca GET /orders/%s returned HTTP %d: %s",
                order_id[:8], resp.status_code, resp.text[:200],
            )
        except Exception as e:
            logger.warning("Failed to fetch order %s: %s", order_id[:8], e)
        return None

    # ── Legacy compatibility shim ─────────────────────────────────────────────
    def _get_order_status(self, order_id: str) -> Optional[str]:
        """Legacy: get order status string. Use _get_order() for full details."""
        order = self._get_order(order_id)
        return order.get("status") if order else None

    def _get_position(self, ticker: str) -> Optional[dict]:
        """
        Legacy: get Alpaca equity position by ticker.
        NOTE: This does NOT work for options — options positions are keyed
        by OSI contract symbol, not by underlying ticker. This method is
        retained only for legacy callers. New code should use _get_order().
        """
        try:
            resp = requests.get(
                f"{self.base_url}/v2/positions/{ticker}",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                return None
        except Exception:
            raise
        return None
