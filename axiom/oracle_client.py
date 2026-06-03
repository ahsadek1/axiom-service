"""
oracle_client.py — Axiom's typed HTTP client for ORACLE service.

All calls are non-blocking on failure — Axiom never crashes due to ORACLE
being down. Every function catches all exceptions, logs at WARNING level,
and returns a safe default (None, False, or empty dict).

No bare `except` clauses. Explicit timeouts on every call.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

import config as _cfg

logger = logging.getLogger("axiom.oracle_client")

# Loaded once at module import — settings already validated by config.load_settings()
_settings: Optional[_cfg.Settings] = None


def _get_settings() -> _cfg.Settings:
    """Return cached settings instance."""
    global _settings
    if _settings is None:
        _settings = _cfg.load_settings()
    return _settings


def _base_url() -> str:
    """Return ORACLE base URL from settings."""
    return _get_settings().oracle_url.rstrip("/")


def _headers() -> Dict[str, str]:
    """Return auth headers for ORACLE requests."""
    return {
        "X-Oracle-Secret": _get_settings().oracle_secret,
        "Content-Type": "application/json",
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def health_check(timeout: float = 3.0) -> bool:
    """
    Check if ORACLE service is reachable.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        True if ORACLE returns 200 on /oracle/health, False otherwise.
    """
    try:
        resp = requests.get(
            f"{_base_url()}/oracle/health",
            headers=_headers(),
            timeout=timeout,
        )
        return resp.status_code == 200
    except requests.exceptions.Timeout:
        logger.warning("ORACLE health check timed out after %.1fs", timeout)
        return False
    except requests.exceptions.ConnectionError:
        logger.warning("ORACLE health check failed — connection refused (is ORACLE running?)")
        return False
    except Exception as e:
        logger.warning("ORACLE health check error: %s", e)
        return False


def get_macro_data(timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """
    Fetch ORACLE macro packet for regime v2 calculation.

    Returns the MacroData dict from ORACLE's Macro Engine (Engine 5):
    VIX, HY spread, yield curve shape, put/call ratio, regime composite score.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        Dict with macro fields, or None if ORACLE unavailable.
    """
    try:
        resp = requests.get(
            f"{_base_url()}/oracle/macro",
            headers=_headers(),
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            # ORACLE /oracle/macro returns {"macro": {...}, "data_freshness": ...}
            # Fall back to bare dict for legacy compatibility
            return data.get("macro") or data.get("data") or data
        logger.warning("ORACLE macro endpoint returned %d", resp.status_code)
        return None
    except requests.exceptions.Timeout:
        logger.warning("ORACLE macro query timed out after %.1fs", timeout)
        return None
    except requests.exceptions.ConnectionError:
        logger.warning("ORACLE macro query failed — connection error")
        return None
    except Exception as e:
        logger.warning("ORACLE macro query error: %s", e)
        return None


def prefetch(tickers: List[str], tier: str, timeout: float = 3.0) -> bool:
    """
    Trigger ORACLE to pre-warm context cards for a list of tickers.

    Fire-and-forget — caller should NOT wait on this response body.
    Returns True if ORACLE accepted the request (2xx), False otherwise.

    Args:
        tickers: List of ticker symbols to pre-warm.
        tier:    "preliminary" (3 engines) or "full" (all 7 engines).
        timeout: Request timeout in seconds.

    Returns:
        True if accepted, False if rejected or unreachable.
    """
    try:
        resp = requests.post(
            f"{_base_url()}/oracle/prefetch",
            json={"tickers": tickers, "tier": tier},
            headers=_headers(),
            timeout=timeout,
        )
        if resp.status_code in (200, 201, 202):
            logger.info(
                "ORACLE prefetch accepted — %d tickers, tier=%s", len(tickers), tier
            )
            return True
        logger.warning(
            "ORACLE prefetch returned %d for tier=%s (%d tickers)",
            resp.status_code, tier, len(tickers),
        )
        return False
    except requests.exceptions.Timeout:
        logger.warning("ORACLE prefetch timed out (tier=%s, %d tickers)", tier, len(tickers))
        return False
    except requests.exceptions.ConnectionError:
        logger.warning("ORACLE prefetch failed — connection error")
        return False
    except Exception as e:
        logger.warning("ORACLE prefetch error: %s", e)
        return False


def get_coherence_scores(
    tickers: List[str],
    timeout: float = 10.0,
) -> Dict[str, Dict[str, Any]]:
    """
    Query ORACLE for coherence scores on a list of pool tickers.

    All tickers queried concurrently. Missing or failed tickers are silently
    omitted from the result (callers check `coherence_available` flag).

    Args:
        tickers: Pool tickers to query coherence for.
        timeout: Per-request timeout in seconds.

    Returns:
        Dict mapping ticker -> {score, level, flag_count}.
        Empty dict if ORACLE is unavailable or no data returned.
    """
    if not tickers:
        return {}

    results: Dict[str, Dict[str, Any]] = {}

    def _fetch_one(ticker: str) -> Optional[tuple]:
        """Fetch coherence for a single ticker."""
        try:
            resp = requests.get(
                f"{_base_url()}/oracle/context/{ticker}",
                headers=_headers(),
                timeout=timeout,
            )
            if resp.status_code != 200:
                return None
            packet = resp.json()
            coherence = packet.get("coherence")
            if coherence is None:
                return None
            return ticker, {
                "score": coherence.get("coherence_score", 0),
                "level": coherence.get("coherence_level", "UNKNOWN"),
                "flag_count": len(coherence.get("flags", [])),
            }
        except requests.exceptions.Timeout:
            logger.debug("ORACLE coherence timeout for %s", ticker)
            return None
        except Exception as e:
            logger.debug("ORACLE coherence error for %s: %s", ticker, e)
            return None

    with ThreadPoolExecutor(max_workers=min(10, len(tickers))) as executor:
        future_to_ticker = {
            executor.submit(_fetch_one, ticker): ticker
            for ticker in tickers
        }
        for future in as_completed(future_to_ticker):
            try:
                result = future.result(timeout=timeout + 1)
                if result is not None:
                    ticker, score_data = result
                    results[ticker] = score_data
            except Exception as e:
                logger.debug("Coherence future error: %s", e)

    logger.info(
        "ORACLE coherence scores fetched — %d/%d tickers returned data",
        len(results), len(tickers),
    )
    return results


def get_cycle_intelligence(timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """
    Fetch Gemini cross-ticker pattern detection result for the current cycle.

    Returns the full CycleIntelligence dict from ORACLE, or None if unavailable.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        Dict with patterns_detected and echo_chamber_risk_tickers, or None.
    """
    try:
        resp = requests.get(
            f"{_base_url()}/oracle/intelligence/cycle",
            headers=_headers(),
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("ORACLE cycle intelligence returned %d", resp.status_code)
        return None
    except requests.exceptions.Timeout:
        logger.warning("ORACLE cycle intelligence timed out after %.1fs", timeout)
        return None
    except requests.exceptions.ConnectionError:
        logger.warning("ORACLE cycle intelligence failed — connection error")
        return None
    except Exception as e:
        logger.warning("ORACLE cycle intelligence error: %s", e)
        return None
