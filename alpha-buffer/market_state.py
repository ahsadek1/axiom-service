"""
market_state.py — Market State for Scanner Gate
================================================
Provides MarketState dataclass and get_scanning_allowed() function.
Used by scanner_v2.py to determine if scanning is permitted.

Rules:
  - Market hours: 9:30 AM - 3:30 PM ET, weekdays only
  - Axiom must be healthy (submissions_open=True)
  - VIX must not be in STOP territory (>= 35)
"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

MARKET_OPEN  = dt_time(9, 30)
MARKET_CLOSE = dt_time(15, 30)  # 3:30 PM — allow time to execute before 4PM close
VIX_STOP_THRESHOLD = 35.0


@dataclass
class MarketState:
    """Current market state for scanner gating."""
    scanning_allowed:   bool   = False
    pause_reason:       Optional[str] = None
    vix:                float  = 0.0
    regime:             str    = "UNKNOWN"
    submissions_open:   bool   = False
    is_market_hours:    bool   = False


def get_scanning_allowed(market_state: MarketState) -> bool:
    """
    Returns True if scanning is currently permitted.
    Called by scanner_v2.py as Gate 1.
    """
    return market_state.scanning_allowed


def build_market_state(
    axiom_url: str,
    nexus_secret: str,
) -> MarketState:
    """
    Build current MarketState by:
    1. Checking market hours (ET)
    2. Querying Axiom for VIX, regime, submissions_open
    Returns a fully populated MarketState.
    """
    now_et = datetime.now(_ET)
    current_time = now_et.time()
    is_weekday = now_et.weekday() < 5
    is_market_hours = (
        is_weekday
        and MARKET_OPEN <= current_time <= MARKET_CLOSE
    )

    if not is_market_hours:
        return MarketState(
            scanning_allowed=False,
            pause_reason=f"Outside market hours ({current_time.strftime('%H:%M')} ET)",
            is_market_hours=False,
        )

    # Query Axiom for live state
    try:
        resp = requests.get(
            f"{axiom_url}/health",
            headers={"X-Nexus-Secret": nexus_secret},
            timeout=5,
        )
        if resp.status_code != 200:
            return MarketState(
                scanning_allowed=False,
                pause_reason=f"Axiom health check failed: HTTP {resp.status_code}",
                is_market_hours=True,
            )

        data = resp.json()
        vix = float(data.get("vix", 0))
        regime = data.get("regime", "UNKNOWN")
        submissions_open = data.get("submissions_open", True)

        # VIX hard stop
        if vix >= VIX_STOP_THRESHOLD:
            return MarketState(
                scanning_allowed=False,
                pause_reason=f"VIX={vix:.1f} above STOP threshold {VIX_STOP_THRESHOLD}",
                vix=vix,
                regime=regime,
                submissions_open=submissions_open,
                is_market_hours=True,
            )

        # Axiom standby (after 3:30 PM Axiom closes submissions)
        if not submissions_open:
            return MarketState(
                scanning_allowed=False,
                pause_reason="AXIOM_STANDBY — submissions closed",
                vix=vix,
                regime=regime,
                submissions_open=False,
                is_market_hours=True,
            )

        return MarketState(
            scanning_allowed=True,
            pause_reason=None,
            vix=vix,
            regime=regime,
            submissions_open=True,
            is_market_hours=True,
        )

    except Exception as exc:
        return MarketState(
            scanning_allowed=False,
            pause_reason=f"Axiom unreachable: {exc}",
            is_market_hours=True,
        )
