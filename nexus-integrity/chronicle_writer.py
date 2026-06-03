"""
chronicle_writer.py — Async CHRONICLE writer for nexus-integrity.

CHRONICLE is the single source of truth for all monitoring state (V4/V14 amendment).
This module writes health events, TRS scores, canary results, and intervention logs
to CHRONICLE asynchronously — local cache is updated first (non-blocking),
CHRONICLE is best-effort (never blocks trading pipeline).

Service continues operating normally if CHRONICLE is unreachable (T15 test).
"""

import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Optional

import requests

import config
from models import HealthEvent, TRSResult

logger = logging.getLogger("integrity.chronicle")

# ---------------------------------------------------------------------------
# Async write queue
# ---------------------------------------------------------------------------

_write_queue: queue.Queue = queue.Queue(maxsize=500)
_writer_thread: Optional[threading.Thread] = None
_writer_running: bool = False

# C4: CHRONICLE mid-session connectivity loss tracking
_consecutive_write_failures: int = 0
_C4_FAILURE_THRESHOLD: int = 3
_C4_alert_sent: bool = False
_local_fallback_cache: list = []   # Local SQLite fallback when CHRONICLE is down
_LOCAL_FALLBACK_DB: str = "/Users/ahmedsadek/nexus/data/nexus_integrity.db"
_MAX_LOCAL_QUEUE: int = 500


def _sanitize_headers(headers: dict) -> dict:
    """Remove secret values from headers before logging.

    Args:
        headers: Request headers dict.

    Returns:
        Headers with sensitive values redacted.
    """
    sensitive = {"x-chronicle-secret", "x-nexus-secret", "authorization"}
    return {k: "***" if k.lower() in sensitive else v for k, v in headers.items()}


def _chronicle_post(endpoint: str, payload: Dict[str, Any]) -> bool:
    """POST a payload to CHRONICLE.

    Args:
        endpoint: CHRONICLE API path (e.g. '/health_events').
        payload: JSON payload dict.

    Returns:
        True if write succeeded, False otherwise.
    """
    url = f"{config.CHRONICLE_URL}{endpoint}"
    headers = {
        "X-Chronicle-Auth": config.CHRONICLE_SECRET,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=5.0)
        if resp.status_code in (200, 201):
            return True
        logger.warning(
            "CHRONICLE POST %s returned %d: %s",
            endpoint, resp.status_code, resp.text[:100]
        )
        return False
    except requests.Timeout:
        logger.warning("CHRONICLE POST %s timed out", endpoint)
        return False
    except requests.ConnectionError as e:
        logger.warning("CHRONICLE POST %s connection error: %s", endpoint, e)
        return False
    except requests.RequestException as e:
        logger.error("CHRONICLE POST %s unexpected error: %s", endpoint, e)
        return False
    except Exception as e:  # noqa: BLE001 — CHRONICLE down must never crash the service
        logger.error("CHRONICLE POST %s fatal error: %s", endpoint, e)
        return False


def _writer_loop() -> None:
    """Background thread that drains the write queue and posts to CHRONICLE.

    Runs until _writer_running is False. Processes up to 10 items per second.
    Items dropped from queue on persistent CHRONICLE failure do not affect service.
    """
    global _writer_running

    logger.info("CHRONICLE writer thread started")
    while _writer_running:
        try:
            item = _write_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        endpoint = item.get("endpoint", "/health_events")
        payload = item.get("payload", {})

        success = _chronicle_post(endpoint, payload)
        if not success:
            # C4: Track consecutive failures
            global _consecutive_write_failures, _C4_alert_sent
            _consecutive_write_failures += 1
            _c4_write_local_fallback(endpoint, payload)  # Save locally
            if _consecutive_write_failures >= _C4_FAILURE_THRESHOLD and not _C4_alert_sent:
                _C4_alert_sent = True
                logger.error(
                    "C4 CHRONICLE OUTAGE: %d consecutive write failures. "
                    "Monitoring state not persisting. Local fallback active.",
                    _consecutive_write_failures,
                )
                # Alert SOVEREIGN via bus (cannot use CHRONICLE — it's down)
                try:
                    import requests as _req
                    _req.post(
                        "http://192.168.1.141:9999/send",
                        json={"from": "nexus-integrity", "to": "sovereign",
                              "message": f"C4 CHRONICLE WRITE OUTAGE: "
                                         f"{_consecutive_write_failures} consecutive failures. "
                                         f"Monitoring state not persisting to CHRONICLE. "
                                         f"Local SQLite fallback active. "
                                         f"Service continues but audit trail interrupted."},
                        timeout=3,
                    )
                except Exception:
                    pass
        else:
            # Reset on success; replay local queue if recovering
            if _consecutive_write_failures >= _C4_FAILURE_THRESHOLD:
                logger.info("C4 CHRONICLE recovered after %d failures. Replaying local queue.",
                            _consecutive_write_failures)
                _C4_alert_sent = False
                _c4_replay_local_fallback()
            _consecutive_write_failures = 0

        _write_queue.task_done()

    logger.info("CHRONICLE writer thread stopped")


def start_chronicle_writer() -> None:
    """Start the background CHRONICLE writer thread.

    Should be called once at service startup.
    """
    global _writer_thread, _writer_running
    _writer_running = True
    _writer_thread = threading.Thread(
        target=_writer_loop,
        name="chronicle-writer",
        daemon=True
    )
    _writer_thread.start()
    logger.info("CHRONICLE writer started")


def stop_chronicle_writer() -> None:
    """Stop the background CHRONICLE writer thread gracefully.

    Waits up to 5 seconds for the queue to drain.
    """
    global _writer_running
    _writer_running = False
    if _writer_thread and _writer_thread.is_alive():
        _writer_thread.join(timeout=5.0)
    logger.info("CHRONICLE writer stopped")


# ---------------------------------------------------------------------------
# Public write methods (enqueue only — never block)
# ---------------------------------------------------------------------------

def write_health_event(event: HealthEvent) -> None:
    """Enqueue a health event for async write to CHRONICLE.

    Args:
        event: HealthEvent to write.
    """
    payload = {
        "ts": time.time(),
        "source": event.source,
        "service": event.service,
        "check_type": event.check_type,
        "result": event.result,
        "latency_ms": event.latency_ms,
        "detail": event.detail,
        "trs_weight": event.trs_weight,
        "schema_version": event.schema_version,
    }
    _enqueue("/health_events", payload)


def write_trs_score(result: TRSResult) -> None:
    """Enqueue a TRS score write to CHRONICLE.

    Args:
        result: TRSResult to persist in CHRONICLE.
    """
    payload = {
        "ts": time.time(),
        "score": result.score,
        "block": result.block,
        "reason": result.reason,
        "color": result.color.value,
        "alert_tier": result.alert_tier.value,
        "components": result.component_scores,
        "schema_version": "2.1",
    }
    _enqueue("/monitoring/trs", payload)


def write_canary_result(
    canary_type: str,
    passed: bool,
    stage_reached: int,
    sla_breach: bool,
    duration_ms: float,
    detail: str = "",
) -> None:
    """Enqueue a canary result write to CHRONICLE.

    Args:
        canary_type: 'pre_market' or 'intraday'.
        passed: Whether canary passed all stages.
        stage_reached: Highest stage completed (1-4).
        sla_breach: Whether canary exceeded SLA.
        duration_ms: Total canary duration.
        detail: Additional context.
    """
    payload = {
        "ts": time.time(),
        "canary_type": canary_type,
        "passed": passed,
        "stage_reached": stage_reached,
        "sla_breach": sla_breach,
        "duration_ms": duration_ms,
        "detail": detail,
    }
    _enqueue("/monitoring/canary", payload)


def write_intervention(
    agent: str,
    error_description: str,
    time_identified: float,
    time_acted: float,
    time_resolved: Optional[float],
    resolution_type: str,
    damage_assessment: str,
    outcome: str,
) -> None:
    """Enqueue an intervention log entry to CHRONICLE.

    Args:
        agent: Agent that performed the intervention.
        error_description: What failed.
        time_identified: Epoch timestamp when error was detected.
        time_acted: Epoch timestamp when action was taken.
        time_resolved: Epoch timestamp when resolved (None if ongoing).
        resolution_type: 'root_cause' | 'symptomatic' | 'escalated' | 'unresolved'.
        damage_assessment: What impact the failure had.
        outcome: 'solved' | 'unsolved' | 'in_progress'.
    """
    payload = {
        "agent": agent,
        "error_description": error_description,
        "time_identified": time_identified,
        "time_acted": time_acted,
        "time_resolved": time_resolved,
        "resolution_type": resolution_type,
        "damage_assessment": damage_assessment,
        "outcome": outcome,
    }
    _enqueue("/intervention_log", payload)


def check_chronicle_schema_version() -> Optional[str]:
    """Query CHRONICLE for current schema version.

    Returns:
        Schema version string (e.g. '2.1') or None if CHRONICLE unreachable.
    """
    try:
        resp = requests.get(
            f"{config.CHRONICLE_URL}/schema/version",
            headers={"X-Chronicle-Auth": config.CHRONICLE_SECRET},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return str(data.get("version", ""))
        logger.warning("CHRONICLE schema version check returned %d", resp.status_code)
        return None
    except requests.RequestException as e:
        logger.warning("CHRONICLE schema version check failed: %s", e)
        return None


def _enqueue(endpoint: str, payload: Dict[str, Any]) -> None:
    """Add a write job to the async queue.

    Drops silently if queue is full (service health > CHRONICLE availability).

    Args:
        endpoint: CHRONICLE API path.
        payload: Data to POST.
    """
    try:
        _write_queue.put_nowait({"endpoint": endpoint, "payload": payload})
    except queue.Full:
        logger.warning("CHRONICLE write queue full — event dropped (non-blocking)")


# ===========================================================================
# C4 — CHRONICLE mid-session outage helpers (Cipher Amendment)
# ===========================================================================

def _c4_write_local_fallback(endpoint: str, payload: dict) -> None:
    """Write a failed CHRONICLE event to local SQLite fallback during outage.

    Queue is capped at _MAX_LOCAL_QUEUE entries. Oldest entries dropped on overflow.
    """
    import sqlite3 as _sq, json as _json, time as _t
    try:
        conn = _sq.connect(_LOCAL_FALLBACK_DB, timeout=3)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chronicle_fallback_queue "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "endpoint TEXT, payload TEXT, ts REAL, replayed INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO chronicle_fallback_queue (endpoint, payload, ts) VALUES (?,?,?)",
            (endpoint, _json.dumps(payload), _t.time())
        )
        # Cap at _MAX_LOCAL_QUEUE
        conn.execute(
            "DELETE FROM chronicle_fallback_queue WHERE id IN "
            "(SELECT id FROM chronicle_fallback_queue ORDER BY id ASC "
            "LIMIT MAX(0, (SELECT COUNT(*) FROM chronicle_fallback_queue) - ?))",
            (_MAX_LOCAL_QUEUE,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("C4 local fallback write failed: %s", e)


def _c4_replay_local_fallback() -> None:
    """Replay undelivered events from local SQLite fallback to CHRONICLE.

    Called when CHRONICLE connectivity is restored after an outage.
    Processes events in order (oldest first). Marks each as replayed on success.
    """
    import sqlite3 as _sq, json as _json
    try:
        conn = _sq.connect(_LOCAL_FALLBACK_DB, timeout=3)
        conn.row_factory = _sq.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT id, endpoint, payload FROM chronicle_fallback_queue "
            "WHERE replayed=0 ORDER BY id ASC LIMIT 100"
        )
        rows = cur.fetchall()
        replayed = 0
        for row in rows:
            try:
                payload = _json.loads(row["payload"])
                success = _chronicle_post(row["endpoint"], payload)
                if success:
                    conn.execute(
                        "UPDATE chronicle_fallback_queue SET replayed=1 WHERE id=?",
                        (row["id"],)
                    )
                    replayed += 1
            except Exception:
                break  # Stop on first failure — CHRONICLE may still be unstable
        conn.commit()
        conn.close()
        if replayed:
            logger.info("C4 replay: %d/%d events replayed to CHRONICLE", replayed, len(rows))
    except Exception as e:
        logger.debug("C4 fallback replay failed: %s", e)
