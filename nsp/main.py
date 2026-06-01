"""
main.py — Nexus Sentinel Prime (NSP) FastAPI application (port 8010).

Startup sequence:
  1. Load config (KeyError if NEXUS_SECRET or TELEGRAM_BOT_TOKEN missing — fail loud)
  2. Init WAL SQLite database (schema creation)
  3. Start single DB writer thread
  4. Start purge job scheduler
  5. Ensure persistent state table + calibration mode
  6. Start all telemetry collector threads (11 services)
  7. Serve on NSP_PORT (default 8010)

Endpoints:
  GET  /health              — DMS watchdog target (no auth required)
  GET  /status              — Full status summary (auth required)
  POST /admin/calibration   — Toggle calibration mode (auth required)
  POST /admin/freeze        — Lift FREEZE mode (auth required)

nexus-integrity is available as an imported Python library via sys.path.insert.
Phase 1 sets up the import path only — no NI functions are called yet.
"""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Import NSP local modules first (before any sys.path manipulation)
# ---------------------------------------------------------------------------
import config
import db
import state
import telemetry

# Load env before importing config (env may come from launchd plist or shell)
_log_dir = os.environ.get("NSP_LOG_DIR", "/Users/ahmedsadek/nexus/logs/nsp")
os.makedirs(_log_dir, exist_ok=True)

# ---------------------------------------------------------------------------
# nexus-integrity import path (imported as library, not HTTP peer)
# ---------------------------------------------------------------------------
import importlib.util as _wd_util
_wd_spec = _wd_util.spec_from_file_location("nns_watchdog", "/Users/ahmedsadek/nexus/shared/watchdog.py")
_wd_mod = _wd_util.module_from_spec(_wd_spec)
_wd_spec.loader.exec_module(_wd_mod)
Watchdog = _wd_mod.Watchdog

# ---------------------------------------------------------------------------
# Logging — configure before any module-level loggers fire
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("nsp.main")

_start_time: float = time.time()


# ---------------------------------------------------------------------------
# Lifespan — startup + shutdown
# ---------------------------------------------------------------------------

_nns_watchdog = Watchdog("nsp")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: ordered startup and graceful shutdown.

    Startup order matters:
      init_db → start_writer_thread → start_purge_job → ensure_state_table
      → start_all_collectors

    Shutdown order (reverse):
      stop_all_collectors → stop_writer_thread
    """
    logger.info("NSP startup — service=%s port=%d", config.SERVICE_NAME, config.NSP_PORT)

    # 1. Initialise DB schema (direct write — writer thread not started yet)
    try:
        db.init_db()
    except Exception as exc:
        logger.critical("DB init failed — NSP cannot start: %s", exc)
        raise

    # 2. Start single writer thread
    db.start_writer_thread()

    # 3. Start purge job
    db.start_purge_job()

    # 4. Ensure state table + calibration default
    try:
        state.ensure_state_table()
    except Exception as exc:
        logger.critical("State table check failed: %s", exc)
        raise

    # 5. Log calibration mode on startup
    cal_mode = state.get_calibration_mode()
    freeze = state.get_freeze_mode()
    logger.info(
        "NSP state loaded — calibration_mode=%s freeze_mode=%s clean_days=%d",
        cal_mode,
        freeze,
        state.get_calibration_clean_days(),
    )

    # 6. Start all telemetry collectors
    telemetry.start_all_collectors()

    logger.info(
        "NSP startup complete — %d collector threads running on port %d",
        len(telemetry.get_collector_service_names()),
        config.NSP_PORT,
    )
    _nns_watchdog.start()

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    logger.info("NSP shutdown initiated")
    telemetry.stop_all_collectors(timeout_s=5.0)
    db.stop_writer_thread(timeout_s=5.0)
    logger.info("NSP shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Nexus Sentinel Prime",
    version="1.0.0-phase1",
    description="Unified sensing, prediction, and healing for the Nexus trading system.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _require_auth(x_nsp_secret: Optional[str]) -> None:
    """Validate the NSP secret header.

    Args:
        x_nsp_secret: Value of the X-NSP-Secret request header.

    Raises:
        HTTPException 401: If the secret is missing or incorrect.
    """
    if x_nsp_secret != config.NSP_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-NSP-Secret")


# ---------------------------------------------------------------------------
# Self-preservation gate helper
# ---------------------------------------------------------------------------

def _check_self_preservation() -> bool:
    """Return True if NSP's own telemetry DB write lag is within tolerance.

    If write lag > DB_WRITE_LAG_THRESHOLD_S (2s), all interventions must be
    stood down.  This check is called before any intervention decision.

    Returns:
        True if healthy (interventions may proceed),
        False if DB is lagging (stand down — P1 alert only).
    """
    lag = db.get_last_write_lag()
    if lag > config.DB_WRITE_LAG_THRESHOLD_S:
        logger.warning(
            "SELF-PRESERVATION GATE: DB write lag %.3fs > %.1fs threshold — "
            "standing down all interventions",
            lag,
            config.DB_WRITE_LAG_THRESHOLD_S,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> Dict[str, Any]:
    """NSP liveness endpoint — watched by DMS every 30 seconds.

    Returns 200 as long as the process is alive and the DB writer is running.
    No auth required (DMS has no secret; this is a pure liveness check).

    Returns:
        JSON with status, uptime, calibration_mode, db_write_lag_s, timestamp.
    """
    write_lag = db.get_last_write_lag()
    return {
        "status": "ok",
        "service": config.SERVICE_NAME,
        "version": "1.0.0-phase1",
        "uptime_s": round(time.time() - _start_time, 1),
        "calibration_mode": state.get_calibration_mode(),
        "freeze_mode": state.get_freeze_mode(),
        "db_write_lag_s": round(write_lag, 4),
        "self_preservation_gate": _check_self_preservation(),
        "collector_count": len(telemetry.get_collector_service_names()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/status")
async def status(
    x_nsp_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Full NSP status summary — auth required.

    Returns calibration state, freeze state, recent intervention summary,
    and telemetry collector health.

    Args:
        x_nsp_secret: NSP secret from X-NSP-Secret header.

    Returns:
        JSON status summary.
    """
    _require_auth(x_nsp_secret)

    recent = state.get_recent_interventions(limit=5)
    hourly_total = state.get_total_intervention_count_last_hour()
    cal_clean_days = state.get_calibration_clean_days()

    return {
        "service": config.SERVICE_NAME,
        "uptime_s": round(time.time() - _start_time, 1),
        "calibration_mode": state.get_calibration_mode(),
        "calibration_clean_days": cal_clean_days,
        "freeze_mode": state.get_freeze_mode(),
        "db_write_lag_s": round(db.get_last_write_lag(), 4),
        "self_preservation_gate_ok": _check_self_preservation(),
        "interventions_last_hour": hourly_total,
        "recent_interventions": recent,
        "collectors": telemetry.get_collector_service_names(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/admin/calibration")
async def set_calibration(
    body: Dict[str, Any],
    x_nsp_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Toggle calibration mode — auth required.

    Body: {"enabled": true|false}

    Args:
        body: Request body with 'enabled' boolean.
        x_nsp_secret: NSP secret from X-NSP-Secret header.

    Returns:
        JSON confirming new calibration mode state.

    Raises:
        HTTPException 400: If 'enabled' field is missing or not boolean.
    """
    _require_auth(x_nsp_secret)

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=400,
            detail="Request body must contain 'enabled' (bool)",
        )

    state.set_calibration_mode(enabled)
    logger.info("Calibration mode set via API: %s", enabled)

    return {
        "ok": True,
        "calibration_mode": enabled,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/admin/freeze/lift")
async def lift_freeze(
    body: Dict[str, Any],
    x_nsp_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Lift FREEZE mode — auth required (SOVEREIGN or Ahmed only).

    Body: {"lifted_by": "SOVEREIGN"|"AHMED"}

    Args:
        body: Request body with 'lifted_by' string.
        x_nsp_secret: NSP secret from X-NSP-Secret header.

    Returns:
        JSON confirming FREEZE has been lifted.

    Raises:
        HTTPException 400: If 'lifted_by' is missing.
    """
    _require_auth(x_nsp_secret)

    lifted_by = body.get("lifted_by", "")
    if not lifted_by:
        raise HTTPException(
            status_code=400,
            detail="Request body must contain 'lifted_by'",
        )

    state.set_freeze_mode(enabled=False, lifted_by=lifted_by)
    logger.warning("FREEZE mode lifted via API by: %s", lifted_by)

    return {
        "ok": True,
        "freeze_mode": False,
        "lifted_by": lifted_by,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config.NSP_PORT,
        log_level=config.LOG_LEVEL.lower(),
        access_log=False,
    )
