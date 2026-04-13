"""
main.py — AILS FastAPI service entry point.
Port 8008 | Auth: X-Ails-Secret header
"""

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config
from database import init_ails_db, init_backtest_db
from regime import get_current_regime, classify_vix
from bayesian import get_win_rate, ingest_outcome
from patterns import store_pattern, find_similar
from reports import (
    generate_eod_report, deliver_eod_report,
    generate_eow_report, deliver_eow_report,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ails")

# ---------------------------------------------------------------------------
# DB connections (module-level, thread-safe WAL mode)
# ---------------------------------------------------------------------------

_ails_conn = None
# OMNI H1 fix: track in-flight background tasks to drain before closing DB connection.
import threading as _threading
_bg_tasks: list = []
_bg_lock  = _threading.Lock()
_backtest_conn = None
_db_lock = threading.Lock()


def get_ails_conn():
    """Return AILS DB connection."""
    return _ails_conn


def get_backtest_conn():
    """Return backtest DB connection."""
    return _backtest_conn


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _verify_auth(x_ails_secret: Optional[str]) -> None:
    """Raise 403 if auth header is missing or invalid.

    Cipher P2-3 fix: replaced != with secrets.compare_digest (timing-safe).
    AILS is the learning system — a timing oracle here allows injecting fabricated
    outcomes that silently corrupt Bayesian win rates over time.
    """
    import secrets as _sec
    if not x_ails_secret or not _sec.compare_digest(x_ails_secret, config.AILS_SECRET):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Ails-Secret")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class OutcomeRequest(BaseModel):
    """Completed trade outcome submitted by Alpha/Prime Execution."""
    ticker: str = Field(..., description="Ticker symbol")
    strategy: str = Field(..., description="Strategy type (bull_put_spread, swing_long, etc.)")
    regime: str = Field(..., description="Regime at trade entry")
    direction: str = Field(..., description="Trade direction (bullish, bearish, neutral)")
    pnl: float = Field(..., description="Realized PnL in dollars")
    win: bool = Field(..., description="True if trade was profitable")
    system: str = Field(..., description="alpha or prime")
    concordance_path: Optional[str] = Field(None, description="P1, P2, P3, P4")
    agent_votes: Optional[Dict[str, bool]] = Field(None, description="Agent vote map")
    setup_vector: Optional[Dict[str, float]] = Field(None, description="Setup signal values")


class ContextResponse(BaseModel):
    """Historical context returned for a ticker."""
    ticker: str
    strategy: str
    regime: str
    direction: str
    win_rate: float
    confidence: str
    sample_count: int
    source: str
    similar_setups: Optional[List[Dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DBs on startup."""
    global _ails_conn, _backtest_conn
    log.info("AILS starting — initializing databases")
    _ails_conn = init_ails_db(config.AILS_DB_PATH)
    _backtest_conn = init_backtest_db(config.BACKTEST_DB_PATH)
    log.info("AILS ready — port %d", config.PORT)
    yield
    log.info("AILS shutting down")
    # OMNI H1 fix: drain in-flight background calibration tasks before closing DB.
    # FastAPI BackgroundTasks do NOT block lifespan teardown — without this drain,
    # _update_agent_calibration races the connection close and gets ProgrammingError.
    _drain_timeout = 5.0  # seconds
    import time as _time
    deadline = _time.monotonic() + _drain_timeout
    while _time.monotonic() < deadline:
        with _bg_lock:
            if not _bg_tasks:
                break
        _time.sleep(0.05)
    with _bg_lock:
        remaining = len(_bg_tasks)
    if remaining:
        log.warning("AILS shutdown: %d background tasks still running after %.1fs drain — closing anyway",
                    remaining, _drain_timeout)
    if _ails_conn:
        _ails_conn.close()
    if _backtest_conn:
        _backtest_conn.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AILS — Adaptive Intelligent Learning System",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# POST /outcome — ingest completed trade
# ---------------------------------------------------------------------------

@app.post("/outcome")
def post_outcome(
    body: OutcomeRequest,
    background_tasks: BackgroundTasks,
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """
    Ingest a completed trade outcome.
    Updates Bayesian win rates and pattern library.
    """
    _verify_auth(x_ails_secret)

    now = datetime.now(timezone.utc).isoformat()
    ails = get_ails_conn()
    backtest = get_backtest_conn()

    # Store outcome + Bayesian update + pattern — all in one atomic transaction.
    # Adversarial fix #2: previous code committed after INSERT then updated Bayesian
    # separately. A crash between steps left the DB in an inconsistent state.
    with _db_lock:
        try:
            ails.execute("BEGIN IMMEDIATE")
            ails.execute(
                "INSERT INTO live_outcomes "
                "(ts, ticker, strategy, regime, direction, pnl, win, system, "
                "concordance_path, agent_votes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    body.ticker,
                    body.strategy,
                    body.regime,
                    body.direction,
                    body.pnl,
                    int(body.win),
                    body.system,
                    body.concordance_path,
                    json.dumps(body.agent_votes) if body.agent_votes else None,
                ),
            )
            # Bayesian update (direction-aware — adversarial fix #1)
            ingest_outcome(
                body.ticker, body.strategy, body.regime, body.direction,
                body.win, ails, backtest,
            )
            # Store pattern
            if body.setup_vector:
                store_pattern(
                    body.ticker, body.setup_vector, body.win,
                    body.regime, body.pnl, body.strategy, ails,
                )
            ails.commit()
        except Exception as exc:
            ails.rollback()
            log.error("AILS outcome transaction rolled back for %s: %s", body.ticker, exc)
            raise

    # Update agent calibration in background
    if body.agent_votes:
        background_tasks.add_task(
            _update_agent_calibration,
            body.agent_votes, body.win, body.strategy, body.regime,
        )

    log.info("Outcome ingested: %s %s/%s win=%s pnl=%.2f",
             body.ticker, body.strategy, body.regime, body.win, body.pnl)

    return JSONResponse({"status": "ok", "ticker": body.ticker, "win": body.win})


def _update_agent_calibration(
    agent_votes: Dict[str, bool],
    actual_win: bool,
    strategy: str,
    regime: str,
) -> None:
    """Update per-agent calibration metrics.

    OMNI H1 fix: registers thread in _bg_tasks on entry and removes on exit,
    so the lifespan drain loop can wait for completion before closing the DB.
    """
    t = _threading.current_thread()
    with _bg_lock:
        _bg_tasks.append(t)
    try:
        _update_agent_calibration_impl(agent_votes, actual_win, strategy, regime)
    finally:
        with _bg_lock:
            if t in _bg_tasks:
                _bg_tasks.remove(t)


def _update_agent_calibration_impl(
    agent_votes: Dict[str, bool],
    actual_win: bool,
    strategy: str,
    regime: str,
) -> None:
    """Inner calibration update logic (called by tracked wrapper above)."""
    ails = get_ails_conn()
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        for agent, predicted_go in agent_votes.items():
            predicted_conf = 0.75 if predicted_go else 0.35
            actual_wr = 1.0 if actual_win else 0.0

            existing = ails.execute(
                "SELECT predicted_confidence, actual_win_rate, n FROM agent_calibration "
                "WHERE agent=? AND strategy=? AND regime=?",
                (agent, strategy, regime),
            ).fetchone()

            if existing:
                n = existing["n"]
                new_n = n + 1
                # Rolling average update
                new_pred = (existing["predicted_confidence"] * n + predicted_conf) / new_n
                new_actual = (existing["actual_win_rate"] * n + actual_wr) / new_n
            else:
                new_n = 1
                new_pred = predicted_conf
                new_actual = actual_wr

            ails.execute(
                "INSERT INTO agent_calibration "
                "(agent, strategy, regime, predicted_confidence, actual_win_rate, n, last_updated) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(agent, strategy, regime) DO UPDATE SET "
                "predicted_confidence=excluded.predicted_confidence, "
                "actual_win_rate=excluded.actual_win_rate, "
                "n=excluded.n, last_updated=excluded.last_updated",
                (agent, strategy, regime, new_pred, new_actual, new_n, now),
            )
        ails.commit()


# ---------------------------------------------------------------------------
# GET /context/{ticker} — historical context for ORACLE / agents
# ---------------------------------------------------------------------------

@app.get("/context/{ticker}")
def get_context(
    ticker: str,
    strategy: str = "bull_put_spread",
    regime: Optional[str] = None,
    direction: str = "bullish",
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """
    Return historical win rates and agent calibration context for a ticker.
    Used by ORACLE as Engine 7 and by OMNI pre-synthesis.
    """
    _verify_auth(x_ails_secret)

    ails = get_ails_conn()
    backtest = get_backtest_conn()

    if regime is None:
        regime_info = get_current_regime()
        regime = regime_info.get("regime", "UNKNOWN")

    # Adversarial fix #3: all DB reads serialized on _db_lock to avoid concurrent
    # read/write conflicts on the shared global connection.
    with _db_lock:
        win_data = get_win_rate(ticker, strategy, regime, direction, ails, backtest)

        # Calibration for all agents
        cal_rows = ails.execute(
            "SELECT agent, predicted_confidence, actual_win_rate, n "
            "FROM agent_calibration WHERE strategy=? AND regime=?",
            (strategy, regime),
        ).fetchall()

    calibration = {
        row["agent"]: {
            "predicted": round(row["predicted_confidence"], 3),
            "actual": round(row["actual_win_rate"], 3),
            "n": row["n"],
        }
        for row in cal_rows
    }

    return JSONResponse({
        "ticker": ticker,
        "strategy": strategy,
        "regime": regime,
        "direction": direction,
        "win_rate": win_data["win_rate"],
        "confidence": win_data["confidence"],
        "sample_count": win_data["sample_count"],
        "source": win_data["source"],
        "agent_calibration": calibration,
        "cache_ttl_s": 900,  # Tells ORACLE to cache for 15 min
    })


# ---------------------------------------------------------------------------
# GET /similar/{ticker} — find similar historical setups
# ---------------------------------------------------------------------------

@app.get("/similar/{ticker}")
def get_similar(
    ticker: str,
    regime: str = "NORMAL",
    strategy: str = "bull_put_spread",
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """Return top-10 most similar historical setups for pattern matching."""
    _verify_auth(x_ails_secret)
    ails = get_ails_conn()

    # Basic vector: use regime/strategy defaults
    setup_vector = {"iv_rank": 0.5, "rsi": 50.0, "momentum": 0.0}
    similar = find_similar(setup_vector, regime, strategy, ails)

    return JSONResponse({
        "ticker": ticker,
        "regime": regime,
        "strategy": strategy,
        "similar_count": len(similar),
        "setups": similar,
    })


# ---------------------------------------------------------------------------
# GET /calibration/{agent} — per-agent calibration metrics
# ---------------------------------------------------------------------------

@app.get("/calibration/{agent}")
def get_calibration(
    agent: str,
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """Return calibration metrics for a specific agent."""
    _verify_auth(x_ails_secret)
    ails = get_ails_conn()

    rows = ails.execute(
        "SELECT strategy, regime, predicted_confidence, actual_win_rate, n "
        "FROM agent_calibration WHERE agent=?",
        (agent,),
    ).fetchall()

    if not rows:
        return JSONResponse({
            "agent": agent,
            "status": "no_data",
            "message": "No calibration data yet — submit trade outcomes via POST /outcome",
        })

    total_n = sum(r["n"] for r in rows)
    avg_error = sum(
        abs(r["predicted_confidence"] - r["actual_win_rate"]) * r["n"]
        for r in rows
    ) / total_n if total_n > 0 else 0.0

    return JSONResponse({
        "agent": agent,
        "total_outcomes": total_n,
        "avg_calibration_error": round(avg_error, 3),
        "by_strategy_regime": [
            {
                "strategy": r["strategy"],
                "regime": r["regime"],
                "predicted": round(r["predicted_confidence"], 3),
                "actual": round(r["actual_win_rate"], 3),
                "n": r["n"],
            }
            for r in rows
        ],
    })


# ---------------------------------------------------------------------------
# GET /regime/history — regime classification history
# ---------------------------------------------------------------------------

@app.get("/regime/history")
def get_regime_history(
    days: int = 30,
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """Return recent regime classification history."""
    _verify_auth(x_ails_secret)
    backtest = get_backtest_conn()

    rows = backtest.execute(
        "SELECT date, vix_close, regime FROM regime_history "
        "ORDER BY date DESC LIMIT ?",
        (days,),
    ).fetchall()

    current = get_current_regime()

    return JSONResponse({
        "current": current,
        "history": [{"date": r["date"], "vix": r["vix_close"], "regime": r["regime"]}
                    for r in rows],
    })


# ---------------------------------------------------------------------------
# GET /report/eod — trigger/return EOD report
# ---------------------------------------------------------------------------

@app.get("/report/eod")
def get_eod_report(
    date: Optional[str] = None,
    deliver: bool = False,
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """Generate (and optionally deliver) EOD report."""
    _verify_auth(x_ails_secret)
    ails = get_ails_conn()
    backtest = get_backtest_conn()

    report = generate_eod_report(ails, backtest, date)

    if deliver:
        deliver_eod_report(report)

    return JSONResponse(report)


# ---------------------------------------------------------------------------
# GET /report/eow — trigger/return EOW report
# ---------------------------------------------------------------------------

@app.get("/report/eow")
def get_eow_report(
    week_ending: Optional[str] = None,
    deliver: bool = False,
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """Generate (and optionally deliver) EOW report."""
    _verify_auth(x_ails_secret)
    ails = get_ails_conn()
    backtest = get_backtest_conn()

    report = generate_eow_report(ails, backtest, week_ending)

    if deliver:
        deliver_eow_report(report)

    return JSONResponse(report)


# ---------------------------------------------------------------------------
# GET /backtest/status — backtest population status
# ---------------------------------------------------------------------------

@app.get("/backtest/status")
def get_backtest_status(
    x_ails_secret: Optional[str] = Header(None),
) -> JSONResponse:
    """Return status of the historical backtest data population."""
    _verify_auth(x_ails_secret)
    backtest = get_backtest_conn()

    hist_count = backtest.execute(
        "SELECT COUNT(*) as n FROM historical_win_rates"
    ).fetchone()["n"]

    regime_count = backtest.execute(
        "SELECT COUNT(*) as n FROM regime_history"
    ).fetchone()["n"]

    regime_rates_count = backtest.execute(
        "SELECT COUNT(*) as n FROM regime_level_rates"
    ).fetchone()["n"]

    meta = {
        row["key"]: row["value"]
        for row in backtest.execute("SELECT key, value FROM backtest_meta").fetchall()
    }

    ails = get_ails_conn()
    live_outcomes = ails.execute(
        "SELECT COUNT(*) as n FROM live_outcomes"
    ).fetchone()["n"]

    bayesian_entries = ails.execute(
        "SELECT COUNT(*) as n FROM bayesian_rates"
    ).fetchone()["n"]

    return JSONResponse({
        "historical_win_rates": hist_count,
        "regime_days_loaded": regime_count,
        "regime_level_rates_seeded": regime_rates_count,
        "live_outcomes_ingested": live_outcomes,
        "bayesian_entries": bayesian_entries,
        "meta": meta,
        "status": "ready" if regime_rates_count > 0 else "initializing",
    })


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> JSONResponse:
    """Service health check."""
    ails = get_ails_conn()
    backtest = get_backtest_conn()

    ails_ok = False
    backtest_ok = False
    live_count  = 0

    # Cipher P2-10 fix: acquire _db_lock for all health-check DB reads.
    # _db_lock's intent is to serialize access on the shared global connection.
    # The health endpoint was the only reader that bypassed it.
    with _db_lock:
        try:
            ails.execute("SELECT 1").fetchone()
            ails_ok = True
        except Exception:
            pass

        try:
            backtest.execute("SELECT 1").fetchone()
            backtest_ok = True
        except Exception:
            pass

        try:
            live_count = ails.execute(
                "SELECT COUNT(*) FROM live_outcomes"
            ).fetchone()[0]
        except Exception:
            pass

    return JSONResponse({
        "status": "healthy" if (ails_ok and backtest_ok) else "degraded",
        "service": "ails",
        "version": "1.0.0",
        "port": config.PORT,
        "subsystems": {
            "ails_db": "ok" if ails_ok else "error",
            "backtest_db": "ok" if backtest_ok else "error",
        },
        "live_outcomes_total": live_count,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
    )
