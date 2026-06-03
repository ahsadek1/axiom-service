"""
ORACLE — ORATS Client (live)
Provides: IV rank (rip), IV surface, HV, skew, contango, implied move.

API: https://api.orats.io/datav2/
Auth: token query param
Key: 4476e955-241a-4540-b114-ebbf1a3a3b87  ($399/mo — unlimited quota)

Primary fields used by scorer:
  rip         = IV rank percentile (0-100) → maps to iv_rank
  iv30d       = current 30d ATM IV (decimal)
  rVol30      = realized vol 30d
  impliedMove = expected earnings move (fraction)
  contango    = term structure ratio (iv30d/iv60d spread)
  dlt25Iv30d  = 25-delta put IV (skew proxy)
  dlt75Iv30d  = 75-delta call IV (skew proxy)

G13 — Retry Logic + Circuit Breaker (2026-05-05):
  - Retries ConnectionError and 5xx up to MAX_RETRIES times with jitter
  - 429 retries with exponential backoff + jitter
  - 403/401 return immediately without retry or circuit increment
  - Circuit breaker: CIRCUIT_BREAK_THRESHOLD consecutive failures → trips circuit
  - Circuit auto-resets after CIRCUIT_RESET_SEC (300s)
  - _orats_get() is the primary internal function; _get() is a legacy shim
"""

import logging
import os
import random
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

ORATS_TOKEN = os.getenv("ORATS_TOKEN") or os.getenv("ORATS_API_KEY")
ORATS_BASE  = "https://api.orats.io/datav2"
TIMEOUT     = 10  # seconds

# ── G13: Circuit Breaker ──────────────────────────────────────────────────────
CIRCUIT_BREAK_THRESHOLD: int = 5      # consecutive failures before tripping
CIRCUIT_RESET_SEC: float = 300.0     # 5 minutes — auto-reset after this window
MAX_RETRIES: int = 3                  # retry attempts per call
RETRY_BASE_DELAY: float = 1.0        # base delay between retries (seconds)

# Module-level circuit breaker state
_consecutive_failures: int = 0
_circuit_open_until: float = 0.0     # epoch time; 0 = circuit closed


def _check_circuit_breaker() -> None:
    """
    If _consecutive_failures has reached CIRCUIT_BREAK_THRESHOLD, open the circuit.
    Called after every failure. Idempotent if circuit already open.
    """
    global _circuit_open_until, _consecutive_failures
    if _consecutive_failures >= CIRCUIT_BREAK_THRESHOLD and _circuit_open_until == 0.0:
        _circuit_open_until = time.time() + CIRCUIT_RESET_SEC
        logger.error(
            "ORATS circuit breaker OPEN: %d consecutive failures. "
            "Blocking all requests for %.0fs.",
            _consecutive_failures, CIRCUIT_RESET_SEC,
        )


def _orats_get(endpoint: str, params: dict) -> Dict[str, Any]:
    """
    Core ORATS GET with retry logic and circuit breaker.

    Returns a dict — never None.
    On error: {"data": [], "error": <reason>}
    On success: the raw response JSON dict (with "data" key).

    Circuit breaker:
      - If circuit is open and not yet expired: returns {"data": [], "error": "circuit_open"}
      - Consecutive failures >= CIRCUIT_BREAK_THRESHOLD → opens circuit for CIRCUIT_RESET_SEC

    Retry logic:
      - ConnectionError / 5xx: retry up to MAX_RETRIES with jitter
      - 429: retry with exponential backoff + jitter
      - 403: immediate return {"data": [], "error": "403_not_on_plan"}, no circuit increment
      - 401: immediate return {"data": [], "error": "401_invalid_key"}, no circuit increment
    """
    global _consecutive_failures, _circuit_open_until

    # ── Circuit breaker check ─────────────────────────────────────────────────
    now = time.time()
    if _circuit_open_until > now:
        logger.warning("ORATS circuit OPEN — skipping request (%.0fs remaining)",
                       _circuit_open_until - now)
        return {"data": [], "error": "circuit_open"}
    elif _circuit_open_until != 0.0:
        # Circuit has expired — reset
        logger.info("ORATS circuit breaker RESET after %.0fs", CIRCUIT_RESET_SEC)
        _circuit_open_until = 0.0
        _consecutive_failures = 0

    # ── Retry loop ────────────────────────────────────────────────────────────
    last_error = "unknown"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(endpoint, params=params, timeout=TIMEOUT)

            # 403 — permanent auth/plan failure, no retry, no circuit increment
            if resp.status_code == 403:
                logger.warning("ORATS 403 Forbidden — not on plan or key missing: %s", endpoint)
                return {"data": [], "error": "403_not_on_plan"}

            # 401 — invalid key, no retry, no circuit increment
            if resp.status_code == 401:
                logger.warning("ORATS 401 Unauthorized — invalid key")
                return {"data": [], "error": "401_invalid_key"}

            # 429 — rate limited, retry with exponential backoff
            if resp.status_code == 429:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warning(
                    "ORATS 429 rate-limit (attempt %d/%d) — retrying in %.1fs",
                    attempt, MAX_RETRIES, delay,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(delay)
                    continue
                last_error = "429_rate_limit_exhausted"
                break

            # 5xx — server error, retry
            if resp.status_code >= 500:
                delay = RETRY_BASE_DELAY + random.uniform(0, 0.5)
                logger.warning(
                    "ORATS HTTP %d (attempt %d/%d) — retrying in %.1fs",
                    resp.status_code, attempt, MAX_RETRIES, delay,
                )
                last_error = f"http_{resp.status_code}"
                if attempt < MAX_RETRIES:
                    time.sleep(delay)
                    continue
                break

            # Success
            if 200 <= resp.status_code < 400:
                _consecutive_failures = 0
                return resp.json()

            # Other unexpected status
            last_error = f"http_{resp.status_code}"
            break

        except requests.exceptions.ConnectionError as e:
            delay = RETRY_BASE_DELAY + random.uniform(0, 0.5)
            logger.warning(
                "ORATS ConnectionError (attempt %d/%d): %s — retrying in %.1fs",
                attempt, MAX_RETRIES, e, delay,
            )
            last_error = f"connection_error"
            if attempt < MAX_RETRIES:
                time.sleep(delay)
                continue
            break

        except requests.exceptions.Timeout:
            logger.warning(
                "ORATS timeout after %ds (attempt %d/%d)",
                TIMEOUT, attempt, MAX_RETRIES,
            )
            last_error = "timeout"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY)
                continue
            break

        except Exception as e:
            logger.error("ORATS unexpected error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            last_error = str(e)[:80]
            break

    # All retries exhausted — update circuit breaker
    _consecutive_failures += 1
    logger.error(
        "ORATS request failed after %d attempts (consecutive_failures=%d): %s",
        MAX_RETRIES, _consecutive_failures, last_error,
    )
    _check_circuit_breaker()
    return {"data": [], "error": last_error}


def _get(endpoint: str, params: dict) -> Optional[Dict[str, Any]]:
    """
    Legacy shim around _orats_get for backward compatibility.
    Returns the first row of 'data' list, or None on error.
    """
    full_endpoint = f"{ORATS_BASE}/{endpoint}"
    params = dict(params)  # copy — do not mutate caller's dict
    params["token"] = ORATS_TOKEN
    result = _orats_get(full_endpoint, params)
    if result.get("error"):
        return None
    rows = result.get("data", [])
    if not rows:
        logger.warning("ORATS %s returned empty data for %s", endpoint, params.get("ticker"))
        return None
    return rows[0]


def get_vol_surface(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetch vol surface + IV rank from ORATS summaries endpoint.

    Returns dict with iv_rank, iv_percentile, hv30, hv60, iv_hv_spread,
    put_call_skew, contango, implied_move, _stub flag.
    """
    row = _get("summaries", {"ticker": ticker})
    if row is None:
        return {"hv30": None, "hv60": None, "_stub": True, "source": "orats_unavailable"}

    iv30d  = row.get("iv30d")      # current 30d ATM IV (decimal, e.g. 0.332)
    iv60d  = row.get("iv60d")
    rVol30 = row.get("rVol30")     # realized vol 30d
    rip    = row.get("rip")        # IV rank percentile (0-100)

    # Put/call skew: difference between 25-delta put IV and 75-delta call IV
    put_iv   = row.get("dlt25Iv30d")  # 25-delta = OTM puts (higher IV = more skew)
    call_iv  = row.get("dlt75Iv30d")  # 75-delta = OTM calls
    skew = None
    if put_iv is not None and call_iv is not None:
        skew = round(put_iv - call_iv, 4)

    # IV-HV spread: IV premium over realized vol
    iv_hv = None
    if iv30d is not None and rVol30 is not None:
        iv_hv = round(iv30d - rVol30, 4)

    # Contango: ORATS provides direct contango field (iv30d/iv60d ratio adjusted)
    contango = row.get("contango")

    # Implied earnings move
    implied_move = row.get("impliedMove")

    # HV60 fallback from hv20 if missing
    hv60 = row.get("rVol2y") or row.get("exErnIv60d")

    result = {
        "iv_rank":        round(rip, 2) if rip is not None else None,
        "iv_percentile":  round(rip, 2) if rip is not None else None,  # rip IS the percentile
        "hv30":           round(rVol30 * 100, 2) if rVol30 is not None else None,
        "hv60":           round(float(hv60) * 100, 2) if hv60 is not None else None,
        "iv_hv_spread":   iv_hv,
        "put_call_skew":  skew,
        "contango":       contango,
        "implied_move":   implied_move,
        "iv30d_raw":      iv30d,
        "_stub":          False,
        "_source":        "orats_live",
    }
    logger.debug("ORATS vol surface for %s: IVR=%.1f HV30=%.1f",
                 ticker,
                 rip if rip is not None else -1,
                 rVol30 * 100 if rVol30 is not None else -1)
    return result


def get_spread_pricing(ticker: str, strategy: str, expiry_days: int,
                       strike_entry: float, strike_target: float) -> Optional[Dict[str, Any]]:
    """
    Fetch theoretical spread pricing from ORATS (stub — use summaries impliedMove for now).
    """
    row = _get("summaries", {"ticker": ticker})
    if row is None:
        return {"theoretical_credit": None, "_stub": True, "source": "orats_unavailable"}

    return {
        "theoretical_credit": None,  # Would need strikes endpoint for exact pricing
        "implied_move":       row.get("impliedMove"),
        "iv30d":              row.get("iv30d"),
        "_stub":              False,
        "_source":            "orats_live_partial",
    }
