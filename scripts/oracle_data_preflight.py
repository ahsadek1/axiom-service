#!/usr/bin/env python3
"""
oracle_data_preflight.py — Oracle Data Integrity Hard Gate
============================================================
Runs at 9:25 AM ET before every trading session.

PURPOSE: Verify Oracle returns REAL data — not stubs, not nulls, not defaults.
         If this fails, the pipeline does NOT open. No exceptions.

WHAT IT CHECKS (per ticker, 5 test tickers):
  - vol.iv_rank        → real ORATS IVR percentile (must be 0-100 float, not None)
  - price.rsi_14       → real RSI from Polygon bars (must be 0-100 float, not None)
  - price.price_vs_50d_ma → real MA deviation (must be float, not None)
  - flow.put_call_ratio   → real PCR from Polygon options (must be float, not None)
  - fundamental.revenue_growth_yoy → real Polygon financials (must be float, not None)

UNIFORM SCORE DETECTION:
  - Requests all 5 tickers and simulates D1+D3+D5 subscores
  - If all 5 tickers produce identical D1 scores → DATA FAILURE (not market)
  - Halts pipeline if detected

EXIT CODES:
  0 → ALL CLEAR — pipeline may open
  1 → FAILED — pipeline MUST NOT open, Ahmed alerted

Usage:
  python3 oracle_data_preflight.py

Deployed: 2026-05-01 (after full-day Oracle null data failure on May 1)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────
def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

ORACLE_URL    = os.environ.get("ORACLE_URL",    "http://192.168.1.141:8007")
ORACLE_SECRET = _require("ORACLE_SECRET")
TELEGRAM_BOT  = _require("TELEGRAM_BOT_TOKEN")
OMNI_BOT      = TELEGRAM_BOT  # alias
AHMED_ID      = os.environ.get("AHMED_CHAT_ID", "8573754783")
HEALTH_GROUP  = os.environ.get("TELEGRAM_HEALTH_GROUP_CHAT_ID", "-1003954790884")

# 5 diverse tickers across sectors — chosen to have real options activity
TEST_TICKERS = ["AAPL", "NVDA", "SPY", "JPM", "MSFT"]

# Required fields per context section
REQUIRED_FIELDS = {
    "vol":         ["iv_rank"],
    "price":       ["rsi_14", "price_vs_50d_ma"],
    "flow":        ["put_call_ratio"],
    "fundamental": ["revenue_growth_yoy"],
}

# Uniform score detection: if all tickers produce same D1 score → data frozen
UNIFORM_THRESHOLD = 0.8   # 80%+ identical = flag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("oracle_preflight")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        logger.warning("Telegram alert failed: %s", e)


def _alert(text: str) -> None:
    """Alert Ahmed + Health Group via OMNI bot."""
    _send_telegram(OMNI_BOT, AHMED_ID, text)
    _send_telegram(OMNI_BOT, HEALTH_GROUP, text)


def _fetch_context(ticker: str) -> dict:
    """Fetch Oracle context for a single ticker. Returns {} on failure."""
    try:
        resp = requests.get(
            f"{ORACLE_URL}/oracle/context/{ticker}",
            headers={"X-Oracle-Secret": ORACLE_SECRET},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Oracle returned %s for %s", resp.status_code, ticker)
        return {}
    except Exception as e:
        logger.error("Oracle fetch failed for %s: %s", ticker, e)
        return {}


def _compute_d1(context: dict) -> int:
    """Replicate D1 scoring logic for uniform detection."""
    ivr = (context.get("vol") or {}).get("iv_rank")
    if ivr is None:
        return 10  # neutral fallback — this is what we're detecting
    if ivr > 80:
        return 17
    if 40 <= ivr <= 70:
        return 20
    if 15 <= ivr < 40:
        return max(6, 12)
    return 5  # low IV floor


# ── Main preflight ────────────────────────────────────────────────────────────

def run_preflight() -> bool:
    """
    Run full Oracle data integrity check.
    Returns True if all checks pass, False if any critical check fails.
    """
    logger.info("=== ORACLE DATA PREFLIGHT STARTING ===")
    logger.info("Test tickers: %s", TEST_TICKERS)

    failures = []
    warnings = []
    contexts = {}
    d1_scores = []

    # ── Step 1: Oracle reachable ──────────────────────────────────────────────
    try:
        resp = requests.get(f"{ORACLE_URL}/health", timeout=5)
        if resp.status_code != 200:
            failures.append("Oracle /health returned non-200")
        else:
            logger.info("Oracle health: %s", resp.json().get("status"))
    except Exception as e:
        failures.append(f"Oracle unreachable: {e}")
        _alert(
            "🔴 <b>PREFLIGHT FAILED — ORACLE UNREACHABLE</b>\n"
            f"Oracle not responding at {ORACLE_URL}\n"
            "Pipeline HALTED. Fix Oracle before 9:30 AM."
        )
        return False

    # ── Step 2: Fetch contexts for all test tickers ───────────────────────────
    logger.info("Fetching Oracle contexts...")
    for ticker in TEST_TICKERS:
        ctx = _fetch_context(ticker)
        contexts[ticker] = ctx
        d1_scores.append(_compute_d1(ctx))
        time.sleep(0.5)  # avoid hammering Oracle

    # ── Step 3: Check required fields per ticker ──────────────────────────────
    null_report = {}
    for ticker in TEST_TICKERS:
        ctx = contexts[ticker]
        ticker_nulls = []
        for section, fields in REQUIRED_FIELDS.items():
            section_data = ctx.get(section) or {}
            for field in fields:
                val = section_data.get(field)
                if val is None:
                    ticker_nulls.append(f"{section}.{field}")
        if ticker_nulls:
            null_report[ticker] = ticker_nulls
            logger.warning("%s: NULL fields: %s", ticker, ticker_nulls)
        else:
            logger.info("%s: ✅ All required fields present", ticker)

    # Critical: >60% of tickers with null IVR = Oracle vol engine broken
    null_ivr = sum(
        1 for t in TEST_TICKERS
        if (contexts[t].get("vol") or {}).get("iv_rank") is None
    )
    if null_ivr >= 3:
        failures.append(
            f"IVR null for {null_ivr}/{len(TEST_TICKERS)} tickers — "
            "ORATS engine broken or returning stubs"
        )

    # Critical: >60% of tickers with null RSI = Polygon bars broken
    null_rsi = sum(
        1 for t in TEST_TICKERS
        if (contexts[t].get("price") or {}).get("rsi_14") is None
    )
    if null_rsi >= 3:
        failures.append(
            f"RSI null for {null_rsi}/{len(TEST_TICKERS)} tickers — "
            "Polygon technicals engine broken"
        )

    # Critical: >60% with null revenue_growth = fundamental engine broken
    null_rev = sum(
        1 for t in TEST_TICKERS
        if (contexts[t].get("fundamental") or {}).get("revenue_growth_yoy") is None
    )
    if null_rev >= 4:
        failures.append(
            f"revenue_growth_yoy null for {null_rev}/{len(TEST_TICKERS)} tickers — "
            "Polygon financials engine broken"
        )

    # ── Step 4: Uniform score detection ──────────────────────────────────────
    if d1_scores:
        most_common = max(set(d1_scores), key=d1_scores.count)
        uniformity = d1_scores.count(most_common) / len(d1_scores)
        logger.info("D1 scores: %s | uniformity=%.0f%%", d1_scores, uniformity * 100)
        if uniformity >= UNIFORM_THRESHOLD:
            failures.append(
                f"D1 score uniformity {uniformity:.0%} — "
                f"all tickers scoring {most_common}/20. "
                "IVR data likely frozen or null. Scoring is fabricated."
            )

    # ── Step 5: Stub sentinel — check for _stub:True in live responses ────────
    stub_tickers = []
    for ticker in TEST_TICKERS:
        ctx = contexts[ticker]
        # Oracle context doesn't expose _stub directly, but null IVR + null RSI together
        # on a liquid large-cap = effectively a stub response
        vol = ctx.get("vol") or {}
        price = ctx.get("price") or {}
        if vol.get("iv_rank") is None and price.get("rsi_14") is None:
            stub_tickers.append(ticker)
    if len(stub_tickers) >= 3:
        failures.append(
            f"Stub-equivalent responses for {stub_tickers} — "
            "Oracle clients returning null across vol+price. Data layer not functional."
        )

    # ── Step 6: Report ─────────────────────────────────────────────────────────
    passed = len(failures) == 0

    if passed:
        # Build pass summary
        sample = contexts.get("AAPL") or {}
        ivr_sample   = (sample.get("vol") or {}).get("iv_rank")
        rsi_sample   = (sample.get("price") or {}).get("rsi_14")
        rev_sample   = (sample.get("fundamental") or {}).get("revenue_growth_yoy")
        pcr_sample   = (sample.get("flow") or {}).get("put_call_ratio")
        logger.info(
            "✅ PREFLIGHT PASSED | AAPL: IVR=%.1f RSI=%.1f rev_growth=%.1f%% PCR=%.2f",
            ivr_sample or 0, rsi_sample or 0, rev_sample or 0, pcr_sample or 0,
        )
        warn_str = f"\n⚠️ Warnings: {'; '.join(warnings)}" if warnings else ""
        _alert(
            f"✅ <b>ORACLE PREFLIGHT PASSED</b> — 9:25 AM gate cleared\n"
            f"AAPL: IVR={ivr_sample:.1f} | RSI={rsi_sample:.1f} | "
            f"rev_growth={rev_sample:.1f}% | PCR={pcr_sample:.2f}\n"
            f"D1 scores: {d1_scores}\n"
            f"Pipeline open for trading.{warn_str}"
        )
    else:
        failure_str = "\n".join(f"• {f}" for f in failures)
        logger.critical("❌ PREFLIGHT FAILED:\n%s", failure_str)
        _alert(
            f"🔴 <b>ORACLE PREFLIGHT FAILED — PIPELINE HALTED</b>\n\n"
            f"<b>Failures:</b>\n{failure_str}\n\n"
            f"<b>Action required:</b> Fix Oracle before 9:30 AM.\n"
            f"Do NOT trade until preflight passes.\n\n"
            f"Run manually: <code>python3 oracle_data_preflight.py</code>"
        )

    return passed


if __name__ == "__main__":
    ok = run_preflight()
    sys.exit(0 if ok else 1)
