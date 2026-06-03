"""
main.py — PROBE QA Council Agent

FastAPI service — security + logic vulnerability analysis.
Port: 9011 | Brain: DeepSeek V3

Endpoints:
  POST /review                → 202 Accepted, async analysis starts
  GET  /review/{cycle_id}     → review status from local DB
  GET  /health                → service health (no auth)
  GET  /sovereign/status      → SOVEREIGN fleet_status endpoint
  POST /sovereign/directive   → SOVEREIGN push endpoint
"""

import logging
import os
import secrets as _secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.sovereign_comms import EscalationLevel, get_instructions, report

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from analyzer import AGENT_NAME, BRAIN_MODEL, run_analysis
from chronicle_client import (
    create_review_entry,
    mark_failed,
    start_flush_thread,
    stop_flush_thread,
    write_findings,
)
from config import Settings, load_settings
from database import (
    complete_review,
    fail_review,
    get_review,
    get_today_stats,
    init_db,
    record_brain_failure,
    upsert_review,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("edge.main")

# ── Application State ─────────────────────────────────────────────────────────
_settings: Optional[Settings] = None
_sovereign_halted: bool = False
_brain_failures_today: int = 0

REVIEW_TIMEOUT_S = 300  # 5 minutes

# ── SOVEREIGN Directive Dispatch ──────────────────────────────────────────────
def _dispatch_sovereign_instruction(instr: dict) -> None:
    global _sovereign_halted

    raw = instr.get("message", "").strip()
    directive = raw.split(":", 1)[0].strip().upper() if ":" in raw else raw.upper()

    logger.info("SOVEREIGN directive received: %s", directive)

    if directive == "HALT":
        _sovereign_halted = True
        report(AGENT_NAME, "ack", {"directive": "HALT", "halted": True})
    elif directive == "RESUME":
        _sovereign_halted = False
        report(AGENT_NAME, "ack", {"directive": "RESUME", "halted": False})
    elif directive == "STATUS":
        stats = get_today_stats(_settings.db_path) if _settings else {}
        report(AGENT_NAME, "status", {"directive": "STATUS", "halted": _sovereign_halted, **stats})
    elif directive == "PING":
        report(AGENT_NAME, "ack", {"directive": "PING", "status": "alive"})
    else:
        logger.warning("SOVEREIGN: unrecognized directive '%s'", directive[:100])
        report(AGENT_NAME, "ack", {"directive": directive[:100], "status": "unrecognized"})


# ── SOVEREIGN Comms Loop ──────────────────────────────────────────────────────
_comms_stop = threading.Event()
SOVEREIGN_POLL_INTERVAL_S = 30
SOVEREIGN_HEARTBEAT_INTERVAL_S = 300


def _sovereign_comms_loop() -> None:
    last_heartbeat: float = 0.0
    logger.info("EDGE: SOVEREIGN comms loop started")
    while not _comms_stop.is_set():
        now = time.monotonic()
        try:
            instructions = get_instructions(AGENT_NAME.lower())
            for instr in instructions:
                _dispatch_sovereign_instruction(instr)
            if now - last_heartbeat >= SOVEREIGN_HEARTBEAT_INTERVAL_S:
                stats = get_today_stats(_settings.db_path) if _settings else {}
                report(AGENT_NAME, "heartbeat", {
                    "halted": _sovereign_halted,
                    "brain_failures_today": _brain_failures_today,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    **stats,
                })
                last_heartbeat = now
        except Exception as e:
            logger.warning("EDGE: comms loop error: %s", e)
        _comms_stop.wait(SOVEREIGN_POLL_INTERVAL_S)
    logger.info("EDGE: SOVEREIGN comms loop stopped")


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings
    _settings = load_settings()
    init_db(_settings.db_path)
    start_flush_thread(_settings.chronicle_db_path, _settings.db_path)
    logger.info("EDGE agent started on port %d", _settings.port)
    report(AGENT_NAME, "status", {"event": "started", "port": _settings.port}, EscalationLevel.INFO)

    _comms_stop.clear()
    threading.Thread(target=_sovereign_comms_loop, daemon=True, name="edge-sovereign-comms").start()

    yield

    _comms_stop.set()
    stop_flush_thread()
    report(AGENT_NAME, "status", {"event": "stopped"}, EscalationLevel.INFO)
    logger.info("EDGE agent shutting down")


app = FastAPI(title="EDGE QA Agent", version="1.0.0", lifespan=lifespan)


# ── Request / Response Models ─────────────────────────────────────────────────
class CodeFile(BaseModel):
    path: str
    content: str


class ReviewRequest(BaseModel):
    service_name: str
    service_version: str
    review_cycle_id: str
    code_files: list[CodeFile]
    spec_content: str
    context: str = ""

    model_config = {"extra": "ignore"}


class SovereignDirective(BaseModel):
    directive: str
    data: dict = {}
    from_agent: str = "sovereign"


# ── Auth ──────────────────────────────────────────────────────────────────────
def _check_auth(provided: Optional[str]) -> None:
    if not provided or not _secrets.compare_digest(provided, _settings.nexus_secret):
        logger.warning("EDGE: unauthorized request")
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Review Background Worker ──────────────────────────────────────────────────
def _run_review(
    review_cycle_id: str,
    service_name: str,
    service_version: str,
    code_files: list,
    spec_content: str,
    context: str,
    chronicle_id: int,
) -> None:
    global _brain_failures_today

    if _sovereign_halted:
        logger.warning("EDGE: SOVEREIGN HALT active — skipping review %s", review_cycle_id)
        mark_failed(_settings.chronicle_db_path, chronicle_id, "SOVEREIGN_HALT")
        fail_review(_settings.db_path, review_cycle_id)
        return

    start = time.monotonic()
    result = run_analysis(
        _settings.openai_api_key,
        service_name,
        service_version,
        code_files,
        spec_content,
        context,
    )

    elapsed = time.monotonic() - start

    if elapsed > REVIEW_TIMEOUT_S:
        logger.error("EDGE: review %s TIMEOUT after %.1fs", review_cycle_id, elapsed)
        mark_failed(_settings.chronicle_db_path, chronicle_id, "TIMEOUT")
        fail_review(_settings.db_path, review_cycle_id)
        report(AGENT_NAME, "alert", {
            "event": "review_timeout", "review_cycle_id": review_cycle_id,
            "elapsed_s": elapsed,
        }, EscalationLevel.CRITICAL)
        return

    if result is None:
        _brain_failures_today += 1
        record_brain_failure(_settings.db_path)
        mark_failed(_settings.chronicle_db_path, chronicle_id, "BRAIN_API_FAILURE")
        fail_review(_settings.db_path, review_cycle_id)
        report(AGENT_NAME, "alert", {
            "event": "brain_failure",
            "review_cycle_id": review_cycle_id,
            "failures_today": _brain_failures_today,
        }, EscalationLevel.CRITICAL)
        logger.error("EDGE: brain API failed all retries for %s", review_cycle_id)
        return

    write_findings(
        _settings.chronicle_db_path,
        _settings.db_path,
        chronicle_id,
        result["overall_assessment"],
        result["p0_count"],
        result["p1_count"],
        result["p2_count"],
        result["p3_count"],
        result["findings"],
        result["confidence_level"],
        review_cycle_id,
    )

    complete_review(
        _settings.db_path,
        review_cycle_id,
        result["overall_assessment"],
        result["p0_count"],
        result["p1_count"],
        result["p2_count"],
        result["p3_count"],
        result["confidence_level"],
    )

    report(AGENT_NAME, "review_complete", {
        "review_cycle_id": review_cycle_id,
        "service_name": service_name,
        "overall_assessment": result["overall_assessment"],
        "p0_count": result["p0_count"],
        "p1_count": result["p1_count"],
        "chronicle_id": chronicle_id,
    }, EscalationLevel.INFO)

    logger.info(
        "EDGE: review complete — cycle=%s service=%s assessment=%s P0=%d P1=%d",
        review_cycle_id, service_name, result["overall_assessment"],
        result["p0_count"], result["p1_count"],
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.post("/review", status_code=202)
async def start_review(
    body: ReviewRequest,
    background_tasks: BackgroundTasks,
    x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret"),
) -> JSONResponse:
    """Accept a review request and launch async analysis."""
    _check_auth(x_nexus_secret)

    chronicle_id = create_review_entry(
        _settings.chronicle_db_path,
        _settings.db_path,
        body.service_name,
        body.service_version,
        body.review_cycle_id,
    )

    upsert_review(
        _settings.db_path,
        body.review_cycle_id,
        body.service_name,
        body.service_version,
        chronicle_id,
    )

    code_files = [f.model_dump() for f in body.code_files]

    background_tasks.add_task(
        _run_review,
        body.review_cycle_id,
        body.service_name,
        body.service_version,
        code_files,
        body.spec_content,
        body.context,
        chronicle_id,
    )

    logger.info(
        "EDGE: review started — cycle=%s service=%s chronicle_id=%d",
        body.review_cycle_id, body.service_name, chronicle_id,
    )

    return JSONResponse({
        "status": "review_started",
        "review_id": str(chronicle_id),
        "agent": AGENT_NAME,
        "service_name": body.service_name,
        "review_cycle_id": body.review_cycle_id,
        "estimated_seconds": 60,
    }, status_code=202)


@app.get("/review/{review_cycle_id}")
async def get_review_status(
    review_cycle_id: str,
    x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret"),
) -> JSONResponse:
    """Return current review status from local DB."""
    _check_auth(x_nexus_secret)

    review = get_review(_settings.db_path, review_cycle_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    return JSONResponse({
        "review_cycle_id": review_cycle_id,
        "status": review.get("status", "UNKNOWN"),
        "agent": AGENT_NAME,
        "overall_assessment": review.get("overall_assessment"),
        "p0_count": review.get("p0_count", 0),
        "p1_count": review.get("p1_count", 0),
        "p2_count": review.get("p2_count", 0),
        "p3_count": review.get("p3_count", 0),
        "confidence_level": review.get("confidence_level"),
        "chronicle_id": review.get("chronicle_id"),
    })


@app.get("/health")
async def health() -> JSONResponse:
    """Service health check — no auth required."""
    brain_ok = True
    chronicle_ok = True

    try:
        import sqlite3 as _sq
        _sq.connect(_settings.chronicle_db_path if _settings else "/dev/null", timeout=1).close()
    except Exception:
        chronicle_ok = False

    env_ok = bool(
        os.getenv("NEXUS_SECRET") and
        os.getenv("OPENAI_API_KEY")
    )

    overall = "healthy" if (brain_ok and chronicle_ok and env_ok) else "degraded"

    return JSONResponse({
        "status": overall,
        "agent": AGENT_NAME,
        "checks": {
            "brain_api": {"ok": brain_ok},
            "chronicle": {"ok": chronicle_ok},
            "required_env_vars": {"ok": env_ok},
        },
    })


@app.get("/sovereign/status")
async def sovereign_status(
    x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret"),
) -> JSONResponse:
    """SOVEREIGN fleet_status endpoint."""
    _check_auth(x_nexus_secret)
    stats = get_today_stats(_settings.db_path) if _settings else {}
    return JSONResponse({
        "agent": AGENT_NAME,
        "reviews_completed_today": stats.get("reviews_completed", 0),
        "last_review_at": stats.get("last_review_at"),
        "brain_failures_today": _brain_failures_today,
        "halted": _sovereign_halted,
    })


@app.post("/sovereign/directive")
async def sovereign_directive(
    body: SovereignDirective,
    x_nexus_secret: Optional[str] = Header(None, alias="X-Nexus-Secret"),
) -> JSONResponse:
    """SOVEREIGN push endpoint — executes directive immediately."""
    _check_auth(x_nexus_secret)
    global _sovereign_halted

    d = body.directive.strip().upper()
    logger.info("EDGE: SOVEREIGN direct push: %s from=%s", d, body.from_agent)

    if d == "PING":
        report(AGENT_NAME, "ack", {"directive": "PING", "status": "alive"})
        return JSONResponse({"ok": True, "directive": "PING", "status": "alive"})

    elif d == "HALT":
        _sovereign_halted = True
        report(AGENT_NAME, "ack", {"directive": "HALT", "halted": True})
        return JSONResponse({"ok": True, "directive": "HALT", "halted": True})

    elif d == "RESUME":
        _sovereign_halted = False
        report(AGENT_NAME, "ack", {"directive": "RESUME", "halted": False})
        return JSONResponse({"ok": True, "directive": "RESUME", "halted": False})

    elif d == "STATUS":
        stats = get_today_stats(_settings.db_path) if _settings else {}
        payload = {
            "agent": AGENT_NAME, "halted": _sovereign_halted,
            "brain_failures_today": _brain_failures_today, **stats,
        }
        report(AGENT_NAME, "status", payload)
        return JSONResponse({"ok": True, **payload})

    else:
        report(AGENT_NAME, "ack", {"directive": d, "status": "unrecognized"})
        return JSONResponse({"ok": False, "error": f"unrecognized directive: {d}"}, status_code=400)


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    s = load_settings()
    uvicorn.run("main:app", host="0.0.0.0", port=s.port, log_level="info")
