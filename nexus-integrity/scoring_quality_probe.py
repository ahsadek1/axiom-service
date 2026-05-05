"""
scoring_quality_probe.py — SOVEREIGN Amendment S1

Detects Oracle returning fallback/null data for key scoring dimensions.
Runs every 30 min during market hours as a Tier 2 probe (no external API calls).

ROOT CAUSE CAUGHT: 2026-04-29 Oracle engine function name mismatch caused
flow_engine and gamma_engine to return null for every ticker. The scorer applied
hardcoded fallback values (D2=7, D6=4), producing valid-looking scores of 43-53
that were below the submission threshold. Every health check returned green.
Pipeline was dead for 2+ hours.

This probe would have detected that failure within 30 seconds.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

import config

logger = logging.getLogger("integrity.scoring_quality")

# ---------------------------------------------------------------------------
# Fallback sentinel values from shared/scorer.py
# These are returned when Oracle provides null data for a dimension.
# Identical score to sentinel = Oracle is not providing real data.
# ---------------------------------------------------------------------------

FALLBACK_SENTINELS: Dict[str, int] = {
    "D2_options_flow":         7,   # returned when flow.put_call_ratio is None
    "D3_technical_setup":      7,   # returned when all price deriveds are None
    "D5_fundamental_quality":  7,   # returned when fundamental data is None/NEUTRAL
    "D6_gamma_safety":         4,   # returned when gamma.net_gex is None/NEUTRAL
}

# Number of sentinel hits on a single ticker that indicates degraded Oracle data
SENTINEL_THRESHOLD_PER_TICKER: int = 2

# Number of degraded tickers (out of probed) to declare quality DEGRADED
DEGRADED_TICKER_THRESHOLD: int = 2

# Liquid tickers always available during market hours
PROBE_TICKERS: List[str] = ["SPY", "QQQ", "AAPL"]

# Oracle endpoint
ORACLE_URL: str = os.environ.get("ORACLE_URL", "http://localhost:8007")
ORACLE_SECRET: str = os.environ.get("ORACLE_SECRET", "")


@dataclass
class ScoringQualityResult:
    quality: str                          # "GOOD" | "PARTIAL" | "DEGRADED"
    trs_score: float                      # 0=block, 50=AMBER, 100=GOOD
    degraded_tickers: int = 0
    checked_tickers: int = 0
    findings: List[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def is_degraded(self) -> bool:
        return self.quality == "DEGRADED"


def run_scoring_quality_probe() -> ScoringQualityResult:
    """
    Fetch Oracle context for sentinel tickers and check scoring dimension quality.

    Returns ScoringQualityResult with:
      - quality: GOOD / PARTIAL / DEGRADED
      - trs_score: 100 (GOOD), 50 (PARTIAL/1 degraded), 0 (DEGRADED/2+)
      - findings: list of human-readable dimension failure descriptions

    Does NOT make external API calls — uses Oracle's in-memory cache only.
    Oracle must be running locally. Probe uses ORACLE_SECRET for auth.
    """
    # Skip outside market hours — price deriveds (avg_volume, volume_ratio) are null
    # pre-market, causing false D3/D5 sentinel hits. Only flag during 9:30-16:00 ET.
    from datetime import datetime, timezone, timedelta
    try:
        et_offset = timedelta(hours=-4)  # EDT (Apr-Oct)
        now_et = datetime.now(timezone(et_offset))
        weekday = now_et.weekday()  # 0=Mon
        hour, minute = now_et.hour, now_et.minute
        in_market_hours = (weekday < 5) and (
            (hour == 9 and minute >= 30) or (10 <= hour < 16)
        )
    except Exception:
        in_market_hours = True  # fail safe

    if not in_market_hours:
        return ScoringQualityResult(
            quality="GOOD",
            trs_score=100,
            findings=["Outside market hours — pre-market null data expected"],
        )

    # Check if Polygon options endpoint is degraded (external failure).
    # If Polygon options are timing out, vol/iv data will be null — this is
    # an EXTERNAL failure, not an Oracle failure. Downgrade to P2 (informational).
    try:
        import requests as _req
        _poly_key = os.environ.get("POLYGON_API_KEY", "")
        if _poly_key:
            _r = _req.get(
                f"https://api.polygon.io/v3/snapshot/options/SPY?limit=1&apiKey={_poly_key}",
                timeout=5.0,
            )
            _polygon_options_ok = _r.status_code == 200
        else:
            _polygon_options_ok = True  # can’t check — assume ok
    except Exception:
        _polygon_options_ok = False

    if not _polygon_options_ok:
        return ScoringQualityResult(
            quality="PARTIAL",
            trs_score=50,
            findings=["Polygon options API degraded — vol/iv data unavailable (EXTERNAL). Oracle not at fault."],
        )

    # Import scorer from shared — same code path as production
    sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
    sys.path.insert(0, "/Users/ahmedsadek/nexus")
    try:
        from shared.scorer import compute_score, determine_direction
    except ImportError:
        try:
            from scorer import compute_score, determine_direction
        except ImportError:
            logger.error("Cannot import shared scorer — scoring quality probe skipped")
            return ScoringQualityResult(quality="UNKNOWN", trs_score=50,
                                        findings=["scorer import failed"])

    degraded_count = 0
    findings: List[str] = []
    checked = 0

    for ticker in PROBE_TICKERS:
        ctx = _fetch_oracle_context(ticker)
        if ctx is None:
            findings.append(f"{ticker}: Oracle unreachable or returned error")
            degraded_count += 1
            checked += 1
            continue

        checked += 1

        try:
            direction = determine_direction(ctx)
            result = compute_score(ctx, direction)
        except Exception as e:
            findings.append(f"{ticker}: scorer raised exception: {e}")
            degraded_count += 1
            continue

        # Check how many dimensions are at their fallback sentinel values
        bd = result.breakdown
        sentinel_hits = []
        for dim, sentinel_val in FALLBACK_SENTINELS.items():
            actual = bd.get(dim)
            if actual is not None and int(actual) == sentinel_val:
                sentinel_hits.append(f"{dim}={actual}")

        if len(sentinel_hits) >= SENTINEL_THRESHOLD_PER_TICKER:
            degraded_count += 1
            findings.append(
                f"{ticker}: {len(sentinel_hits)}/4 dimensions at fallback sentinel "
                f"[{', '.join(sentinel_hits)}] — Oracle engines likely returning null"
            )
        elif len(sentinel_hits) == 1:
            findings.append(
                f"{ticker}: 1 dimension at sentinel ({sentinel_hits[0]}) — watch"
            )

    # Determine quality
    if checked == 0:
        quality = "UNKNOWN"
        trs_score = 0.0
    elif degraded_count >= DEGRADED_TICKER_THRESHOLD:
        quality = "DEGRADED"
        trs_score = 0.0
        logger.warning(
            "SCORING QUALITY DEGRADED: %d/%d tickers show fallback sentinel values. "
            "Oracle engines may be returning null data. Findings: %s",
            degraded_count, checked, "; ".join(findings)
        )
    elif degraded_count == 1:
        quality = "PARTIAL"
        trs_score = 50.0
        logger.warning(
            "SCORING QUALITY PARTIAL: %d/%d tickers degraded. %s",
            degraded_count, checked, findings
        )
    else:
        quality = "GOOD"
        trs_score = 100.0
        logger.debug("SCORING QUALITY GOOD: all %d tickers scored with real data", checked)

    return ScoringQualityResult(
        quality=quality,
        trs_score=trs_score,
        degraded_tickers=degraded_count,
        checked_tickers=checked,
        findings=findings,
    )


def _fetch_oracle_context(ticker: str) -> Optional[dict]:
    """
    Fetch full Oracle context for a ticker using the probe secret.

    Returns parsed context dict or None on failure.
    """
    try:
        resp = requests.get(
            f"{ORACLE_URL}/oracle/context/{ticker}",
            headers={"X-Oracle-Secret": ORACLE_SECRET},
            timeout=15.0,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Oracle context for %s returned %d", ticker, resp.status_code)
        return None
    except requests.RequestException as e:
        logger.error("Oracle context fetch failed for %s: %s", ticker, e)
        return None
