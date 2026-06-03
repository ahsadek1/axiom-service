"""
ORACLE — AILS Client (Engine 7)
Queries the Adaptive Intelligent Learning System for historical win rates.
Non-blocking: if AILS is down, returns None without crashing other engines.

Endpoint: GET /context/{ticker}?strategy=&regime=&direction=
Response includes win_rate, confidence, sample_count, source, cache_ttl_s
"""

import logging
import time
from typing import Any, Dict, Optional

import requests

import config

logger = logging.getLogger(__name__)

TIMEOUT = (3, 10)  # Shorter timeout — AILS is internal, non-blocking


def get_historical(
    ticker: str,
    regime: str,
    strategy: str = "bull_put_spread",
    direction: str = "bullish",
) -> Optional[Dict[str, Any]]:
    """
    Fetch historical win-rate context from AILS for a ticker/regime/strategy.

    Args:
        ticker:    Stock ticker symbol.
        regime:    Current market regime (e.g. 'ELEVATED').
        strategy:  Strategy type (e.g. 'bull_put_spread').
        direction: Trade direction ('bullish', 'bearish', 'neutral').

    Returns:
        Dict with win_rate, confidence, sample_count, source, cache_ttl_s.
        Returns fallback dict silently if AILS is unavailable — never raises.
    """
    url = f"{config.AILS_URL}/context/{ticker.upper()}"
    params = {"strategy": strategy, "regime": regime, "direction": direction}
    headers = {"X-Ails-Secret": getattr(config, "AILS_SECRET", "")}

    start = time.monotonic()
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            data["latency_ms"] = latency_ms
            logger.debug(
                "AILS context: %s %s/%s → wr=%.2f (%s) in %dms",
                ticker, strategy, regime,
                data.get("win_rate", 0),
                data.get("confidence", "?"),
                latency_ms,
            )
            return data
        elif resp.status_code == 403:
            logger.warning("AILS auth failed — check AILS_SECRET in ORACLE .env")
            return _no_data_response()
        else:
            logger.warning("AILS returned %d for %s", resp.status_code, ticker)
            return _no_data_response()

    except requests.Timeout:
        logger.debug("AILS timeout for %s — continuing without historical data", ticker)
        return None
    except requests.ConnectionError:
        logger.debug("AILS unavailable for %s — continuing without historical data", ticker)
        return None
    except (ValueError, KeyError) as exc:
        logger.warning("AILS parse error for %s: %s", ticker, exc)
        return None


def _no_data_response() -> Dict[str, Any]:
    """Return a neutral fallback when AILS has no data."""
    return {
        "win_rate": 0.5,
        "confidence": "NONE",
        "sample_count": 0,
        "source": "unavailable",
        "cache_ttl_s": 900,
    }
