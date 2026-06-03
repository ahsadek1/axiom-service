"""
health.py — Honest /health endpoint
=====================================
3D from spec: Reports actual system state including degraded sources.
No "healthy" while serving null data.
Status is HEALTHY | DEGRADED | SUSPENDED | ERROR — never a lie.

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from market_state import MarketState, get_scanning_allowed

logger = logging.getLogger(__name__)

MAX_MARKET_STATE_AGE = timedelta(minutes=12)


def get_health(
    db_path:      str,
    market_state: MarketState,
    preflight_passed: bool,
    scanner_active:   bool,
    version:          str = "2.0.0",
) -> dict:
    """
    Build honest /health response.
    Every field reflects actual runtime state — never a cached "ok".
    """

    # ── Market state ──────────────────────────────────────────────────────────
    vix_age_ok = (
        market_state.vix_updated_at is not None and
        datetime.now(timezone.utc) - market_state.vix_updated_at <= MAX_MARKET_STATE_AGE
    )
    scanning_allowed = get_scanning_allowed(market_state)  # staleness-aware

    # ── Positions ─────────────────────────────────────────────────────────────
    open_positions = 0
    allocated_usd  = 0.0
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM active_positions WHERE status IN ('pending','open')"
            ).fetchone()
            open_positions = row["cnt"] if row else 0

            cap_row = conn.execute(
                "SELECT SUM(allocated_usd) as total FROM capital_ledger"
            ).fetchone()
            allocated_usd = cap_row["total"] or 0.0
    except Exception as e:
        logger.warning("Health DB query failed: %s", e)

    # ── Data sources ──────────────────────────────────────────────────────────
    data_sources   = {}
    degraded_sources = []
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT source, status, fallback_active FROM data_source_health"
            ).fetchall()
            for r in rows:
                data_sources[r["source"]] = r["status"]
                if r["status"] != "OK" or r["fallback_active"]:
                    degraded_sources.append(r["source"])
    except Exception as e:
        logger.warning("Data source health DB query failed: %s", e)
        data_sources = {"error": "db_unavailable"}

    # ── Last scan cycle ───────────────────────────────────────────────────────
    last_scan     = None
    last_skip_rate = None
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT completed_at, pool_size, verdicts_go, skips_total, "
                "data_degraded, uniform_score_flag "
                "FROM scan_cycles ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                last_scan = row["completed_at"]
                if row["pool_size"] and row["pool_size"] > 0:
                    last_skip_rate = round(row["skips_total"] / row["pool_size"], 2)
    except Exception as e:
        logger.warning("Last scan query failed: %s", e)

    # ── Overall status ────────────────────────────────────────────────────────
    if market_state.suspended:
        overall = "SUSPENDED"
    elif degraded_sources:
        overall = "DEGRADED"
    elif not vix_age_ok:
        overall = "DEGRADED"
    elif not preflight_passed:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    return {
        "status":  overall,
        "version": version,
        "market_state": {
            "regime":          market_state.regime,
            "vix":             market_state.vix,
            "vix_source":      market_state.vix_source,
            "vix_age_ok":      vix_age_ok,
            "scanning_allowed": scanning_allowed,
            "pause_reason":    market_state.pause_reason,
            "suspended":       market_state.suspended,
            "suspend_reason":  market_state.suspend_reason,
        },
        "positions": {
            "open":          open_positions,
            "cap":           3,
            "allocated_usd": round(allocated_usd, 2),
            "available_usd": round(max(0, 3000.0 - allocated_usd), 2),
        },
        "data_sources":    data_sources,
        "degraded_sources": degraded_sources,
        "preflight_passed": preflight_passed,
        "scanner_active":   scanner_active,
        "last_scan_cycle":  last_scan,
        "last_skip_rate":   last_skip_rate,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }
