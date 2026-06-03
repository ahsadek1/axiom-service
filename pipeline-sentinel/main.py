"""
main.py — FastAPI application for Pipeline Sentinel (port 8008).

Endpoints:
  POST /trace              — Record a pipeline hop (fire-and-forget, always 200)
  GET  /health             — Liveness check (no auth)
  GET  /system-health      — Current health score + context block (auth)
  GET  /pipeline/{id}      — Full trace for a pick (auth)
  GET  /stalls             — Currently stalled picks (auth)
  POST /report             — Accept Guardian Angel anomaly report (auth)

Background threads:
  - Scorer:     recomputes health score every SCORE_RECOMPUTE_INTERVAL_S seconds
  - Classifier: runs all 7 failure detectors every 30 seconds

Auth: X-Nexus-Secret header required on all protected endpoints.
"""

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

load_dotenv()

from classifier import FailureClassifier
from database import (
    get_stalled_picks,
    get_traces_for_id,
    init_db,
    insert_anomaly_report,
    insert_trace,
)
from models import AnomalyReport, SystemHealthResponse, TraceRequest
from notifier import TelegramNotifier
from scorer import HealthScorer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_LEVEL    = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("sentinel.main")

NEXUS_SECRET          = os.environ.get("NEXUS_SECRET", "")
DB_PATH               = os.environ.get(
    "PIPELINE_SENTINEL_DB_PATH",
    os.path.join(os.environ.get("DATA_DIR", "/Users/ahmedsadek/nexus/data"), "pipeline_sentinel.db"),
)
STALL_WINDOW_S        = int(os.environ.get("STALL_WINDOW_SECONDS", "300"))
SCORE_INTERVAL_S      = int(os.environ.get("SCORE_RECOMPUTE_INTERVAL_S", "30"))

# ---------------------------------------------------------------------------
# Shared state (set during lifespan)
# ---------------------------------------------------------------------------

_notifier:   TelegramNotifier      = None   # type: ignore[assignment]
_scorer:     HealthScorer          = None   # type: ignore[assignment]
_classifier: FailureClassifier     = None   # type: ignore[assignment]
_cached_health: Dict[str, Any]     = {}
_health_lock: threading.Lock       = threading.Lock()


def _scorer_loop() -> None:
    """Background thread: recompute health score every SCORE_INTERVAL_S seconds."""
    global _cached_health
    while True:
        try:
            result = _scorer.compute_score()
            with _health_lock:
                _cached_health = result.dict()
        except Exception as exc:
            logger.error("Scorer loop error: %s", exc)
        time.sleep(SCORE_INTERVAL_S)


def _classifier_loop() -> None:
    """Background thread: run failure detectors every 30 seconds."""
    while True:
        try:
            _classifier.detect_all()
        except Exception as exc:
            logger.error("Classifier loop error: %s", exc)
        time.sleep(30)


TRACE_TTL_S = 86400  # 24 hours — traces older than this are stale crash artifacts


def _purge_loop() -> None:
    """Background thread: purge stale traces every 30 minutes.

    GENESIS-FIX-SENTINEL-TTL-001 2026-05-01: The sentinel had no TTL on traces.
    Crash-era picks accumulated indefinitely, permanently suppressing the health
    score and triggering PIPELINE_STALL / COMPLETION_RATE_LOW / NETWORK_DEGRADED
    alerts even after the underlying failures were resolved. This loop purges
    traces older than TRACE_TTL_S (24h) and resolves stale failure events.
    """
    import sqlite3 as _sqlite3
    while True:
        try:
            cutoff = time.time() - TRACE_TTL_S
            conn = _sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM traces WHERE ts < ?", (cutoff,))
            purged = c.rowcount
            # Resolve failure events older than 4 hours that are still open
            c.execute(
                "UPDATE failure_events SET resolved=1, resolved_at=? "
                "WHERE (resolved=0 OR resolved IS NULL) AND ts < ?",
                (time.time(), time.time() - 14400),
            )
            resolved = c.rowcount
            conn.commit()
            conn.close()
            if purged or resolved:
                logger.info("Purge: removed %d stale trace rows, resolved %d old failure events",
                            purged, resolved)
        except Exception as exc:
            logger.error("Purge loop error: %s", exc)
        time.sleep(1800)  # Run every 30 minutes


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):  # type: ignore[type-arg]
    """Initialize DB, scorer, classifier, and background threads on startup."""
    global _notifier, _scorer, _classifier, _cached_health

    logger.info("Pipeline Sentinel starting up …")
    init_db(DB_PATH)

    _notifier   = TelegramNotifier()
    _scorer     = HealthScorer(DB_PATH, STALL_WINDOW_S, _notifier)
    _classifier = FailureClassifier(DB_PATH, STALL_WINDOW_S, _notifier)

    # Compute an initial score synchronously so /system-health is ready immediately
    try:
        initial = _scorer.compute_score()
        _cached_health = initial.dict()
    except Exception as exc:
        logger.warning("Initial score computation failed: %s", exc)

    # Start background threads (daemon — they die with the process)
    scorer_thread = threading.Thread(target=_scorer_loop, daemon=True, name="scorer-loop")
    classifier_thread = threading.Thread(target=_classifier_loop, daemon=True, name="classifier-loop")
    purge_thread = threading.Thread(target=_purge_loop, daemon=True, name="purge-loop")
    scorer_thread.start()
    classifier_thread.start()
    purge_thread.start()

    logger.info("Pipeline Sentinel running on port %s", os.environ.get("PORT", "8008"))
    yield
    logger.info("Pipeline Sentinel shutting down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Pipeline Sentinel", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _require_auth(x_nexus_secret: str) -> None:
    """
    Validate the X-Nexus-Secret header.

    Args:
        x_nexus_secret: Value of the X-Nexus-Secret header from the request.

    Raises:
        HTTPException 401: If the secret is missing or does not match.
    """
    if not NEXUS_SECRET:
        logger.warning("NEXUS_SECRET not configured — all auth checks will fail")
    if not x_nexus_secret or x_nexus_secret != NEXUS_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/trace", status_code=200)
async def post_trace(
    req: TraceRequest,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Record a pipeline hop. Auth required.

    ALWAYS returns 200 — the pipeline must never be blocked by a monitoring write.
    DB write failures are logged but never propagated to the caller.

    Args:
        req:             Validated TraceRequest payload.
        x_nexus_secret:  Auth header.
    """
    _require_auth(x_nexus_secret)
    # Fire-and-forget — never block the caller
    def _write() -> None:
        insert_trace(DB_PATH, req)

    t = threading.Thread(target=_write, daemon=True)
    t.start()

    logger.info("Trace received: trace_id=%s hop=%s service=%s ticker=%s",
                req.trace_id, req.hop.value, req.service.value, req.ticker)
    return JSONResponse({"status": "ok"})


@app.get("/health", status_code=200)
async def get_health() -> JSONResponse:
    """
    Liveness check. No auth required.

    Returns service name and status.
    """
    return JSONResponse({"status": "healthy", "service": "pipeline-sentinel"})


@app.get("/system-health", status_code=200)
async def get_system_health(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return the latest computed system health score and context block. Auth required.

    Returns the cached score (updated every 30s by background thread).
    If no cached score exists yet, computes one synchronously.

    Args:
        x_nexus_secret: Auth header.
    """
    _require_auth(x_nexus_secret)
    global _cached_health

    with _health_lock:
        cached = dict(_cached_health)

    if not cached:
        # No cache yet — compute synchronously
        try:
            result = _scorer.compute_score()
            cached = result.dict()
            with _health_lock:
                _cached_health = cached
        except Exception as exc:
            logger.error("Synchronous score computation failed: %s", exc)
            return JSONResponse({"error": "score not yet available", "stale": True}, status_code=503)

    return JSONResponse(cached)


@app.get("/pipeline/{trace_id}", status_code=200)
async def get_pipeline(
    trace_id: str,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return all recorded hops for a specific pick. Auth required.

    Args:
        trace_id:        UUID4 of the pick.
        x_nexus_secret:  Auth header.
    """
    _require_auth(x_nexus_secret)
    hops = get_traces_for_id(DB_PATH, trace_id)
    return JSONResponse({"trace_id": trace_id, "hops": hops, "count": len(hops)})


@app.get("/stalls", status_code=200)
async def get_stalls(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return all currently stalled picks. Auth required.

    A pick is stalled if it has not received a hop update in STALL_WINDOW_S seconds
    and has not yet reached alpaca_confirmed.

    Args:
        x_nexus_secret: Auth header.
    """
    _require_auth(x_nexus_secret)
    stalled = get_stalled_picks(DB_PATH, STALL_WINDOW_S)
    return JSONResponse({"stalled_picks": stalled, "count": len(stalled)})


@app.post("/report", status_code=200)
async def post_report(
    report: AnomalyReport,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Accept an anomaly report from Guardian Angel v3. Auth required.

    Stored to DB and factored into the next health score computation.

    Args:
        report:          Validated AnomalyReport payload.
        x_nexus_secret:  Auth header.
    """
    _require_auth(x_nexus_secret)
    insert_anomaly_report(DB_PATH, report)
    logger.info("Anomaly report received: type=%s service=%s", report.anomaly_type, report.service)
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8008"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
