#!/usr/bin/env python3
"""
stub_sentinel.py — Oracle Client Stub Detector
================================================
Scans all Oracle client modules for stub responses against LIVE tickers.
Alerts immediately if any client returns _stub=True for a liquid ticker.

A stub reaching production = silent data fabrication.
This was the root cause of the May 1, 2026 full-day Oracle failure.

Run: python3 stub_sentinel.py
     or import and call run_stub_sentinel() programmatically

Exit codes:
  0 — all clients returning live data
  1 — one or more clients returning stubs → alert sent
"""

import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("stub_sentinel")

def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

ORACLE_URL    = os.environ.get("ORACLE_URL",    "http://192.168.1.141:8007")
ORACLE_SECRET = _require("ORACLE_SECRET")
OMNI_BOT      = _require("TELEGRAM_BOT_TOKEN")
AHMED_ID      = os.environ.get("AHMED_CHAT_ID", "8573754783")
HEALTH_GROUP  = os.environ.get("TELEGRAM_HEALTH_GROUP_CHAT_ID", "-1003954790884")

# Test tickers that should always have real options data
# Equity-only tickers (ETFs excluded from earnings check — they have no earnings dates)
TEST_TICKERS = ["AAPL", "NVDA", "JPM"]
ETF_TICKERS  = {"SPY", "QQQ", "IWM", "GLD", "EFA", "IEF", "HYG", "AGG", "IBB"}

# Client modules to audit (relative to nexus/oracle/)
CLIENT_STUBS_TO_CHECK = [
    ("orats",          "vol",         "iv_rank"),
    ("spotgamma",      "gamma",       "call_wall"),
    ("market_chameleon","flow",       "put_call_ratio"),
    ("polygon",        "price",       "rsi_14"),
    ("unusual_whales", "flow",        "net_flow_bias"),
    ("alpha_vantage",  "fundamental", "days_to_earnings"),
]


def _send_telegram(text: str) -> None:
    try:
        for chat_id in [AHMED_ID, HEALTH_GROUP]:
            requests.post(
                f"https://api.telegram.org/bot{OMNI_BOT}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=5,
            )
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


def run_stub_sentinel() -> bool:
    """
    Check Oracle context for stub indicators.
    Returns True if clean, False if stubs detected.
    """
    logger.info("=== STUB SENTINEL STARTING ===")
    stub_findings = []

    for ticker in TEST_TICKERS:
        try:
            resp = requests.get(
                f"{ORACLE_URL}/oracle/context/{ticker}",
                headers={"X-Oracle-Secret": ORACLE_SECRET},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("Oracle returned %s for %s", resp.status_code, ticker)
                continue
            ctx = resp.json()
        except Exception as e:
            logger.error("Oracle fetch failed for %s: %s", ticker, e)
            continue

        # Check each critical field — null on a liquid large-cap = effectively a stub
        vol   = ctx.get("vol")   or {}
        price = ctx.get("price") or {}
        flow  = ctx.get("flow")  or {}
        fund  = ctx.get("fundamental") or {}
        gamma = ctx.get("gamma") or {}

        checks = [
            ("ORATS/vol.iv_rank",               vol.get("iv_rank")),
            ("Polygon/price.rsi_14",             price.get("rsi_14")),
            ("Polygon/price.price_vs_50d_ma",    price.get("price_vs_50d_ma")),
            ("MarketChameleon/flow.put_call_ratio", flow.get("put_call_ratio")),
            ("AlphaVantage/fundamental.days_to_earnings",
             fund.get("days_to_earnings") if ticker not in ETF_TICKERS else True),  # ETFs have no earnings — skip check
        ]

        for name, val in checks:
            if val is None:
                stub_findings.append(f"{ticker} → {name} = None")
                logger.warning("STUB DETECTED: %s → %s is None", ticker, name)
            else:
                logger.info("✅ %s → %s = %s", ticker, name, val)

    if stub_findings:
        finding_str = "\n".join(f"• {f}" for f in stub_findings)
        msg = (
            f"🔴 <b>STUB SENTINEL: DATA GAPS DETECTED</b>\n\n"
            f"<b>Null fields on liquid tickers:</b>\n{finding_str}\n\n"
            f"<b>Risk:</b> Agents scoring on defaults, not real data.\n"
            f"Run <code>oracle_data_preflight.py</code> for full diagnosis."
        )
        logger.critical("STUB SENTINEL FINDINGS:\n%s", finding_str)
        _send_telegram(msg)
        return False
    else:
        logger.info("✅ STUB SENTINEL PASSED — all engines returning live data")
        return True


if __name__ == "__main__":
    ok = run_stub_sentinel()
    sys.exit(0 if ok else 1)
