"""
telemetry.py — NSP Telemetry Fabric.

Architecture:
- One polling thread per service.
- Each thread runs an adaptive polling state machine:
    HEALTHY     → poll every 30s
    AMBER       → poll every 5s  (anomaly detected)
    POST_RESTART→ poll every 2s for first 5 min, then revert to HEALTHY
- All collected signal snapshots are enqueued to the single DB writer thread
  via db.insert_telemetry_snapshot() (fire-and-forget).
- Log scanning uses a file-position cache so only new content is read each cycle.

Signals collected per service (Phase 1 — HTTP + log scan skeleton):
  AXIOM (8001):      pool_size, regime, regime_last_updated_age_s, vix,
                     tier2_consecutive_failures, submissions_open,
                     polygon_timeout_count (log scan)
  ALPHA-BUFFER(8002):cb_status, omni_retry_queue_depth, db_write_error_count (log scan)
  PRIME-BUFFER(8003):same as ALPHA-BUFFER
  OMNI (8004):       brain_error_rate, last_synthesis_age_s, pool_active_threads,
                     canary_gate_status, halt_active, echo_chamber_count
  ALPHA-EXEC (8005): alpaca_reachable, execution_paused, vix_brake,
                     ticker_fail_counts, reconcile_mismatches, stale_pending_positions
  PRIME-EXEC (8006): same as ALPHA-EXEC
  ORACLE (8007):     cache_hit_rate, cache_warm_count, polygon_timeout_rate (log scan),
                     engine_error_count
  AILS (8008):       ails_db_status, backtest_db_status, write_error_count (log scan)
  SCANNERS (9001-3): brain_failures, today_picks, total_windows, picks_per_window_ratio
  psutil (all):      memory_percent, cpu_percent, disk_percent
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil
import requests

import config
import db

logger = logging.getLogger("nsp.telemetry")

# ---------------------------------------------------------------------------
# Adaptive polling state machine
# ---------------------------------------------------------------------------

class PollState(Enum):
    """Polling rate states per the spec adaptive polling contract."""
    HEALTHY = "HEALTHY"
    AMBER = "AMBER"
    POST_RESTART = "POST_RESTART"


@dataclass
class PollStateMachine:
    """Per-service adaptive polling state.

    Transitions:
      HEALTHY  ──anomaly──►  AMBER
      AMBER    ──cleared──►  HEALTHY
      any      ──restart──►  POST_RESTART (2s for 5min, then HEALTHY)
    """
    service: str
    state: PollState = PollState.HEALTHY
    post_restart_started_at: Optional[float] = None

    def interval(self) -> float:
        """Return the current polling interval in seconds.

        Auto-transitions POST_RESTART → HEALTHY after 5 minutes.

        Returns:
            Polling interval in seconds.
        """
        if self.state == PollState.POST_RESTART:
            if self.post_restart_started_at is not None:
                elapsed = time.monotonic() - self.post_restart_started_at
                if elapsed >= config.POLL_POST_RESTART_WINDOW_S:
                    self._do_transition(PollState.HEALTHY)
                    return config.POLL_HEALTHY_S
            return config.POLL_POST_RESTART_S
        if self.state == PollState.AMBER:
            return config.POLL_AMBER_S
        return config.POLL_HEALTHY_S

    def transition_amber(self) -> None:
        """Move to AMBER state if currently HEALTHY.

        Idempotent — already-AMBER stays AMBER.
        POST_RESTART is not overridden by AMBER.
        """
        if self.state == PollState.HEALTHY:
            self._do_transition(PollState.AMBER)

    def transition_healthy(self) -> None:
        """Clear anomaly — return to HEALTHY polling rate.

        No-op if already HEALTHY.
        """
        if self.state != PollState.HEALTHY:
            self._do_transition(PollState.HEALTHY)
            self.post_restart_started_at = None

    def transition_post_restart(self) -> None:
        """Enter POST_RESTART state — 2s polling for the next 5 minutes."""
        self._do_transition(PollState.POST_RESTART)
        self.post_restart_started_at = time.monotonic()

    def _do_transition(self, new_state: PollState) -> None:
        """Log and execute a state transition.

        Args:
            new_state: Target state.
        """
        if new_state != self.state:
            logger.info(
                "[%s] Poll state: %s → %s",
                self.service, self.state.value, new_state.value,
            )
        self.state = new_state


# ---------------------------------------------------------------------------
# Log scanner — reads only new content since last check
# ---------------------------------------------------------------------------

# Maps log_path → last byte offset scanned
_log_positions: Dict[str, int] = {}
_log_pos_lock = threading.Lock()


def _scan_log_for_pattern(log_path: str, pattern: str) -> int:
    """Count occurrences of a regex pattern in new log content since last scan.

    Uses a byte-offset cache so only new lines are read each call.

    Args:
        log_path: Absolute path to the log file.
        pattern: Regex pattern to count.

    Returns:
        Count of matches in new content (0 if file missing or unreadable).
    """
    if not log_path:
        return 0
    try:
        if not os.path.exists(log_path):
            return 0
        file_size = os.path.getsize(log_path)
        with _log_pos_lock:
            last_pos = _log_positions.get(log_path, max(0, file_size - 65536))
            # Handle log rotation (file shrank)
            if file_size < last_pos:
                last_pos = 0

        count = 0
        compiled = re.compile(pattern)
        with open(log_path, "r", errors="replace") as fh:
            fh.seek(last_pos)
            new_content = fh.read()
            count = len(compiled.findall(new_content))
            new_pos = fh.tell()

        with _log_pos_lock:
            _log_positions[log_path] = new_pos

        return count
    except OSError as exc:
        logger.debug("Log scan failed for %s: %s", log_path, exc)
        return 0
    except Exception as exc:
        logger.warning("Unexpected log scan error for %s: %s", log_path, exc)
        return 0


# ---------------------------------------------------------------------------
# psutil helpers
# ---------------------------------------------------------------------------

def _get_process_for_port(port: int) -> Optional[psutil.Process]:
    """Find the process listening on the given TCP port.

    Args:
        port: TCP port number.

    Returns:
        psutil.Process if found, None otherwise.
    """
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                if conn.pid is not None:
                    try:
                        return psutil.Process(conn.pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        return None
    except Exception as exc:
        logger.debug("net_connections scan failed for port %d: %s", port, exc)
    return None


def _get_process_metrics(port: int) -> Dict[str, Any]:
    """Collect CPU%, memory%, and disk% for the process on the given port.

    Disk% is the system-level disk usage (processes don't own disk%).

    Args:
        port: TCP port of the service.

    Returns:
        Dict with process_running, cpu_percent, memory_percent, disk_percent.
    """
    disk_percent: Optional[float] = None
    try:
        disk_percent = psutil.disk_usage("/").percent
    except Exception:
        pass

    proc = _get_process_for_port(port)
    if proc is None:
        return {
            "process_running": False,
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": disk_percent,
        }
    try:
        return {
            "process_running": True,
            "cpu_percent": proc.cpu_percent(interval=None),
            "memory_percent": round(proc.memory_percent(), 3),
            "disk_percent": disk_percent,
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        logger.debug("Process metrics error port %d: %s", port, exc)
        return {
            "process_running": False,
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": disk_percent,
        }


# ---------------------------------------------------------------------------
# HTTP collector helper
# ---------------------------------------------------------------------------

def _fetch_health(url: str, secret: str = "") -> Tuple[bool, Dict[str, Any]]:
    """GET /health from a service and return (reachable, data_dict).

    Args:
        url: Base URL of the service (e.g. 'http://localhost:8001').
        secret: Optional X-Nexus-Secret header value.

    Returns:
        (True, parsed JSON dict) if reachable and status 200.
        (False, {'_error': reason}) otherwise.
    """
    headers = {}
    if secret:
        headers["X-Nexus-Secret"] = secret
    try:
        resp = requests.get(
            f"{url}/health",
            headers=headers,
            timeout=config.HTTP_TIMEOUT_S,
        )
        if resp.status_code == 200:
            try:
                return True, resp.json()
            except ValueError:
                return True, {"_raw": resp.text[:500]}
        return False, {"_error": f"http_{resp.status_code}"}
    except requests.Timeout:
        return False, {"_error": "timeout"}
    except requests.ConnectionError:
        return False, {"_error": "connection_refused"}
    except requests.RequestException as exc:
        return False, {"_error": str(exc)[:120]}


# ---------------------------------------------------------------------------
# Per-service collectors — return Dict[str, Any] of signals
# ---------------------------------------------------------------------------

def _collect_axiom() -> Dict[str, Any]:
    """Collect AXIOM (8001) telemetry signals.

    HTTP signals: pool_size, regime, regime_last_updated_age_s, vix,
                  tier2_consecutive_failures, submissions_open.
    Log scan:     polygon_timeout_count.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.AXIOM_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "pool_size": data.get("pool_size"),
        "regime": data.get("regime"),
        "regime_last_updated_age_s": data.get("regime_last_updated_age_s"),
        "vix": data.get("vix"),
        "tier2_consecutive_failures": data.get("tier2_consecutive_failures"),
        "submissions_open": data.get("submissions_open"),
        "_http_error": data.get("_error"),
    }
    # Log scan — polygon_timeout_count (new occurrences since last scan)
    log_path = config.SERVICE_LOG_PATHS.get("axiom", "")
    signals["polygon_timeout_count"] = _scan_log_for_pattern(
        log_path, r"polygon.*timeout|timeout.*polygon"
    )
    # psutil
    signals.update(_get_process_metrics(config.SERVICE_PORTS["axiom"]))
    return signals


def _collect_alpha_buffer() -> Dict[str, Any]:
    """Collect ALPHA-BUFFER (8002) telemetry signals.

    HTTP signals: cb_status, omni_retry_queue_depth.
    Log scan:     db_write_error_count.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.ALPHA_BUFFER_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "cb_status": data.get("cb_status"),
        "omni_retry_queue_depth": data.get("omni_retry_queue_depth"),
        "_http_error": data.get("_error"),
    }
    log_path = config.SERVICE_LOG_PATHS.get("alpha-buffer", "")
    signals["db_write_error_count"] = _scan_log_for_pattern(
        log_path, r"db.*write.*error|write.*error.*db|OperationalError"
    )
    signals.update(_get_process_metrics(config.SERVICE_PORTS["alpha-buffer"]))
    return signals


def _collect_prime_buffer() -> Dict[str, Any]:
    """Collect PRIME-BUFFER (8003) telemetry signals.

    HTTP signals: cb_status, omni_retry_queue_depth.
    Log scan:     db_write_error_count.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.PRIME_BUFFER_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "cb_status": data.get("cb_status"),
        "omni_retry_queue_depth": data.get("omni_retry_queue_depth"),
        "_http_error": data.get("_error"),
    }
    log_path = config.SERVICE_LOG_PATHS.get("prime-buffer", "")
    signals["db_write_error_count"] = _scan_log_for_pattern(
        log_path, r"db.*write.*error|write.*error.*db|OperationalError"
    )
    signals.update(_get_process_metrics(config.SERVICE_PORTS["prime-buffer"]))
    return signals


def _collect_omni() -> Dict[str, Any]:
    """Collect OMNI (8004) telemetry signals.

    HTTP signals: brain_error_rate, last_synthesis_age_s, pool_active_threads,
                  canary_gate_status, halt_active, echo_chamber_count.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.OMNI_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "brain_error_rate": data.get("brain_error_rate"),
        "last_synthesis_age_s": data.get("last_synthesis_age_s"),
        "pool_active_threads": data.get("pool_active_threads"),
        "canary_gate_status": data.get("canary_gate_status"),
        "halt_active": data.get("halt_active"),
        "echo_chamber_count": data.get("echo_chamber_count"),
        "_http_error": data.get("_error"),
    }
    signals.update(_get_process_metrics(config.SERVICE_PORTS["omni"]))
    return signals


def _collect_alpha_execution() -> Dict[str, Any]:
    """Collect ALPHA-EXEC (8005) telemetry signals.

    HTTP signals: alpaca_reachable, execution_paused, vix_brake,
                  ticker_fail_counts, reconcile_mismatches, stale_pending_positions.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.ALPHA_EXEC_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "alpaca_reachable": data.get("alpaca_reachable"),
        "execution_paused": data.get("execution_paused"),
        "vix_brake": data.get("vix_brake"),
        "ticker_fail_counts": data.get("ticker_fail_counts"),
        "reconcile_mismatches": data.get("reconcile_mismatches"),
        "stale_pending_positions": data.get("stale_pending_positions"),
        "_http_error": data.get("_error"),
    }
    signals.update(_get_process_metrics(config.SERVICE_PORTS["alpha-execution"]))
    return signals


def _collect_prime_execution() -> Dict[str, Any]:
    """Collect PRIME-EXEC (8006) telemetry signals (same schema as ALPHA-EXEC).

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.PRIME_EXEC_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "alpaca_reachable": data.get("alpaca_reachable"),
        "execution_paused": data.get("execution_paused"),
        "vix_brake": data.get("vix_brake"),
        "ticker_fail_counts": data.get("ticker_fail_counts"),
        "reconcile_mismatches": data.get("reconcile_mismatches"),
        "stale_pending_positions": data.get("stale_pending_positions"),
        "_http_error": data.get("_error"),
    }
    signals.update(_get_process_metrics(config.SERVICE_PORTS["prime-execution"]))
    return signals


def _collect_oracle() -> Dict[str, Any]:
    """Collect ORACLE (8007) telemetry signals.

    HTTP signals: cache_hit_rate, cache_warm_count, engine_error_count.
    Log scan:     polygon_timeout_rate.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.ORACLE_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "cache_hit_rate": data.get("cache_hit_rate"),
        "cache_warm_count": data.get("cache_warm_count"),
        "engine_error_count": data.get("engine_error_count"),
        "_http_error": data.get("_error"),
    }
    log_path = config.SERVICE_LOG_PATHS.get("oracle", "")
    signals["polygon_timeout_rate"] = _scan_log_for_pattern(
        log_path, r"polygon.*timeout|timeout.*polygon"
    )
    signals.update(_get_process_metrics(config.SERVICE_PORTS["oracle"]))
    return signals


def _collect_ails() -> Dict[str, Any]:
    """Collect AILS (8008) telemetry signals.

    HTTP signals: ails_db_status, backtest_db_status.
    Log scan:     write_error_count.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(config.AILS_URL, config.NEXUS_SECRET)
    signals: Dict[str, Any] = {
        "reachable": reachable,
        "ails_db_status": data.get("ails_db_status"),
        "backtest_db_status": data.get("backtest_db_status"),
        "_http_error": data.get("_error"),
    }
    log_path = config.SERVICE_LOG_PATHS.get("ails", "")
    signals["write_error_count"] = _scan_log_for_pattern(
        log_path, r"write.*error|OperationalError|disk.*full"
    )
    signals.update(_get_process_metrics(config.SERVICE_PORTS["ails"]))
    return signals


def _collect_scanner(name: str, url: str, port: int) -> Dict[str, Any]:
    """Collect scanner (Cipher/Atlas/Sage) telemetry signals.

    HTTP signals: brain_failures, today_picks, total_windows, picks_per_window_ratio.

    Args:
        name: Scanner service name (e.g. 'cipher-scanner').
        url: Base URL of the scanner.
        port: TCP port for psutil.

    Returns:
        Dict of signal name → value.
    """
    reachable, data = _fetch_health(url, config.NEXUS_SECRET)
    today_picks: Optional[int] = data.get("today_picks")
    total_windows: Optional[int] = data.get("total_windows")
    picks_per_window_ratio: Optional[float] = None
    if (
        today_picks is not None
        and total_windows is not None
        and total_windows > 0
    ):
        picks_per_window_ratio = round(today_picks / total_windows, 4)

    signals: Dict[str, Any] = {
        "reachable": reachable,
        "brain_failures": data.get("brain_failures"),
        "today_picks": today_picks,
        "total_windows": total_windows,
        "picks_per_window_ratio": picks_per_window_ratio,
        "_http_error": data.get("_error"),
    }
    signals.update(_get_process_metrics(port))
    return signals


def _collect_cipher_scanner() -> Dict[str, Any]:
    """Collect Cipher scanner (9001) telemetry.

    Returns:
        Dict of signal name → value.
    """
    return _collect_scanner(
        "cipher-scanner",
        config.CIPHER_SCANNER_URL,
        config.SERVICE_PORTS["cipher-scanner"],
    )


def _collect_atlas_scanner() -> Dict[str, Any]:
    """Collect Atlas scanner (9002) telemetry.

    Returns:
        Dict of signal name → value.
    """
    return _collect_scanner(
        "atlas-scanner",
        config.ATLAS_SCANNER_URL,
        config.SERVICE_PORTS["atlas-scanner"],
    )


def _collect_sage_scanner() -> Dict[str, Any]:
    """Collect Sage scanner (9003) telemetry.

    Returns:
        Dict of signal name → value.
    """
    return _collect_scanner(
        "sage-scanner",
        config.SAGE_SCANNER_URL,
        config.SERVICE_PORTS["sage-scanner"],
    )


# ---------------------------------------------------------------------------
# Collector registry — service name → (collector_fn, port)
# ---------------------------------------------------------------------------

_COLLECTORS: List[Tuple[str, Callable[[], Dict[str, Any]]]] = [
    ("axiom",           _collect_axiom),
    ("alpha-buffer",    _collect_alpha_buffer),
    ("prime-buffer",    _collect_prime_buffer),
    ("omni",            _collect_omni),
    ("alpha-execution", _collect_alpha_execution),
    ("prime-execution", _collect_prime_execution),
    ("oracle",          _collect_oracle),
    ("ails",            _collect_ails),
    ("cipher-scanner",  _collect_cipher_scanner),
    ("atlas-scanner",   _collect_atlas_scanner),
    ("sage-scanner",    _collect_sage_scanner),
]


# ---------------------------------------------------------------------------
# Polling thread
# ---------------------------------------------------------------------------

_stop_event: threading.Event = threading.Event()
_threads: List[threading.Thread] = []


def _poll_service(
    service: str,
    collector_fn: Callable[[], Dict[str, Any]],
    state: PollStateMachine,
) -> None:
    """Main loop for a single service polling thread.

    1. Compute current interval from adaptive state machine.
    2. Call collector_fn() to get signals.
    3. Persist snapshot to DB (fire-and-forget).
    4. Transition poll state based on reachability.
    5. Sleep for the computed interval.

    Args:
        service: Service name (used for logging and DB key).
        collector_fn: Function that returns a Dict of signals.
        state: The per-service PollStateMachine instance.
    """
    logger.info("[%s] Polling thread started — initial state: %s", service, state.state.value)

    while not _stop_event.is_set():
        interval = state.interval()

        try:
            signals = collector_fn()
        except Exception as exc:
            logger.error("[%s] Collector raised unexpectedly: %s", service, exc, exc_info=True)
            signals = {"_collector_crash": str(exc)[:200], "reachable": False}

        # Persist snapshot (fire-and-forget — never blocks the polling loop)
        try:
            db.insert_telemetry_snapshot(
                service=service,
                poll_state=state.state.value,
                signals=json.dumps(signals, default=str),
            )
        except Exception as exc:
            logger.error("[%s] DB snapshot enqueue failed: %s", service, exc)

        # Adaptive state transition based on reachability
        reachable = signals.get("reachable", False)
        http_error = signals.get("_http_error")
        if not reachable or http_error:
            state.transition_amber()
        else:
            state.transition_healthy()

        _stop_event.wait(timeout=interval)

    logger.info("[%s] Polling thread stopped", service)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_all_collectors() -> None:
    """Start one polling thread per service.

    Each thread is a daemon thread — it exits when the main process exits.
    Threads are tracked in _threads so stop_all_collectors() can join them.
    """
    global _threads
    _stop_event.clear()
    _threads = []

    for service, collector_fn in _COLLECTORS:
        state = PollStateMachine(service=service)
        t = threading.Thread(
            target=_poll_service,
            args=(service, collector_fn, state),
            name=f"nsp-telemetry-{service}",
            daemon=True,
        )
        t.start()
        _threads.append(t)
        logger.info("Collector started: %s", service)

    logger.info("All %d collector threads started", len(_threads))


def stop_all_collectors(timeout_s: float = 5.0) -> None:
    """Signal all polling threads to stop and wait for them to exit.

    Args:
        timeout_s: Maximum seconds to wait for each thread.
    """
    logger.info("Stopping %d collector threads…", len(_threads))
    _stop_event.set()
    for t in _threads:
        t.join(timeout=timeout_s)
        if t.is_alive():
            logger.warning("Thread %s did not exit within %.1fs", t.name, timeout_s)
    logger.info("All collector threads stopped")


def get_collector_service_names() -> List[str]:
    """Return the list of service names registered for collection.

    Returns:
        List of service name strings.
    """
    return [name for name, _ in _COLLECTORS]
