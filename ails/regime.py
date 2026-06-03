"""
regime.py — VIX-based regime classification.
Classifies current and historical market conditions into discrete regimes.
"""

import logging
import requests
from typing import Optional, Dict, Any
from config import FRED_API_KEY, REGIME_THRESHOLDS

log = logging.getLogger(__name__)

# NOTE: Do NOT use Polygon I:VIX — our plan does not cover index data (NOT_AUTHORIZED).
# FRED VIXCLS is free, authoritative, and returns correct values.
# Confirmed April 12, 2026 — same fix applied to backtest_populator.py.
_FRED_VIXCLS_URL = "https://api.stlouisfed.org/fred/series/observations"


def classify_vix(vix: float) -> str:
    """
    Classify VIX into regime bucket.

    Args:
        vix: Current VIX level

    Returns:
        Regime string: LOW_VOL | NORMAL | ELEVATED | STRESS | HIGH_STRESS | CRISIS
    """
    if vix <= REGIME_THRESHOLDS["LOW_VOL"]:
        return "LOW_VOL"
    if vix <= REGIME_THRESHOLDS["NORMAL"]:
        return "NORMAL"
    if vix <= REGIME_THRESHOLDS["ELEVATED"]:
        return "ELEVATED"
    if vix <= REGIME_THRESHOLDS["STRESS"]:
        return "STRESS"
    if vix <= REGIME_THRESHOLDS["HIGH_STRESS"]:
        return "HIGH_STRESS"
    return "CRISIS"


def get_current_vix() -> Optional[float]:
    """
    Fetch the most recent VIX close from FRED (series VIXCLS).

    FRED publishes the official CBOE VIX closing value each trading day.
    Returns the most recent available value (typically prior close).

    Returns:
        Most recent VIX closing value, or None on failure.
    """
    try:
        resp = requests.get(
            _FRED_VIXCLS_URL,
            params={
                "series_id":  "VIXCLS",
                "api_key":    FRED_API_KEY,
                "file_type":  "json",
                "sort_order": "desc",
                "limit":      5,   # last 5 observations — take the most recent non-missing
            },
            timeout=10,
        )
        if not resp.ok:
            log.warning("FRED VIXCLS fetch failed: HTTP %d", resp.status_code)
            return None

        observations = resp.json().get("observations", [])
        for obs in observations:
            value_str = obs.get("value", ".")
            if value_str != ".":   # FRED uses "." for missing values
                try:
                    vix = float(value_str)
                    log.debug("FRED VIXCLS: %.2f on %s", vix, obs.get("date"))
                    return vix
                except ValueError:
                    continue

        log.warning("FRED VIXCLS: no valid observations in last 5 results")
        return None

    except requests.RequestException as exc:
        log.warning("FRED VIXCLS request error: %s", exc)
        return None


def get_current_regime() -> Dict[str, Any]:
    """
    Get current market regime classification.

    Returns:
        Dict with regime, vix, confidence, source fields.
    """
    vix = get_current_vix()
    if vix is None:
        return {
            "regime": "UNKNOWN",
            "vix": None,
            "confidence": "low",
            "source": "unavailable",
        }

    regime = classify_vix(vix)
    return {
        "regime": regime,
        "vix": vix,
        "confidence": "high",
        "source": "fred_vixcls",
    }
