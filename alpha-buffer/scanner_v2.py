"""
scanner.py — Stateless scan cycle + APScheduler wiring
=======================================================
1C from spec: Pure function. No instance state. No threads. No locks.
APScheduler max_instances=1 prevents overlap.
DB UNIQUE constraint on processed_picks handles the Railway rolling-restart
dual-instance edge case (C3 critique — already handled by 1D idempotency).

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from data_contracts import (
    ScanResult, TickerContext,
    NoVolatilityDataError, NoTechnicalDataError,
)
from data_fetchers import get_ticker_context
from market_state import MarketState, get_scanning_allowed
from scorer import compute_score_from_context  # existing shared scorer

logger = logging.getLogger(__name__)

SCAN_INTERVAL_MINUTES = 15
# NEXUS_SECRET is passed in via create_scheduler() from the caller's env — never hardcoded here.


# ── DB helpers ────────────────────────────────────────────────────────────────

@contextmanager
def get_db(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_et_date() -> str:
    """Current date in ET. C6 fix: never use date.today() on a UTC server.
    BUG-FIX (Axiom 3.8 / Sage B1): fallback now uses pytz instead of date.today()
    so UTC servers never return the wrong date during the 8PM-midnight ET window.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        try:
            import pytz as _pytz
            return datetime.now(_pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
        except Exception:
            # Last resort: UTC offset hardcode (-5h winter / -4h summer)
            import time as _t
            _utc_offset = -4 if _t.localtime().tm_isdst else -5
            from datetime import timedelta
            return (datetime.utcnow() + timedelta(hours=_utc_offset)).strftime("%Y-%m-%d")


def make_pick_id(ticker: str, window_id: str) -> str:
    """Deterministic pick ID — same input always produces same ID."""
    et_date = get_et_date()
    return f"{et_date}-{ticker.upper()}-{window_id}"


def pick_already_processed(db_path: str, pick_id: str) -> bool:
    """Check processed_picks table. Survives restarts — no in-memory state."""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_picks WHERE pick_id = ?", (pick_id,)
        ).fetchone()
    return row is not None


def record_pick(db_path: str, pick_id: str, ticker: str, direction: str,
                window_id: str, verdict: str, score: Optional[float],
                pathway: Optional[str]) -> bool:
    """
    Insert into processed_picks. Returns False if already exists (UNIQUE violation).
    This is the idempotency guarantee — duplicate processing is impossible.
    """
    try:
        with get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO processed_picks
                   (pick_id, ticker, direction, window_id, arena, verdict, score, pathway)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (pick_id, ticker, direction, window_id, "alpha", verdict, score, pathway)
            )
        return True
    except sqlite3.IntegrityError:
        return False  # already processed — safe to ignore


def fetch_pool_from_axiom(axiom_url: str, nexus_secret: str) -> list[dict]:
    """Fetch current ticker pool from Axiom."""
    import requests
    try:
        r = requests.get(
            f"{axiom_url}/pool",
            headers={"X-Nexus-Secret": nexus_secret},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            pool = data.get("pool", data.get("tickers", []))
            return [{"ticker": t, "direction": "bullish"} for t in pool if isinstance(t, str)]
        logger.warning("Axiom /pool returned %s", r.status_code)
        return []
    except Exception as e:
        logger.error("Axiom /pool fetch failed: %s", e)
        return []


def fetch_axiom_score(ticker: str, axiom_url: str, nexus_secret: str,
                      ivr: float, dte: int = 35) -> Optional[dict]:
    """Fetch Axiom risk assessment. Returns None on failure — caller skips."""
    import requests
    try:
        r = requests.post(
            f"{axiom_url}/assess",
            headers={"Content-Type": "application/json", "X-Nexus-Secret": nexus_secret},
            json={"ticker": ticker, "strategy": "bull_put_spread",
                  "ivr": ivr, "dte": dte},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning("Axiom /assess returned %s for %s", r.status_code, ticker)
        return None
    except Exception as e:
        logger.warning("Axiom /assess failed for %s: %s", ticker, e)  # BUG-FIX (Axiom 3.7): args were swapped
        return None


def check_score_distribution(scores: list[float], window_id: str) -> None:
    """
    3B from spec: Raise if >60% of scores are identical.
    Uniform scores = broken data. Stop the cycle immediately.
    """
    if len(scores) < 5:
        return
    most_common = max(set(scores), key=scores.count)
    pct = scores.count(most_common) / len(scores)
    if pct > 0.60:
        raise UniformScoreError(
            f"BROKEN DATA: {pct:.0%} of {len(scores)} scores = {most_common:.1f} "
            f"in window {window_id}. Data layer returning defaults. Halting cycle."
        )


class UniformScoreError(Exception):
    pass


# ── Pure Scan Function ────────────────────────────────────────────────────────

def run_scan_cycle(
    db_path:     str,
    axiom_url:   str,
    nexus_secret: str,
    market_state: MarketState,
    alert_fn,        # callable(msg) — Telegram alert
    omni_dispatch_fn,  # callable(ticker, context, axiom, score) — sends to OMNI synthesis
    *,
    _event_loop=None,  # event loop for run_coroutine_threadsafe — passed by scheduler wrapper
) -> ScanResult:
    """
    1C from spec: Pure function. No instance state. No threads. No locks.
    Crashes clean. Restarts clean. DB handles all durability.

    Called by APScheduler every 15 minutes with max_instances=1.
    """
    # Derive current window ID
    now_et = datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")
                          if hasattr(__import__("zoneinfo"), "ZoneInfo") else timezone.utc)
    m = (now_et.minute // 15) * 15
    window_id = f"{now_et.strftime('%Y-%m-%d')}-{now_et.hour:02d}{m:02d}"

    result = ScanResult(window_id=window_id, pool_size=0)

    # Gate 1: Market state check (staleness-aware)
    if not get_scanning_allowed(market_state):
        reason = market_state.pause_reason or "MarketState not ready"
        logger.info("[Scanner] Scan blocked: %s", reason)
        result.log_skip("market_state_blocked")
        return result

    # Fetch pool
    pool = fetch_pool_from_axiom(axiom_url, nexus_secret)
    if not pool:
        logger.warning("[Scanner] Empty pool from Axiom — skipping cycle")
        result.log_skip("empty_pool")
        return result

    result.pool_size = len(pool)
    scores_this_cycle = []

    for entry in pool:
        ticker    = entry.get("ticker", "").upper().strip()
        direction = entry.get("direction", "bullish")
        pick_id   = make_pick_id(ticker, window_id)

        # Skip already-processed (DB-backed dedup — survives restarts)
        if pick_already_processed(db_path, pick_id):
            result.log_skip("already_processed")
            continue

        # Fetch typed context — raises on critical data failure
        try:
            ctx: TickerContext = get_ticker_context(ticker)
        except NoVolatilityDataError:
            logger.warning("[Scanner] %s: no volatility data — skipping", ticker)
            result.log_skip("no_volatility_data")
            record_pick(db_path, pick_id, ticker, direction, window_id, "SKIP", None, None)
            continue
        except NoTechnicalDataError:
            logger.warning("[Scanner] %s: no technical data — skipping", ticker)
            result.log_skip("no_technical_data")
            record_pick(db_path, pick_id, ticker, direction, window_id, "SKIP", None, None)
            continue
        except Exception as e:
            logger.error("[Scanner] %s: unexpected data error: %s", ticker, e)
            result.log_skip("data_error")
            continue

        # Mark degraded sources
        if ctx.vol.is_fallback:
            result.degraded = True
            if "orats" not in result.degraded_sources:
                result.degraded_sources.append("orats")

        # FIX-STALE-IVR (2026-05-04): Guard against after-hours stale IVR data.
        # ORATS returns 0 or None after market close. Passing IVR=0 to Axiom
        # triggers the IVR < 30 hard block for all credit spreads, generating
        # a 100% skip rate with 'axiom_hard_stop' — a false alarm, not a real signal.
        # Skip the ticker with 'stale_ivr' reason instead of letting Axiom block it.
        _ivr = ctx.vol.iv_rank
        if _ivr is None or _ivr <= 0:
            logger.warning(
                "[Scanner] %s: IVR is %s — likely stale after-hours data. Skipping.",
                ticker, _ivr,
            )
            result.log_skip("stale_ivr")
            continue

        # Fetch Axiom risk assessment
        axiom = fetch_axiom_score(ticker, axiom_url, nexus_secret,
                                  ivr=_ivr)
        if axiom is None:
            result.log_skip("axiom_unavailable")
            continue

        # Axiom hard stops — skip immediately
        # FIX-STALE-IVR: If ALL hard stops are IVR-related and market is closed,
        # this is almost certainly stale data — log as stale_ivr, not axiom_hard_stop.
        if axiom.get("hard_stops"):
            stops = axiom["hard_stops"]
            _is_ivr_stop = all("IVR" in s or "ivr" in s.lower() for s in stops)
            _market_closed = not market_state.scanning_allowed
            if _is_ivr_stop and _market_closed:
                logger.warning(
                    "[Scanner] %s: Axiom IVR hard stop during market-closed window — "
                    "treating as stale data, not a trading signal: %s",
                    ticker, stops[0][:80],
                )
                result.log_skip("stale_ivr_axiom")
                continue
            logger.info("[Scanner] %s blocked by Axiom: %s", ticker, stops[0][:60])
            result.log_skip("axiom_hard_stop")
            record_pick(db_path, pick_id, ticker, direction, window_id,
                        "NO_GO", None, "AXIOM_BLOCKED")
            continue

        # Compute deterministic score
        try:
            score_result = compute_score_from_context(ctx, direction)
        except Exception as e:
            logger.error("[Scanner] %s: scoring failed: %s", ticker, e)
            result.log_skip("scoring_error")
            continue

        scores_this_cycle.append(float(score_result.score))

        # Score below threshold — record and skip
        if score_result.score < 58.0:
            record_pick(db_path, pick_id, ticker, direction, window_id,
                        "NO_GO", score_result.score, None)
            continue

        # Dispatch to OMNI synthesis
        try:
            omni_dispatch_fn(ticker, ctx, axiom, score_result)
            result.verdicts.append({
                "ticker":    ticker,
                "direction": direction,
                "score":     score_result.score,
                "pathway":   score_result.recommendation,
                "window_id": window_id,
            })
            record_pick(db_path, pick_id, ticker, direction, window_id,
                        "GO", score_result.score, score_result.recommendation)
            logger.info("[Scanner] GO: %s score=%.1f", ticker, score_result.score)
        except Exception as e:
            logger.error("[Scanner] %s: OMNI dispatch failed: %s", ticker, e)
            result.log_skip("omni_dispatch_failed")

    # Score distribution check (3B) — raises UniformScoreError if data broken
    # G1 FIX (2026-05-03): run_scan_cycle() executes in a thread pool via
    # loop.run_in_executor(). asyncio.create_task() requires a *running* event loop
    # on the current thread — which thread pool threads do not have. Use
    # run_coroutine_threadsafe(coro, loop) instead, which is safe from any thread.
    def _fire_alert(msg: str) -> None:
        if _event_loop is not None and not _event_loop.is_closed():
            import asyncio as _asyncio
            _asyncio.run_coroutine_threadsafe(alert_fn(msg), _event_loop)
        else:
            # Fallback: log the alert so it's never silently dropped
            logger.warning("[Scanner] Alert (no loop): %s", msg[:200])

    try:
        check_score_distribution(scores_this_cycle, window_id)
    except UniformScoreError as e:
        logger.critical("[Scanner] %s", e)
        result.uniform_score_flag = True
        _fire_alert(
            f"🔴 <b>UNIFORM SCORE ALERT</b>\n"
            f"Window {window_id}: {len(scores_this_cycle)} scores all = "
            f"{scores_this_cycle[0] if scores_this_cycle else '?'}\n"
            f"DATA FAILURE — Oracle likely returning defaults.\n"
            f"Verdicts from this window should be discarded."
        )

    # C2 critique: alert if >50% of pool skipped
    if result.is_data_failure():
        _fire_alert(
            f"⚠️ <b>HIGH SKIP RATE</b> — window {window_id}\n"
            f"{result.skip_count}/{result.pool_size} tickers skipped "
            f"({result.skip_rate:.0%})\n"
            f"Likely data failure. Skip reasons: {result.skips}"
        )

    # Persist cycle to DB
    try:
        with get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scan_cycles
                   (window_id, completed_at, pool_size, verdicts_go, verdicts_nogo,
                    skips_total, skip_reasons, data_degraded, degraded_sources, uniform_score_flag)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (window_id,
                 datetime.now(timezone.utc).isoformat(),
                 result.pool_size,
                 len(result.verdicts),
                 result.skip_count - len(result.verdicts),
                 result.skip_count,
                 json.dumps(result.skips),
                 int(result.degraded),
                 json.dumps(result.degraded_sources),
                 int(result.uniform_score_flag))
            )
    except Exception as e:
        logger.warning("[Scanner] Cycle DB persist failed: %s", e)

    logger.info(
        "[Scanner] Cycle complete: window=%s pool=%d go=%d skips=%d degraded=%s",
        window_id, result.pool_size, len(result.verdicts), result.skip_count, result.degraded
    )
    return result


# ── APScheduler wiring ────────────────────────────────────────────────────────

def create_scheduler(
    db_path: str,
    axiom_url: str,
    nexus_secret: str,
    market_state: MarketState,
    alert_fn,
    omni_dispatch_fn,
) -> AsyncIOScheduler:
    """
    1C from spec: APScheduler with max_instances=1.
    This replaces all thread management, watchdog, revival logic.
    If the job takes longer than misfire_grace_time, it's skipped — not doubled.
    """
    scheduler = AsyncIOScheduler(timezone="America/New_York")

    import asyncio

    async def _async_scan_wrapper():
        loop = asyncio.get_event_loop()
        # G1 FIX: pass the running event loop so run_scan_cycle() can fire
        # coroutine alerts via run_coroutine_threadsafe() from the thread pool.
        import functools
        await loop.run_in_executor(
            None,
            functools.partial(
                run_scan_cycle,
                db_path, axiom_url, nexus_secret,
                market_state, alert_fn, omni_dispatch_fn,
                _event_loop=loop,
            ),
        )

    scheduler.add_job(
        _async_scan_wrapper,
        trigger      = IntervalTrigger(minutes=SCAN_INTERVAL_MINUTES),
        id           = "omni_scanner",
        max_instances= 1,           # CRITICAL: prevents duplicate scan threads
        misfire_grace_time = 60,    # if missed by <60s, run it; else skip
        replace_existing = True,
    )

    return scheduler
