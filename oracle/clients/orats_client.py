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
"""

import logging
import os
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

ORATS_TOKEN = "4476e955-241a-4540-b114-ebbf1a3a3b87"
ORATS_BASE  = "https://api.orats.io/datav2"
TIMEOUT     = 10  # seconds


def _get(endpoint: str, params: dict) -> Optional[Dict[str, Any]]:
    """Raw GET against ORATS datav2 API."""
    params["token"] = ORATS_TOKEN
    try:
        resp = requests.get(f"{ORATS_BASE}/{endpoint}", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", [])
        if not rows:
            logger.warning("ORATS %s returned empty data for %s", endpoint, params.get("ticker"))
            return None
        return rows[0]
    except requests.Timeout:
        logger.warning("ORATS %s timed out for %s", endpoint, params.get("ticker"))
        return None
    except requests.HTTPError as e:
        logger.warning("ORATS %s HTTP error for %s: %s", endpoint, params.get("ticker"), e)
        return None
    except Exception as e:
        logger.error("ORATS %s unexpected error for %s: %s", endpoint, params.get("ticker"), e)
        return None


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
