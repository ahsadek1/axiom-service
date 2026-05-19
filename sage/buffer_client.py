"""
buffer_client.py — Alpha and Prime Buffer Submission Client

Submits scored picks to Alpha Buffer (options) and Prime Buffer (equity).
ALSO submits directly to Railway /submit so Atlas participates in Railway
concordance (fix: Atlas was only submitting to local buffers, invisible to
the Railway concordance system — causing Layer11 AGENT_SILENCE).

Retry logic: one retry after 30s on first failure (local buffers only).
Railway submit is non-blocking fire-and-forget — never blocks or retries.
Never raises — returns False on persistent failure.
"""

import logging
import os
import threading
import time
import requests
from typing import Optional

logger = logging.getLogger("sage.buffer")

# ── Axiom universe preflight cache ────────────────────────────────────────────
# GENESIS-UNIVERSE-FILTER-001 2026-05-05: Pre-submission universe check.
# Agents were submitting tickers (e.g. GOOG, GOOGL) not in the Axiom universe,
# wasting buffer HTTP cycles and generating C-01 rejection noise.
# Fail-open: if Axiom unreachable, proceed (buffer's C-01 is the authoritative backstop).
_universe_cache: list = []
_universe_cache_ts: float = 0.0
_UNIVERSE_CACHE_TTL: float = 300.0  # 5 minutes
_universe_lock = threading.Lock()


def _get_axiom_universe() -> list:
    """Fetch and cache Axiom universe tickers. Returns [] on any error (fail-open)."""
    global _universe_cache, _universe_cache_ts
    with _universe_lock:
        if time.time() - _universe_cache_ts < _UNIVERSE_CACHE_TTL and _universe_cache:
            return _universe_cache
    try:
        axiom_url = os.getenv("AXIOM_URL", "http://localhost:8001")
        axiom_secret = os.getenv("AXIOM_SECRET", "")
        r = requests.get(
            f"{axiom_url}/universe",
            headers={"X-Axiom-Secret": axiom_secret},
            timeout=3,
        )
        if r.status_code == 200:
            tickers = r.json().get("tickers", [])
            if tickers:
                with _universe_lock:
                    _universe_cache = tickers
                    _universe_cache_ts = time.time()
                return tickers
    except Exception:
        pass
    return []  # fail-open: empty list = skip check


def _ticker_in_universe(ticker: str) -> bool:
    """True if ticker is in Axiom universe, or universe is unavailable (fail-open)."""
    universe = _get_axiom_universe()
    if not universe:
        return True  # fail-open: can't confirm exclusion
    return ticker in universe

SUBMIT_TIMEOUT_S = 10
RETRY_DELAY_S = 30

# Railway direct submission (bypass local buffer for concordance participation)
RAILWAY_WEBHOOK_URL = os.getenv("RAILWAY_WEBHOOK_URL")  # None if not set — no hardcoded fallback
RAILWAY_SECRET = os.getenv(
    "NEXUS_SECRET",
    "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2",
)

# Score thresholds (mirrors buffer validator config)
ALPHA_MIN_SCORE = 58.0
PRIME_MIN_SCORE = 63.0


def _submit_to_railway_async(payload: dict) -> None:
    """
    Fire-and-forget Railway /submit in a background thread.
    Atlas participates in Railway concordance alongside Cipher/Sage.
    Failures are logged but never block local buffer submission.
    """
    if not RAILWAY_WEBHOOK_URL:
        logger.debug("RAILWAY_WEBHOOK_URL not set — skipping Railway submission")
        return

    def _fire():
        try:
            resp = requests.post(
                RAILWAY_WEBHOOK_URL,
                json=payload,
                headers={
                    "X-Nexus-Secret": RAILWAY_SECRET,
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                logger.info(
                    "Railway submit OK: %s/%s → verdict=%s concordance_agents=%s",
                    payload["ticker"], payload["direction"],
                    result.get("verdict", "?"), result.get("concordance_agents", "?"),
                )
            elif resp.status_code == 409:
                logger.info(
                    "Railway submit 409 (dedup): %s/%s already submitted today",
                    payload["ticker"], payload["direction"],
                )
            else:
                logger.warning(
                    "Railway submit non-2xx: %s/%s HTTP %d — %s",
                    payload["ticker"], payload["direction"],
                    resp.status_code, resp.text[:200],
                )
        except Exception as e:
            logger.warning(
                "Railway submit error for %s/%s: %s (non-blocking)",
                payload["ticker"], payload["direction"], e,
            )

    t = threading.Thread(target=_fire, daemon=True, name=f"railway-{payload['ticker']}")
    t.start()


def submit_to_alpha(
    ticker: str,
    agent: str,
    direction: str,
    score: float,
    reasoning: str,
    alpha_buffer_url: str,
    alpha_headers: dict,
    window_id: str = "",  # Pool window_id from Axiom push [GENESIS concordance-window-fix]
) -> bool:
    """
    Submit a pick to Alpha Buffer (options concordance).
    Also fires a non-blocking Railway /submit so Atlas is visible to the
    Railway concordance system and clears Layer11 AGENT_SILENCE.

    Args:
        ticker: Stock ticker symbol
        agent: Agent name ('Atlas')
        direction: 'bullish' or 'bearish'
        score: Conviction score 0–100
        reasoning: Human-readable analysis summary
        alpha_buffer_url: Alpha Buffer base URL
        alpha_headers: Auth headers with X-Nexus-Secret

    Returns:
        True if submitted to local buffer successfully, False otherwise.
    """
    if not _ticker_in_universe(ticker):
        logger.debug("Alpha skip %s/%s: ticker not in Axiom universe (pre-filter)", ticker, direction)
        return False
    if score < ALPHA_MIN_SCORE:
        logger.debug("Alpha skip %s/%s: score %.1f < %.1f", ticker, direction, score, ALPHA_MIN_SCORE)
        return False

    payload: dict = {
        "agent": agent,
        "ticker": ticker,
        "direction": direction,
        "score": score,
        "reasoning": reasoning,
    }
    if window_id:
        payload["window_id"] = window_id  # GENESIS concordance-window-fix

    # Fire Railway submission in background (non-blocking).
    # GENESIS 2026-05-08: Railway /submit hard-rejects (422) if expiry, strike_entry,
    # or strike_target are None. Fixed with multi-fallback price resolution + proper
    # strike spread calculation. strike_target was previously hardcoded to None — fixed.
    _railway_strategy = "bull_put" if direction == "bullish" else "bear_call"

    import datetime as _dt

    def _nearest_friday_expiry(min_dte: int = 28, max_dte: int = 45) -> str:
        """Return nearest Friday expiry between min_dte and max_dte days out."""
        today = _dt.date.today()
        for d in range(min_dte, max_dte + 1):
            candidate = today + _dt.timedelta(days=d)
            if candidate.weekday() == 4:  # Friday
                return candidate.strftime("%Y-%m-%d")
        return (today + _dt.timedelta(days=min_dte)).strftime("%Y-%m-%d")

    def _resolve_stock_price(sym: str) -> float:
        """
        Resolve current stock price via multiple fallbacks.
        Polygon snapshot -> Polygon daily bars -> Axiom -> $100 last resort.
        Never raises. Always returns a positive float.
        """
        _poly_key = os.getenv("POLYGON_API_KEY", "")

        # 1. Polygon snapshot (real-time)
        if _poly_key:
            try:
                r = requests.get(
                    f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{sym}",
                    params={"apiKey": _poly_key}, timeout=5,
                )
                if r.status_code == 200:
                    snap = r.json()
                    price = (
                        snap.get("ticker", {}).get("day", {}).get("c") or
                        snap.get("ticker", {}).get("lastTrade", {}).get("p")
                    )
                    if price and float(price) > 0:
                        return float(price)
            except Exception:
                pass

        # 2. Polygon daily bars fallback
        if _poly_key:
            try:
                today = _dt.date.today()
                from_d = (today - _dt.timedelta(days=5)).isoformat()
                r2 = requests.get(
                    f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/{from_d}/{today.isoformat()}",
                    params={"adjusted": "true", "sort": "desc", "limit": 3, "apiKey": _poly_key},
                    timeout=5,
                )
                if r2.status_code == 200:
                    bars = r2.json().get("results", [])
                    if bars and float(bars[0]["c"]) > 0:
                        return float(bars[0]["c"])
            except Exception:
                pass

        # 3. Axiom price cache (local service — always available on Mac mini)
        try:
            axiom_url = os.getenv("AXIOM_URL", "http://localhost:8001")
            axiom_secret = os.getenv("AXIOM_SECRET", "")
            r3 = requests.get(
                f"{axiom_url}/price/{sym}",
                headers={"X-Axiom-Secret": axiom_secret},
                timeout=3,
            )
            if r3.status_code == 200:
                price = r3.json().get("price") or r3.json().get("last_price")
                if price and float(price) > 0:
                    return float(price)
        except Exception:
            pass

        # 4. Cannot resolve price — skip this ticker instead of using bogus fallback
        #    610 422 errors on May 18 were caused by $100 fallback sending invalid strikes.
        logger.warning(
            "[Railway] Cannot resolve price for %s — returning None (submission will be skipped)", sym
        )
        return None

    def _resolve_strikes(sym: str, dir_: str, price: float) -> tuple:
        """
        Compute (strike_entry, strike_target) from current price. Never returns None.
        bull_put: sell put ~5% OTM below price, buy put 5pts lower
        bear_call: sell call ~5% OTM above price, buy call 5pts higher
        """
        _ETF_LIST = {"SPY", "QQQ", "IWM", "XLK", "XLF", "XLV", "XLE", "TLT", "GLD", "SOXX"}
        _round_to = 5 if sym in _ETF_LIST else 1
        _spread_width = _round_to * 5  # 25pts ETF, 5pts equity

        if dir_ == "bullish":
            entry  = round(price * 0.95 / _round_to) * _round_to
            target = entry - _spread_width
        else:
            entry  = round(price * 1.05 / _round_to) * _round_to
            target = entry + _spread_width

        return float(entry), float(target)

    _expiry = _nearest_friday_expiry()
    _price  = _resolve_stock_price(ticker)
    if _price is None or _price <= 0:
        logger.warning("Skipping %s/%s — cannot resolve stock price", ticker, direction)
        return False  # skip submission; 422 prevention
    _strike_entry, _strike_target = _resolve_strikes(ticker, direction, _price)

    # Map internal direction ('bullish'/'bearish') to Railway enum ('LONG'/'SHORT')
    _railway_direction = "LONG" if direction == "bullish" else "SHORT"

    _submit_to_railway_async({
        "agent":         agent,
        "ticker":        ticker,
        "direction":     _railway_direction,
        "score":         score,
        "confidence":    int(score),
        "strategy":      _railway_strategy,
        "reasoning":     reasoning,
        "arena":         "alpha",
        "round":         1,
        "r1_score":      score,
        "expiry":        _expiry,
        "strike_entry":  _strike_entry,
        "strike_target": _strike_target,
    })

    return _submit(payload, f"{alpha_buffer_url}/submit", alpha_headers, "Alpha")


def submit_to_prime(
    ticker: str,
    agent: str,
    direction: str,
    score: float,
    reasoning: str,
    prime_buffer_url: str,
    prime_headers: dict,
    window_id: str = "",  # Pool window_id from Axiom push [GENESIS concordance-window-fix]
) -> bool:
    """
    Submit a pick to Prime Buffer (swing equity concordance).

    Args:
        ticker: Stock ticker symbol
        agent: Agent name ('Sage')
        direction: 'bullish' or 'bearish'
        score: Conviction score 0–100
        reasoning: Human-readable analysis summary
        prime_buffer_url: Prime Buffer base URL
        prime_headers: Auth headers with X-Nexus-Prime-Secret
        window_id: Pool window_id from Axiom push

    Returns:
        True if submitted successfully, False otherwise.
    """
    if score < PRIME_MIN_SCORE:
        logger.debug("Prime skip %s/%s: score %.1f < %.1f", ticker, direction, score, PRIME_MIN_SCORE)
        return False

    payload: dict = {
        "agent": agent,
        "ticker": ticker,
        "direction": direction,
        "score": score,
        "reasoning": reasoning,
    }
    if window_id:
        payload["window_id"] = window_id  # GENESIS concordance-window-fix
    return _submit(payload, f"{prime_buffer_url}/submit", prime_headers, "Prime")


def _submit(
    payload: dict,
    url: str,
    headers: dict,
    buffer_name: str,
    _retry: bool = True,
) -> bool:
    """
    POST payload to buffer URL with one retry on failure.

    Args:
        payload: Submission dict
        url: Full buffer submission URL
        headers: Auth headers
        buffer_name: 'Alpha' or 'Prime' for logging
        _retry: Whether to retry once on failure (internal flag)

    Returns:
        True on success, False on persistent failure.
    """
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=SUBMIT_TIMEOUT_S)
        if resp.status_code in (200, 201):
            logger.info(
                "%s Buffer accepted %s/%s score=%.1f",
                buffer_name, payload["ticker"], payload["direction"], payload["score"],
            )
            return True
        if resp.status_code == 409:
            # 409 = buffer dedup gate: this agent already submitted this ticker+direction
            # today (in a prior window). This is expected behavior — not a failure.
            # Do NOT retry and do NOT alert.
            logger.info(
                "%s Buffer: %s/%s already submitted today by %s — skipping (not an error)",
                buffer_name, payload["ticker"], payload["direction"], payload["agent"],
            )
            return True
        logger.error(
            "%s Buffer rejected %s/%s: HTTP %d — %s",
            buffer_name, payload["ticker"], payload["direction"],
            resp.status_code, resp.text[:200],
        )
    except Exception as e:
        logger.error(
            "%s Buffer submit error for %s/%s: %s",
            buffer_name, payload["ticker"], payload["direction"], e,
        )

    # One retry after delay
    if _retry:
        logger.warning("Retrying %s Buffer submission for %s in %ds...", buffer_name, payload["ticker"], RETRY_DELAY_S)
        time.sleep(RETRY_DELAY_S)
        return _submit(payload, url, headers, buffer_name, _retry=False)

    logger.critical(
        "%s Buffer submission permanently failed for %s/%s",
        buffer_name, payload["ticker"], payload["direction"],
    )
    return False
