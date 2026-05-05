"""
main.py — FastAPI application for nexus-integrity service (port 8011).

Commercial-grade monitoring for the Nexus trading system.
Implements Vector's full monitoring vision with all amendments accepted.

Startup sequence:
  1. Load config (KeyError if required env vars missing — fail loud)
  2. Schema version gate (V8/V10 amendment) — refuse to start on mismatch
  3. Init TRS database (WAL SQLite)
  4. Start CHRONICLE async writer
  5. Start background schedulers (flow verify, Layer 1 presence, intraday canary)
  6. Serve on port 8011

Endpoints:
  GET  /health              — Liveness (no auth)
  GET  /composite-health    — Full composite score + components (auth)
  GET  /flow-status         — Current 5-stage flow state (auth)
  GET  /canary/latest       — Last canary results (auth)
  GET  /trs                 — Current TRS score (auth)
  POST /canary/run          — Trigger canary manually (auth)
  GET  /thresholds          — Current regime-aware thresholds (auth)
  POST /thresholds          — Update threshold (auth)
"""

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# Load env before importing config
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

import config
from chronicle_writer import (
    check_chronicle_schema_version,
    start_chronicle_writer,
    stop_chronicle_writer,
    write_health_event,
    write_trs_score,
)
from composite import (
    compute_composite_score,
    score_canary_success_rate,
    score_data_freshness,
    score_error_rate,
    score_service_availability,
)
from flow_verifier import get_flow_score, run_flow_verification
from scoring_quality_probe import run_scoring_quality_probe, ScoringQualityResult
from brain_connectivity_probe import run_brain_connectivity_probe, BrainConnectivityResult
from window_completion_monitor import check_window_completion_rates, WindowCompletionResult
from restart_cluster_detector import (
    snapshot_service_uptimes, detect_restarts, record_restart,
    check_cluster, update_uptime_baselines, _previous_uptime_since,
)
from models import AlertTier, HealthEvent, TRSResult
from notifier import send_alert, send_flow_stage_escalation, send_trs_alert
from trs import get_trading_readiness_score, init_trs, write_trs
from canary import get_recent_canary_results, run_intraday_canary

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("integrity.main")

# ---------------------------------------------------------------------------
# Schema version gate (V8/V10 amendment)
# ---------------------------------------------------------------------------

def _assert_chronicle_schema() -> None:
    """Hard-stop if CHRONICLE schema version does not match required version.

    If CHRONICLE is unreachable, logs a P1 warning and continues (CHRONICLE down
    ≠ service down per T15). Schema mismatch = hard stop.
    """
    logger.info(
        "Checking CHRONICLE schema version (required: %s)",
        config.REQUIRED_CHRONICLE_SCHEMA,
    )
    version = check_chronicle_schema_version()

    if version is None:
        # CHRONICLE unreachable at startup — log warning, continue with local cache
        logger.warning(
            "CHRONICLE unreachable at startup — continuing with local cache. "
            "Schema version could not be verified (required: %s)",
            config.REQUIRED_CHRONICLE_SCHEMA,
        )
        return

    if version != config.REQUIRED_CHRONICLE_SCHEMA:
        raise SystemExit(
            f"FATAL: CHRONICLE schema version '{version}' does not match "
            f"required '{config.REQUIRED_CHRONICLE_SCHEMA}'. "
            f"Run migration before starting nexus-integrity."
        )

    logger.info("CHRONICLE schema version OK: %s", version)


# ---------------------------------------------------------------------------
# Background scheduler state
# ---------------------------------------------------------------------------

_scheduler_running: bool = False
_last_flow_results: list = []
_last_composite: Optional[TRSResult] = None
_last_presence_scores: Dict[str, bool] = {}
_state_lock = threading.Lock()


def _run_layer1_presence(interval_s: int = 60) -> None:
    """Background thread: check /health on all services every interval_s.

    Args:
        interval_s: Check interval in seconds.
    """
    import requests

    logger.info("Layer 1 presence checker started (interval=%ds)", interval_s)
    while _scheduler_running:
        healthy_count = 0
        total = len(config.ALL_SERVICE_URLS)
        presence: Dict[str, bool] = {}

        for name, url in config.ALL_SERVICE_URLS.items():
            try:
                resp = requests.get(f"{url}/health", timeout=3.0)
                ok = resp.status_code == 200
                presence[name] = ok
                if ok:
                    healthy_count += 1

                write_health_event(HealthEvent(
                    source="nexus-integrity:layer1",
                    service=name,
                    check_type="presence",
                    result="pass" if ok else "fail",
                    trs_weight=config.COMPONENT_WEIGHTS.get("service_availability", 20.0),
                ))
            except requests.RequestException as e:
                presence[name] = False
                logger.warning("Layer 1: %s unreachable: %s", name, e)

        with _state_lock:
            _last_presence_scores.update(presence)

        logger.debug(
            "Layer 1: %d/%d services healthy", healthy_count, total
        )

        time.sleep(interval_s)


def _run_composite_scheduler(interval_s: int = 300) -> None:
    """Background thread: recompute composite TRS score every interval_s.

    Args:
        interval_s: Recompute interval in seconds (default 5 min).
    """
    logger.info("Composite score scheduler started (interval=%ds)", interval_s)
    time.sleep(15)  # Brief startup delay to let Layer 1 run first

    while _scheduler_running:
        try:
            with _state_lock:
                presence = dict(_last_presence_scores)
                flow_results = list(_last_flow_results)

            healthy_count = sum(1 for v in presence.values() if v)
            total_count = max(len(presence), len(config.ALL_SERVICE_URLS))
            svc_score = score_service_availability(healthy_count, total_count)

            canary_results = get_recent_canary_results()
            canary_score = score_canary_success_rate(canary_results)

            flow_score = get_flow_score(flow_results) if flow_results else 80.0

            # Compute composite
            trs = compute_composite_score(
                service_availability=svc_score,
                canary_success_rate=canary_score,
                config_correctness=90.0,  # TODO: implement config check
                error_rate_score=90.0,    # TODO: implement error rate tracking
                pipeline_throughput=flow_score,
                data_freshness=80.0,      # TODO: implement freshness check
                session_activity=80.0,    # TODO: implement session check
            )

            # Write to TRS store
            write_trs(trs)

            # Write to CHRONICLE async
            write_trs_score(trs)

            with _state_lock:
                global _last_composite
                _last_composite = trs

            # Alert if needed
            if trs.alert_tier in (AlertTier.P0, AlertTier.P1):
                send_trs_alert(trs.score, trs.alert_tier, trs.reason)

            logger.info(
                "TRS recomputed: score=%.1f color=%s tier=%s",
                trs.score, trs.color.value, trs.alert_tier.value,
            )

        except Exception as e:  # noqa: BLE001
            logger.error("Composite scheduler error: %s", e)

        time.sleep(interval_s)


def _run_flow_verifier(interval_s: int = 900) -> None:
    """Background thread: run 5-stage flow verification every interval_s.

    Args:
        interval_s: Verification interval in seconds (default 15 min).
    """
    logger.info("Flow verifier scheduler started (interval=%ds)", interval_s)
    time.sleep(30)  # Wait for startup

    while _scheduler_running:
        try:
            results = run_flow_verification()
            with _state_lock:
                global _last_flow_results
                _last_flow_results = results

            # Alert on escalated stages
            for r in results:
                from models import StageResult
                if r.result == StageResult.ESCALATED:
                    send_flow_stage_escalation(r.stage, r.name, r.detail)

        except Exception as e:  # noqa: BLE001
            logger.error("Flow verifier error: %s", e)

        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# SOVEREIGN Addendum — S1 / S2 / S4 / S5 scheduler threads
# ---------------------------------------------------------------------------

# Shared state for restart cluster detector
_uptime_baseline: dict = {}
_uptime_baseline_lock = threading.Lock()

# Shared latest results (read by /composite-health endpoint)
_latest_scoring_quality: Optional["ScoringQualityResult"] = None
_latest_brain_probe: Optional["BrainConnectivityResult"] = None
_latest_window_completion: Optional["WindowCompletionResult"] = None


def _run_scoring_quality_probe(interval_s: int = 1800) -> None:
    """S1: Oracle scoring quality probe every 30 min."""
    global _latest_scoring_quality
    logger.info("S1 scoring quality probe started (interval=%ds)", interval_s)
    time.sleep(120)  # Wait 2 min after startup for Oracle to warm up
    while _scheduler_running:
        try:
            result = run_scoring_quality_probe()
            _latest_scoring_quality = result
            write_health_event(HealthEvent(
                source="s1_scoring_quality",
                service="oracle",
                check_type="scoring_quality",
                result="fail" if result.is_degraded else "pass",
                detail=" | ".join(result.findings) if result.findings else "ok",
                trs_weight=result.trs_score,
            ))
            if result.is_degraded:
                from notifier import send_alert
                send_alert(
                    tier="P1",
                    title="Oracle Scoring Quality DEGRADED",
                    body=(
                        f"Oracle returning fallback sentinel values on "
                        f"{result.degraded_tickers}/{result.checked_tickers} tickers.\n"
                        + "\n".join(result.findings)
                    ),
                )
        except Exception as e:
            logger.error("S1 scoring quality probe error: %s", e)
        time.sleep(interval_s)


def _run_brain_connectivity_probe(interval_s: int = 1800) -> None:
    """S5: Brain API connectivity probe every 30 min."""
    global _latest_brain_probe
    logger.info("S5 brain connectivity probe started (interval=%ds)", interval_s)
    time.sleep(60)  # Short initial delay
    while _scheduler_running:
        try:
            result = run_brain_connectivity_probe()
            _latest_brain_probe = result
            write_health_event(HealthEvent(
                source="s5_brain_connectivity",
                service="omni_brains",
                check_type="brain_connectivity",
                result="fail" if result.trs_score == 0 else (
                    "warn" if result.trs_score < 100 else "pass"
                ),
                detail=result.summary,
                latency_ms=0.0,
            ))
            if result.available_count < 3:
                from notifier import send_alert
                send_alert(
                    tier="P1" if result.available_count >= 2 else "P0",
                    title=f"Brain Connectivity Degraded: {result.available_count}/4 Available",
                    body=(
                        f"{result.summary}\n"
                        f"Failed: {result.failed}"
                    ),
                )
        except Exception as e:
            logger.error("S5 brain connectivity probe error: %s", e)
        time.sleep(interval_s)


def _run_window_completion_monitor(interval_s: int = 1200) -> None:
    """S4: Scanner window completion rate check every 20 min."""
    global _latest_window_completion
    logger.info("S4 window completion monitor started (interval=%ds)", interval_s)
    time.sleep(300)  # Wait 5 min for initial windows to populate
    while _scheduler_running:
        try:
            result = check_window_completion_rates()
            _latest_window_completion = result
            for stats in result.agent_stats:
                if stats.alert_severity:
                    write_health_event(HealthEvent(
                        source="s4_window_completion",
                        service=stats.agent,
                        check_type="window_completion_rate",
                        result="fail",
                        detail=stats.alert_message,
                        trs_weight=stats.trs_score,
                    ))
                    from notifier import send_alert
                    send_alert(
                        tier=stats.alert_severity,
                        title=f"Window Completion Rate Alert: {stats.agent}",
                        body=stats.alert_message,
                    )
        except Exception as e:
            logger.error("S4 window completion monitor error: %s", e)
        time.sleep(interval_s)


def _run_restart_cluster_detector(interval_s: int = 60) -> None:
    """S2: Service restart cluster detection every 60s."""
    global _uptime_baseline
    logger.info("S2 restart cluster detector started (interval=%ds)", interval_s)
    time.sleep(30)  # Initial baseline snapshot

    # Build initial baseline
    with _uptime_baseline_lock:
        initial = snapshot_service_uptimes()
        _uptime_baseline = update_uptime_baselines(initial)

    while _scheduler_running:
        try:
            current = snapshot_service_uptimes()

            with _uptime_baseline_lock:
                baseline = dict(_uptime_baseline)
                restarted = detect_restarts(current, baseline)

            for svc in restarted:
                record_restart(svc)
                write_health_event(HealthEvent(
                    source="s2_restart_detector",
                    service=svc,
                    check_type="service_restart",
                    result="warn",
                    detail=f"{svc} restarted (uptime_since changed)",
                ))
                # RESILIENCE_SPEC_v2 Cascade Recovery: Oracle restart → clear agent dedup
                # GENESIS-FIX-CASCADE-001 2026-05-01: When Oracle restarts its cache
                # wipes to zero. Agents have already recorded today's picks in their
                # dedup tables, so they won't resubmit — even though Oracle now serves
                # fresh context. This cascade clears the dedup so agents resubmit
                # as soon as Oracle is warm again, without any manual intervention.
                if svc == "oracle":
                    try:
                        import sqlite3 as _sq, datetime as _dt
                        _today = _dt.datetime.now().strftime("%Y-%m-%d")
                        for _agent in ["cipher", "atlas", "sage"]:
                            _db = f"/Users/ahmedsadek/nexus/data/{_agent}.db"
                            try:
                                _conn = _sq.connect(_db, timeout=5)
                                _conn.execute(
                                    "DELETE FROM picks WHERE created_at LIKE ?",
                                    (f"{_today}%",)
                                )
                                _conn.commit()
                                _conn.close()
                            except Exception:
                                pass  # Agent DB unavailable — skip silently
                        # Also purge alpha-buffer concordances
                        import requests as _req
                        _req.post(
                            f"{config.ALPHA_BUFFER_URL}/concordance/purge",
                            headers={"X-Nexus-Secret": config.NEXUS_SECRET},
                            timeout=5,
                        )
                        logger.info(
                            "CASCADE RECOVERY: Oracle restart detected — agent dedup cleared, "
                            "buffer concordances purged. Pipeline will auto-recover once Oracle warms."
                        )
                    except Exception as _ce:
                        logger.warning("Cascade recovery failed: %s", _ce)

            if restarted:
                from restart_cluster_detector import get_recent_restart_events
                cluster = check_cluster(get_recent_restart_events())
                if cluster.is_alert:
                    write_health_event(HealthEvent(
                        source="s2_restart_detector",
                        service="cluster",
                        check_type="restart_cluster",
                        result="fail",
                        detail=cluster.message,
                        trs_weight=0.0 if cluster.severity == "P0" else 50.0,
                    ))
                    from notifier import send_alert
                    send_alert(
                        tier=cluster.severity,
                        title=f"Service Restart {cluster.kind}",
                        body=cluster.message,
                    )

            # Update baseline
            with _uptime_baseline_lock:
                _uptime_baseline = update_uptime_baselines(current)

        except Exception as e:
            logger.error("S2 restart cluster detector error: %s", e)

        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Intraday Canary Scheduler
# ---------------------------------------------------------------------------

def _run_intraday_canary_scheduler(interval_s: int = 1800) -> None:
    """Background thread: run intraday canary every interval_s during market hours.

    GENESIS-FIX-CANARY-001 2026-04-30: The intraday canary was never being
    scheduled — only accessible via POST /canary/run. This left canary_success_rate
    stuck at the pre-market result (50%) all day, causing TRS=AMBER permanently.
    Fix: schedule intraday canary every 30 minutes during market hours.

    Args:
        interval_s: Run interval in seconds (default 1800 = 30 min).
    """
    from zoneinfo import ZoneInfo
    from datetime import time as dt_time
    ET = ZoneInfo("America/New_York")
    MARKET_OPEN  = dt_time(9, 30)
    MARKET_CLOSE = dt_time(16, 0)

    logger.info("Intraday canary scheduler started (interval=%ds)", interval_s)
    time.sleep(120)  # Wait 2 min after startup before first run

    while _scheduler_running:
        try:
            now_et = datetime.now(ET).time()
            if MARKET_OPEN <= now_et <= MARKET_CLOSE:
                logger.info("Running scheduled intraday canary")
                result = run_intraday_canary()
                logger.info(
                    "Intraday canary complete: passed=%s stage=%s detail=%s",
                    result.passed, result.stage_reached, result.detail[:120],
                )
            else:
                logger.debug("Intraday canary skipped — outside market hours")
        except Exception as exc:
            logger.error("Intraday canary scheduler error: %s", exc)
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup and shutdown handlers."""
    global _scheduler_running

    logger.info("nexus-integrity starting up")

    # Schema gate (V8/V10)
    _assert_chronicle_schema()

    # Init TRS
    init_trs()

    # Start CHRONICLE writer
    start_chronicle_writer()

    # Start background threads
    _scheduler_running = True
    threads = [
        threading.Thread(target=_run_layer1_presence, kwargs={"interval_s": 60}, daemon=True, name="layer1-presence"),
        threading.Thread(target=_run_composite_scheduler, kwargs={"interval_s": 300}, daemon=True, name="composite-scheduler"),
        threading.Thread(target=_run_flow_verifier, kwargs={"interval_s": config.FLOW_VERIFY_INTERVAL_S}, daemon=True, name="flow-verifier"),
        # SOVEREIGN Addendum amendments
        threading.Thread(target=_run_scoring_quality_probe, kwargs={"interval_s": 1800}, daemon=True, name="s1-scoring-quality"),
        threading.Thread(target=_run_brain_connectivity_probe, kwargs={"interval_s": 1800}, daemon=True, name="s5-brain-probe"),
        threading.Thread(target=_run_window_completion_monitor, kwargs={"interval_s": 1200}, daemon=True, name="s4-window-completion"),
        threading.Thread(target=_run_restart_cluster_detector, kwargs={"interval_s": 60}, daemon=True, name="s2-restart-cluster"),
        threading.Thread(target=_run_intraday_canary_scheduler, kwargs={"interval_s": 1800}, daemon=True, name="intraday-canary"),
    ]
    for t in threads:
        t.start()
        logger.info("Started thread: %s", t.name)

    logger.info("nexus-integrity startup complete — listening on port %d", config.PORT)
    yield

    # Shutdown
    logger.info("nexus-integrity shutting down")
    _scheduler_running = False
    stop_chronicle_writer()
    logger.info("nexus-integrity shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="nexus-integrity",
    description="Commercial-grade monitoring for the Nexus trading system",
    version="1.0.0",
    lifespan=lifespan,
)


def _require_auth(x_nexus_secret: Optional[str]) -> None:
    """Validate X-Nexus-Secret header.

    Args:
        x_nexus_secret: Value from X-Nexus-Secret header.

    Raises:
        HTTPException: 403 if secret is missing or invalid.
    """
    if not x_nexus_secret or x_nexus_secret != config.NEXUS_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> Dict[str, Any]:
    """Service liveness check (no auth required).

    Returns:
        JSON with status and service name.
    """
    return {"status": "healthy", "service": config.SERVICE_NAME, "port": config.PORT}


@app.get("/trs")
async def get_trs(
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Get current Trading Readiness Score from TRS store.

    Returns:
        TRS result with score, color, block flag, and component breakdown.
    """
    _require_auth(x_nexus_secret)
    result = get_trading_readiness_score()
    return {
        "score": result.score,
        "block": result.block,
        "color": result.color.value,
        "alert_tier": result.alert_tier.value,
        "reason": result.reason,
        "age_seconds": result.age_seconds,
        "components": result.component_scores,
    }


@app.get("/composite-health")
async def composite_health(
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Get full composite health score with component breakdown.

    Returns:
        Composite score, color, components, and TRS.
    """
    _require_auth(x_nexus_secret)
    with _state_lock:
        composite = _last_composite
        presence = dict(_last_presence_scores)

    trs = get_trading_readiness_score()

    return {
        "trs": {
            "score": trs.score,
            "color": trs.color.value,
            "block": trs.block,
            "tier": trs.alert_tier.value,
            "reason": trs.reason,
        },
        "composite": {
            "score": composite.score if composite else None,
            "color": composite.color.value if composite else "UNKNOWN",
        },
        "service_presence": presence,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/composite-health/trend")
async def composite_health_trend(
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """C5: Return last 8 composite score snapshots (2 hours) with delta annotations.

    Distinguishes stable-AMBER from deteriorating-toward-RED.
    No new data collection — reads from CHRONICLE monitoring_state snapshots.
    """
    _require_auth(x_nexus_secret)
    try:
        import sqlite3 as _sq
        db = "/Users/ahmedsadek/nexus/data/chronicle.db"
        conn = _sq.connect(db, timeout=3)
        conn.row_factory = _sq.Row
        cur = conn.cursor()
        # Read last 8 composite score snapshots from monitoring_state
        cur.execute(
            "SELECT * FROM monitoring_state ORDER BY rowid DESC LIMIT 8"
            if _table_exists(conn, 'monitoring_state')
            else "SELECT NULL WHERE 1=0"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        # Annotate with deltas
        annotated = []
        for i, row in enumerate(rows):
            entry = dict(row)
            if i < len(rows) - 1:
                prev_score = rows[i + 1].get("composite_score", rows[i + 1].get("score"))
                curr_score = row.get("composite_score", row.get("score"))
                if prev_score is not None and curr_score is not None:
                    entry["delta"] = round(float(curr_score) - float(prev_score), 1)
                else:
                    entry["delta"] = None
            else:
                entry["delta"] = None
            annotated.append(entry)

        # Trend summary
        scores = [r.get("composite_score", r.get("score")) for r in rows
                  if r.get("composite_score") is not None or r.get("score") is not None]
        trend = "STABLE"
        if len(scores) >= 3:
            recent_avg = sum(float(s) for s in scores[:3]) / 3
            older_avg  = sum(float(s) for s in scores[-3:]) / 3
            diff = recent_avg - older_avg
            if diff <= -5:
                trend = "DETERIORATING"
            elif diff >= 5:
                trend = "IMPROVING"

        return {
            "snapshots": annotated,
            "trend": trend,
            "window_hours": 2,
            "snapshot_count": len(annotated),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": str(e), "snapshots": [], "trend": "UNKNOWN"}


def _table_exists(conn, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,))
    return cur.fetchone() is not None


@app.get("/flow-status")
async def flow_status(
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Get current 5-stage pipeline flow verification status.

    Returns:
        Stage results from last flow verification run.
    """
    _require_auth(x_nexus_secret)
    with _state_lock:
        results = list(_last_flow_results)

    return {
        "stages": [
            {
                "stage": r.stage,
                "name": r.name,
                "result": r.result.value,
                "detail": r.detail,
                "fix_attempted": r.fix_attempted,
                "fix_succeeded": r.fix_succeeded,
                "latency_ms": r.latency_ms,
            }
            for r in results
        ],
        "flow_score": get_flow_score(results),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/canary/latest")
async def canary_latest(
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Get last canary results.

    Returns:
        Recent canary pass/fail history.
    """
    _require_auth(x_nexus_secret)
    results = get_recent_canary_results()
    return {
        "recent_results": results,
        "pass_rate": sum(results) / len(results) * 100 if results else None,
        "last_5": results[-5:],
    }


@app.post("/canary/run")
async def canary_run(
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Trigger an intraday canary run manually.

    Returns:
        Canary result.
    """
    _require_auth(x_nexus_secret)
    result = run_intraday_canary()
    return {
        "canary_type": result.canary_type,
        "passed": result.passed,
        "stage_reached": result.stage_reached,
        "sla_breach": result.sla_breach,
        "duration_ms": result.duration_ms,
        "detail": result.detail,
    }


@app.get("/thresholds")
async def get_thresholds(
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Get current regime-aware thresholds (loaded from CHRONICLE).

    Returns:
        Current threshold values.
    """
    _require_auth(x_nexus_secret)
    from composite import _get_thresholds
    return {"thresholds": _get_thresholds()}


@app.post("/thresholds")
async def update_threshold(
    request: Request,
    x_nexus_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Update a threshold value (Ahmed/SOVEREIGN only).

    Body: {"key": "GREEN_MIN", "value": 90.0}

    Returns:
        Updated threshold confirmation.
    """
    _require_auth(x_nexus_secret)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")

    if not key or value is None:
        raise HTTPException(status_code=400, detail="key and value required")

    from composite import _threshold_cache, _threshold_cache_time
    _threshold_cache[key] = float(value)

    logger.info("Threshold updated: %s = %s", key, value)
    return {"updated": key, "value": value}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        reload=False,
    )
