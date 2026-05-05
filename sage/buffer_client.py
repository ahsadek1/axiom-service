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

    # Fire Railway submission in background (non-blocking)
    # Railway V1 validator requires strategy + confidence (mapped from score)
    # for non-neutral picks. Derive strategy from direction so Phase 2
    # validation passes the required-fields check. [GENESIS 2026-04-20]
    _railway_strategy = "bull_put" if direction == "bullish" else "bear_call"
    # V3 fix (2026-04-28): Railway v3 requires expiry + strike_entry for alpha arena.
    import datetime as _dt
    def _nearest_friday_expiry(min_dte=28, max_dte=38):
        today = _dt.date.today()
        for d in range(min_dte, max_dte + 1):
            candidate = today + _dt.timedelta(days=d)
            if candidate.weekday() == 4:
                return candidate.strftime("%Y-%m-%d")
        return (today + _dt.timedelta(days=min_dte)).strftime("%Y-%m-%d")
    def _get_atm_strike(sym, direction):
        try:
            import requests as _req
            _poly_key = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
            resp = _req.get(
                f"https://api.polygon.io/v2/aggs/ticker/{sym}/prev?adjusted=true&apiKey={_poly_key}",
                timeout=5,
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    price = float(results[0]["c"])
                    if direction == "bullish":
                        return round(price * 0.95 / 5) * 5
                    else:
                        return round(price * 1.05 / 5) * 5
        except Exception:
            pass
        return None
    _expiry = _nearest_friday_expiry()
    _strike = _get_atm_strike(ticker, direction)
    _submit_to_railway_async({
        "agent":      agent,
        "ticker":     ticker,
        "direction":  direction,
        "score":      score,
        "confidence": int(score),
        "strategy":   _railway_strategy,
        "reasoning":  reasoning,
        "arena":    "alpha",
        "round":    1,
        "r1_score": score,
        "expiry":       _expiry,
        "strike_entry": _strike,
        "strike_target": None,
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
