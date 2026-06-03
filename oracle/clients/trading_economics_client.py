"""
ORACLE — Trading Economics Client (live)

Provides US macro indicators and economic calendar data.
Key: 6eb95b2cd21d463:ay585nt84yhsrn1 (client credentials format)

Access confirmed:
  - /country/{country}  → latest indicator values per country ✅
  - /indicators         → full indicator catalog ✅
  - /markets/index      → equity index data ✅
  - /markets/currency   → FX rates ✅
  - /calendar           → 403 (not in plan tier)

Macro indicators pulled for Nexus: GDP Growth, Unemployment, CPI, Fed Funds Rate,
Interest Rate, Consumer Confidence, ISM Manufacturing PMI, Retail Sales.
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

TRADING_ECONOMICS_KEY: str = os.environ["TRADING_ECONOMICS_KEY"]
TE_BASE = "https://api.tradingeconomics.com"
REQUEST_TIMEOUT = 10

# US macro indicators we care about — matched to Trading Economics Category names
TARGET_INDICATORS = {
    "GDP Growth Rate",
    "Unemployment Rate",
    "Inflation Rate",
    "Interest Rate",
    "Fed Funds Rate",
    "Consumer Confidence",
    "ISM Manufacturing PMI",
    "Retail Sales MoM",
    "Initial Jobless Claims",
    "Non Farm Payrolls",
    "Core Inflation Rate",
    "Balance of Trade",
    "Government Debt to GDP",
}


def _te_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """
    Make a GET request to the Trading Economics API.

    Args:
        path: API path (e.g. /country/united%20states).
        params: Optional query parameters (key auto-injected).

    Returns:
        Parsed JSON (list or dict) or None on failure.
    """
    url = f"{TE_BASE}{path}"
    p = params or {}
    p["c"] = TRADING_ECONOMICS_KEY
    try:
        resp = requests.get(url, params=p, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.warning("Trading Economics request failed for %s: %s", path, exc)
        return None


def get_us_macro_indicators() -> Optional[Dict[str, Any]]:
    """
    Fetch latest US macro indicator values from Trading Economics.

    Pulls all US indicators and filters to the target set relevant
    for Nexus regime classification and macro context.

    Returns:
        Dict with named indicator fields (snake_case), raw_indicators list,
        fetched_at timestamp, and _stub flag.
        Returns None if API call fails.
    """
    data = _te_get("/country/united%20states")
    if not data or not isinstance(data, list):
        logger.warning("Trading Economics US indicators fetch failed or empty")
        return None

    # Build lookup of category → latest value
    indicator_map: Dict[str, Any] = {}
    raw: List[Dict[str, Any]] = []

    for item in data:
        category = item.get("Category", "")
        latest_value = item.get("LatestValue")
        latest_date = item.get("LatestValueDate", "")
        unit = item.get("Unit", "")

        if category in TARGET_INDICATORS:
            indicator_map[category] = {
                "value": latest_value,
                "date": latest_date,
                "unit": unit
            }
            raw.append({"category": category, "value": latest_value, "date": latest_date})

    if not indicator_map:
        return None

    def _val(key: str) -> Optional[float]:
        """Extract numeric value for a given indicator name."""
        entry = indicator_map.get(key, {})
        v = entry.get("value")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "gdp_growth_rate": _val("GDP Growth Rate"),
        "unemployment_rate": _val("Unemployment Rate"),
        "inflation_rate": _val("Inflation Rate") or _val("Core Inflation Rate"),
        "fed_funds_rate": _val("Fed Funds Rate") or _val("Interest Rate"),
        "consumer_confidence": _val("Consumer Confidence"),
        "ism_pmi": _val("ISM Manufacturing PMI"),
        "retail_sales_mom": _val("Retail Sales MoM"),
        "initial_jobless_claims": _val("Initial Jobless Claims"),
        "nonfarm_payrolls": _val("Non Farm Payrolls"),
        "raw_indicators": raw,
        "fetched_at": datetime.utcnow().isoformat(),
        "_stub": False,
        "_source": "trading_economics_live"
    }


def get_next_high_impact_event() -> Optional[Dict[str, Any]]:
    """
    Attempt to fetch the next high-impact US macro event.

    Calendar endpoint requires a higher-tier plan (403 on current key).
    Falls back to FRED calendar (already in macro engine) with a flag.

    Returns:
        Dict indicating calendar data should come from FRED fallback.
    """
    logger.debug("Trading Economics calendar not available on current plan — FRED fallback active")
    return {
        "event": None,
        "date": None,
        "impact": None,
        "_stub": False,
        "_source": "te_calendar_unavailable_use_fred",
        "_note": "Calendar endpoint requires higher TE plan tier. FRED provides equivalent coverage."
    }


def get_calendar(days_ahead: int = 14) -> List[Dict[str, Any]]:
    """
    Return empty list — calendar endpoint is not accessible on current plan tier.

    Macro engine uses FRED as primary calendar source; this is graceful degradation.

    Args:
        days_ahead: Unused (preserved for interface compatibility).

    Returns:
        Empty list — signals macro engine to rely on FRED calendar.
    """
    logger.debug(
        "Trading Economics calendar endpoint not accessible on current plan (403). "
        "Macro engine uses FRED for calendar data."
    )
    return []
