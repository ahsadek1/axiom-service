"""
vector_resilience.py — VECTOR Resilience Layer v1.1
Spec: vector-resilience-v1.md (self-authored by VECTOR, revised post Cipher review)
Built by: GENESIS
Date: 2026-05-02

7 blocks:
  V1 — Diagnostic crash guard (tombstone + emergency Telegram)
  V2 — Restart verification contract (body status, not just HTTP 200)
  V3 — Log scan staleness guard + intervention idempotency lock
  V4 — Credential watchdog liveness (watches the watcher)
  V5 — Bus directive schema validation + consumed_ids dedup
  V6 — Bus poison message guard (per-message exception boundary)
  V7 — @audit_action decorator (structured audit on every action)
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("vector.resilience")

# ---------------------------------------------------------------------------
# State directories (persistent across reboots)
# ---------------------------------------------------------------------------

VECTOR_STATE_DIR = Path("/Users/ahmedsadek/nexus/vector/state")
LOCK_DIR         = VECTOR_STATE_DIR / "locks"
TOMBSTONE_PATH   = VECTOR_STATE_DIR / "diagnostic_crashed"
CONSUMED_IDS_PATH = VECTOR_STATE_DIR / "consumed_ids.json"

VECTOR_STATE_DIR.mkdir(parents=True, exist_ok=True)
LOCK_DIR.mkdir(parents=True, exist_ok=True)

ET = __import__("pytz").timezone("America/New_York") if __import__("importlib").util.find_spec("pytz") else timezone.utc

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

SERVICES = [
    ("axiom",           8001, "ai.nexus.axiom"),
    ("alpha-buffer",    8002, "ai.nexus.alpha-buffer"),
    ("prime-buffer",    8003, "ai.nexus.prime-buffer"),
    ("omni",            8004, "ai.nexus.omni"),
    ("alpha-execution", 8005, "ai.nexus.alpha-execution"),
    ("prime-execution", 8006, "ai.nexus.prime-execution"),
    ("oracle",          8007, "ai.nexus.oracle"),
    ("ails",            8008, "ai.nexus.ails"),
    ("guardian-angel",  8009, "ai.nexus.guardian-angel"),
]

VALID_SERVICE_NAMES   = {name for name, _, _ in SERVICES}
VALID_DIRECTIVE_TYPES = {"restart_service", "check_service", "escalate", "ping"}

# Per-service verification timeouts (local = 5s)
SERVICE_VERIFY_TIMEOUT = {name: 5 for name, _, _ in SERVICES}


# ===========================================================================
# BLOCK V1 — Diagnostic Crash Guard
# ===========================================================================

def wrap_diagnostic_main(main_fn):
    """
    Two-tier crash guard for vector_diagnostic.py main().
    On crash: writes tombstone to persistent path + fires emergency Telegram.
    On clean run: clears tombstone.
    Guardian Angel checks tombstone every 5 min and fires VECTOR_DIAGNOSTIC_DOWN.
    """
    try:
        main_fn()
        TOMBSTONE_PATH.unlink(missing_ok=True)
        logger.info("Diagnostic run complete — tombstone cleared")
    except Exception as e:
        logger.critical("vector_diagnostic CRASHED: %s", e)
        _emergency_telegram_alert(f"🚨 vector_diagnostic CRASHED: {str(e)[:300]}")
        TOMBSTONE_PATH.write_text(json.dumps({
            "crashed_at": datetime.now(timezone.utc).isoformat(),
            "error": str(e)[:500],
        }))
        raise  # re-raise so caller sees exit code 1


def check_tombstone_for_guardian_angel() -> Optional[dict]:
    """
    Called by Guardian Angel every 5 min.
    Returns tombstone data if present and age < 24h, else None.
    """
    if not TOMBSTONE_PATH.exists():
        return None
    try:
        age_hours = (time.time() - TOMBSTONE_PATH.stat().st_mtime) / 3600
        if age_hours > 24:
            return None  # stale tombstone — ignore
        data = json.loads(TOMBSTONE_PATH.read_text())
        data["age_hours"] = round(age_hours, 2)
        return data
    except Exception as e:
        logger.warning("Tombstone read failed: %s", e)
        return None


def _emergency_telegram_alert(message: str) -> None:
    """Direct Telegram alert — no bus dependency (bus may be down)."""
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("AHMED_CHAT_ID", "8573754783")
        if not token:
            logger.error("TELEGRAM_BOT_TOKEN not set — cannot send emergency alert")
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception as e:
        logger.error("Emergency Telegram alert failed: %s", e)


# ===========================================================================
# BLOCK V2 — Restart Verification Contract
# ===========================================================================

@dataclass
class RestartVerification:
    service: str
    http_code: int
    body_status: str    # parsed from body, "MISSING" if key absent
    latency_ms: float
    is_healthy: bool    # True ONLY if 200 AND body status in ("healthy", "active")
    failure_reason: str


def verify_service_recovery(name: str, port: int) -> RestartVerification:
    """
    Verify a service restarted successfully.
    Checks body content, not just HTTP 200.
    Missing status key = degraded (suspicious, logged).
    Returns RestartVerification — is_healthy=True only if fully healthy.
    """
    timeout = SERVICE_VERIFY_TIMEOUT.get(name, 10)
    try:
        r = requests.get(f"http://localhost:{port}/health", timeout=timeout)
        body = r.json() if r.status_code == 200 else {}
        body_status = body.get("status", "MISSING")
        if body_status == "MISSING":
            logger.warning("Service %s returned 200 with no status field — treating as degraded", name)
        is_healthy = r.status_code == 200 and body_status in ("healthy", "active")
        return RestartVerification(
            service=name,
            http_code=r.status_code,
            body_status=body_status,
            latency_ms=r.elapsed.total_seconds() * 1000,
            is_healthy=is_healthy,
            failure_reason="" if is_healthy else f"status={body_status}",
        )
    except Exception as e:
        return RestartVerification(
            service=name,
            http_code=0,
            body_status="UNREACHABLE",
            latency_ms=0,
            is_healthy=False,
            failure_reason=str(e)[:200],
        )


# ===========================================================================
# BLOCK V3 — Log Scan Staleness Guard + Intervention Idempotency Lock
# ===========================================================================

class InterventionInProgress(Exception):
    pass


@contextlib.contextmanager
def intervention_lock(service_name: str, timeout: int = 60):
    """
    Filesystem sentinel lock per service.
    Prevents two concurrent diagnostic runs from double-restarting the same service.
    Lock file in persistent state dir (survives reboots unlike /tmp).
    Raises InterventionInProgress if lock is fresh (< timeout seconds).
    """
    lock_path = LOCK_DIR / f"{service_name}.lock"
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age < timeout:
            raise InterventionInProgress(
                f"{service_name} already being intervened (lock age {age:.0f}s)"
            )
    try:
        lock_path.write_text(datetime.now(timezone.utc).isoformat())
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def check_log_staleness(log_path: str, service: str, max_age_minutes: int = 20) -> Optional[str]:
    """
    Check if a log file has been written to recently.
    Returns a finding string if stale, None if fresh.
    A stale log during market hours = service frozen or log rotated.
    """
    try:
        if not os.path.exists(log_path):
            return f"LOG_MISSING: {log_path} does not exist"
        mtime = os.path.getmtime(log_path)
        age_minutes = (time.time() - mtime) / 60
        if age_minutes > max_age_minutes:
            return f"LOG_STALE: {service} last write {age_minutes:.0f}m ago (max {max_age_minutes}m)"
        return None
    except Exception as e:
        return f"LOG_CHECK_ERROR: {service}: {e}"


# ===========================================================================
# BLOCK V4 — Credential Watchdog Liveness
# ===========================================================================

def record_watchdog_run() -> None:
    """
    Write heartbeat to RESILIENCE-DB on successful credential watchdog run.
    Call at end of every successful vector_credential_watchdog.py run.
    Never raises.
    """
    try:
        from shared.resilience.triad_db import log_intervention
        log_intervention(
            agent="vector",
            action_type="watchdog_heartbeat",
            target="credential_watchdog",
            trigger="scheduled_run",
            outcome="recovered",  # "recovered" = ran clean
        )
        logger.info("Credential watchdog heartbeat recorded to RESILIENCE-DB")
    except Exception as e:
        logger.error("record_watchdog_run failed (non-fatal): %s", e)


def check_watchdog_liveness() -> None:
    """
    Check if credential watchdog has run in the last 8 days.
    Called from VECTOR's 30-min heartbeat.
    Fails open — CHRONICLE down must not block heartbeat.
    Null guard — no alert on first-ever run.
    """
    try:
        from shared.resilience.triad_db import _get_conn, init_triad_db
        import shared.resilience.triad_db as tdb

        conn = tdb._get_conn()
        row = conn.execute(
            """SELECT timestamp FROM intervention_log
               WHERE agent='vector' AND action_type='watchdog_heartbeat'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        conn.close()

        if row is None:
            logger.info("credential_watchdog: no prior run recorded (first run expected)")
            return

        last_run_iso = row["timestamp"]
        from datetime import datetime, timezone
        last_run_dt = datetime.fromisoformat(last_run_iso)
        days_since = (datetime.now(timezone.utc) - last_run_dt).days

        if days_since > 8:
            from shared.resilience.alert_router import route_alert
            route_alert(
                agent="vector",
                issue_type="watchdog_liveness",
                target="credential_watchdog",
                severity="P1",
                detail=f"credential_watchdog has not run in {days_since} days",
            )
            logger.warning("VECTOR: credential_watchdog last run %d days ago — P1 alert sent", days_since)

    except Exception as e:
        logger.warning("check_watchdog_liveness failed (fail-open): %s", e)


# ===========================================================================
# BLOCK V5 — Bus Directive Schema Validation + Dedup
# BLOCK V6 — Bus Poison Message Guard (embedded in V5 exception boundary)
# ===========================================================================

def _load_consumed_ids() -> set:
    """Load consumed message IDs from persistent file. Returns empty set on error."""
    try:
        return set(json.loads(CONSUMED_IDS_PATH.read_text()))
    except Exception:
        return set()


def _save_consumed_id(msg_id: str) -> None:
    """Append msg_id to consumed set. Keeps last 1000 only."""
    ids = _load_consumed_ids()
    ids.add(msg_id)
    try:
        CONSUMED_IDS_PATH.write_text(json.dumps(list(ids)[-1000:]))
    except Exception as e:
        logger.error("_save_consumed_id failed: %s", e)


def _quarantine(agent: str, directive, reason: str) -> None:
    """Quarantine a bad directive to RESILIENCE-DB."""
    try:
        from shared.resilience.triad_db import quarantine_directive
        quarantine_directive(agent=agent, raw_directive=str(directive)[:500], quarantine_reason=reason)
    except Exception as e:
        logger.error("quarantine failed: %s", e)


def validate_and_process_directives(directives: list, process_fn) -> dict:
    """
    V5 + V6: Validate bus directives and process with per-message exception boundary.

    - Deduplicates via consumed_ids (persistent, no bus /consume endpoint needed)
    - Schema validates type + target before executing
    - Invalid directives quarantined to RESILIENCE-DB
    - Per-message exception boundary: one bad message never crashes the loop

    Args:
        directives:  List of directive dicts from bus inbox
        process_fn:  Callable(directive) — the actual action to take

    Returns:
        dict with counts: processed, skipped_dedup, quarantined, failed
    """
    consumed = _load_consumed_ids()
    counts = {"processed": 0, "skipped_dedup": 0, "quarantined": 0, "failed": 0}

    for d in directives:
        msg_id = d.get("id", "")

        # V5: Dedup check
        if msg_id and msg_id in consumed:
            counts["skipped_dedup"] += 1
            continue

        # V5: Schema validation
        if d.get("type") not in VALID_DIRECTIVE_TYPES:
            _quarantine("vector", d, f"unknown directive type: {d.get('type')}")
            if msg_id:
                _save_consumed_id(msg_id)
            counts["quarantined"] += 1
            continue

        if d.get("type") == "restart_service":
            if d.get("target") not in VALID_SERVICE_NAMES:
                _quarantine("vector", d, f"unknown target: {d.get('target')}")
                if msg_id:
                    _save_consumed_id(msg_id)
                counts["quarantined"] += 1
                continue

        # V6: Per-message exception boundary — one bad message never crashes the loop
        try:
            process_fn(d)
            counts["processed"] += 1
        except Exception as e:
            logger.error("Directive processing failed — quarantining: %s | error: %s",
                         str(d)[:80], e)
            _quarantine("vector", d, str(e)[:200])
            counts["failed"] += 1
        finally:
            if msg_id:
                _save_consumed_id(msg_id)

    return counts


# ===========================================================================
# BLOCK V7 — Action Audit Log (@audit_action decorator)
# ===========================================================================

def _probe_health_for_audit(service: str) -> Optional[dict]:
    """Quick health probe for pre/post state in audit log."""
    try:
        for name, port, _ in SERVICES:
            if name == service:
                r = requests.get(f"http://localhost:{port}/health", timeout=3)
                if r.status_code == 200:
                    body = r.json()
                    return {"status": body.get("status"), "http_code": 200}
                return {"http_code": r.status_code}
        return None
    except Exception:
        return None


def audit_action(action_type: str):
    """
    Decorator that wraps any VECTOR infrastructure action with structured audit logging.
    Every action written to RESILIENCE-DB intervention_log — no code path can skip it.

    Usage:
        @audit_action(action_type="service_restart")
        def restart_service(service: str, port: int) -> RestartVerification:
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            target  = kwargs.get("service") or (args[0] if args else "unknown")
            started = time.time()
            pre_state = _probe_health_for_audit(str(target))
            outcome, error = "failed", None
            try:
                result  = fn(*args, **kwargs)
                outcome = "recovered"
                return result
            except Exception as e:
                error   = str(e)[:200]
                outcome = "failed"
                raise
            finally:
                post_state = _probe_health_for_audit(str(target))
                try:
                    from shared.resilience.triad_db import log_intervention
                    log_intervention(
                        agent="vector",
                        action_type=action_type,
                        target=str(target),
                        trigger="vector_diagnostic",
                        outcome=outcome,
                        pre_state=pre_state,
                        post_state=post_state,
                        elapsed_sec=round(time.time() - started, 2),
                        error=error,
                    )
                except Exception as log_err:
                    logger.error("audit_action log failed (non-fatal): %s", log_err)
        return wrapper
    return decorator
