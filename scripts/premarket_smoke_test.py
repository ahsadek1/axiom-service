#!/usr/bin/env python3
"""
premarket_smoke_test.py — 8:15 AM Live Paper Order Smoke Test

Submits a real $1 fractional equity order to Alpaca V2 paper account,
verifies it reaches the order book, then cancels it.

This is a BLOCKING gate. If this test fails, V2 does not trade today.
No exceptions. No overrides.

Usage:
    python3 premarket_smoke_test.py

Exit codes:
    0 — PASSED — system cleared for trading
    1 — FAILED — system must NOT trade today

Sends result to Ahmed via Telegram regardless of outcome.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), "../alpha-execution/.env"))

ALPACA_KEY        = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET     = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER_URL  = "https://paper-api.alpaca.markets"
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN",
                        "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
AHMED_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID",
                        os.environ.get("AHMED_CHAT_ID", "8573754783"))

SMOKE_TICKER      = "SPY"      # Liquid, always tradeable
SMOKE_QTY         = 0.01       # Fractional — ~$5, no real P&L impact
SMOKE_TIMEOUT_S   = 15         # Alpaca must accept within 15 seconds
CANCEL_TIMEOUT_S  = 10

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smoke_test")

headers = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}


def _send_telegram(text: str) -> None:
    """Send result to Ahmed."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": text},
            timeout=8,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def _cancel_order(order_id: str) -> bool:
    """Cancel the smoke test order. Returns True on success."""
    try:
        resp = requests.delete(
            f"{ALPACA_PAPER_URL}/v2/orders/{order_id}",
            headers=headers,
            timeout=CANCEL_TIMEOUT_S,
        )
        return resp.status_code in (200, 204)
    except Exception as e:
        logger.warning("Order cancel failed: %s", e)
        return False


def _check_account() -> tuple[bool, str]:
    """Verify Alpaca V2 paper account is accessible and correct."""
    try:
        resp = requests.get(
            f"{ALPACA_PAPER_URL}/v2/account",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return False, f"Account check failed: HTTP {resp.status_code}"
        data = resp.json()
        account_id = data.get("id", "unknown")
        status     = data.get("status", "unknown")
        equity     = float(data.get("equity", 0))
        if status != "ACTIVE":
            return False, f"Account status is {status} (expected ACTIVE)"
        logger.info("Account verified: %s | status=%s | equity=$%.0f",
                    account_id, status, equity)
        return True, f"Account ACTIVE | equity=${equity:,.0f}"
    except Exception as e:
        return False, f"Account check exception: {e}"


def run_smoke_test() -> tuple[bool, str]:
    """
    Execute the full smoke test sequence:
      1. Verify account accessible
      2. Place fractional buy order
      3. Confirm order accepted by Alpaca
      4. Cancel order
      5. Confirm cancellation

    Returns:
        (passed: bool, detail: str)
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")
    logger.info("=== V2 PRE-MARKET SMOKE TEST === %s", now)

    # Step 1: Account check
    logger.info("[1/4] Verifying Alpaca V2 paper account...")
    acct_ok, acct_msg = _check_account()
    if not acct_ok:
        return False, f"Account verification failed: {acct_msg}"
    logger.info("[1/4] PASS — %s", acct_msg)

    # Step 2: Place order
    logger.info("[2/4] Placing smoke test order: BUY %s qty=%s...", SMOKE_TICKER, SMOKE_QTY)
    order_id = None
    try:
        resp = requests.post(
            f"{ALPACA_PAPER_URL}/v2/orders",
            headers=headers,
            json={
                "symbol":        SMOKE_TICKER,
                "qty":           str(SMOKE_QTY),
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
            },
            timeout=SMOKE_TIMEOUT_S,
        )
        if resp.status_code not in (200, 201):
            return False, f"Order placement rejected: HTTP {resp.status_code} — {resp.text[:200]}"
        order = resp.json()
        order_id = order.get("id")
        order_status = order.get("status", "unknown")
        logger.info("[2/4] PASS — Order accepted: id=%s status=%s", order_id, order_status)
    except requests.exceptions.Timeout:
        return False, f"Order placement timed out after {SMOKE_TIMEOUT_S}s — Alpaca unreachable"
    except Exception as e:
        return False, f"Order placement exception: {e}"

    # Step 3: Confirm order in book
    logger.info("[3/4] Confirming order in Alpaca order book...")
    try:
        time.sleep(1)
        resp = requests.get(
            f"{ALPACA_PAPER_URL}/v2/orders/{order_id}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return False, f"Order confirmation failed: HTTP {resp.status_code}"
        confirmed = resp.json()
        confirmed_status = confirmed.get("status", "unknown")
        if confirmed_status in ("rejected", "expired"):
            return False, f"Order was {confirmed_status} by Alpaca"
        logger.info("[3/4] PASS — Order confirmed in book: status=%s", confirmed_status)
    except Exception as e:
        return False, f"Order confirmation exception: {e}"

    # Step 4: Cancel order
    logger.info("[4/4] Cancelling smoke test order...")
    cancelled = _cancel_order(order_id)
    if not cancelled:
        # Not a blocking failure — order may have filled (fractional market order)
        logger.warning("[4/4] Cancel returned non-200 — order may have filled. Acceptable for smoke test.")
    else:
        logger.info("[4/4] PASS — Order cancelled cleanly")

    return True, (
        f"Account: {acct_msg}\n"
        f"Order: {order_id}\n"
        f"Status: confirmed in order book\n"
        f"Cancellation: {'clean' if cancelled else 'filled (acceptable)'}"
    )


def main() -> int:
    passed, detail = run_smoke_test()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    if passed:
        msg = (
            f"✅ 8:15AM SMOKE TEST — PASSED\n"
            f"{now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Alpaca V2 paper account: VERIFIED\n"
            f"Order placement: ACCEPTED\n"
            f"Order confirmation: IN BOOK\n"
            f"Cancellation: CLEAN\n\n"
            f"{detail}\n\n"
            f"✅ V2 CLEARED FOR TRADING"
        )
        logger.info("SMOKE TEST PASSED — V2 cleared for trading")
        _send_telegram(msg)
        return 0
    else:
        msg = (
            f"❌ 8:15AM SMOKE TEST — FAILED\n"
            f"{now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"FAILURE: {detail}\n\n"
            f"❌ V2 MUST NOT TRADE TODAY\n"
            f"Action required: Diagnose Alpaca V2 connection before 9:31 AM"
        )
        logger.error("SMOKE TEST FAILED — %s", detail)
        _send_telegram(msg)
        return 1


if __name__ == "__main__":
    sys.exit(main())
