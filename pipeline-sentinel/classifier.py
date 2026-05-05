"""
classifier.py — Failure mode detection and classification for Pipeline Sentinel.

Runs detect_all() on a background daemon thread every 30 seconds.
Inserts failure events to DB and sends Telegram alerts for each new class.

Seven failure classes (from spec):
  PIPELINE_STALL      — pick stuck >stall_window_s without progress
  EXECUTOR_SLOW       — Alpaca hop latency P95 >3000ms
  BRAIN_TIMEOUT       — OMNI started but not completed after 6 minutes
  NETWORK_DEGRADED    — P99 inter-hop latency >2000ms in 3+ consecutive picks
  AGENT_SILENT        — Any agent with 0 hops in last 15-minute window
  COMPLETION_RATE_LOW — <80% of last 10 picks reached alpaca_confirmed
  DATA_STALE          — >15min gap in traces (data feed issue)
"""

import logging
import time
from datetime import datetime
from statistics import quantiles
from typing import Any, Dict, List, Optional, Set

import pytz

from database import (
    get_active_failures,
    get_recent_traces,
    get_stalled_picks,
    insert_failure_event,
    resolve_failure,
)
from notifier import TelegramNotifier

logger = logging.getLogger("sentinel.classifier")

# Agent service names that must be active each 15-min window
_AGENT_SERVICES: Set[str] = {"cipher", "atlas", "sage"}

# Hop that marks completion
_FINAL_HOP = "alpaca_confirmed"

# OMNI timeout before declaring BRAIN_TIMEOUT
_BRAIN_TIMEOUT_S = 360   # 6 minutes

# Cipher fix: hard timeout for AGENT_SILENCE — restart agent after this duration
_AGENT_SILENCE_HARD_TIMEOUT_S = 1800   # 30 minutes of silence → force restart
_agent_silence_since: Dict[str, float] = {}  # service → unix ts when silence first detected
_agent_hard_reset_fired: Set[str] = set()     # services that have had a hard reset this session

# Market hours (ET) — only meaningful failures during these windows
_ET = pytz.timezone("America/New_York")
_MARKET_OPEN_H,  _MARKET_OPEN_M  = 9, 30
_MARKET_CLOSE_H, _MARKET_CLOSE_M = 16, 0


_AGENT_LAUNCHAGENT_MAP = {
    "atlas":  "ai.nexus.atlas",
    "cipher": "ai.nexus.cipher",
    "sage":   "ai.nexus.sage",
}

_LAUNCHAGENT_PLIST_DIR = "/Users/ahmedsadek/Library/LaunchAgents"


def _restart_agent_service(service: str, notifier: "TelegramNotifier") -> None:
    """
    Cipher fix: hard-reset a silent agent by unloading and reloading its LaunchAgent.

    Called after AGENT_SILENCE hard timeout (_AGENT_SILENCE_HARD_TIMEOUT_S).
    Logs and alerts Ahmed. Never raises — failures are logged only.

    Args:
        service:  Agent service name (e.g. "atlas").
        notifier: TelegramNotifier instance for alerting.
    """
    import subprocess
    label = _AGENT_LAUNCHAGENT_MAP.get(service)
    if not label:
        logger.warning("No LaunchAgent label found for service '%s' — skipping restart", service)
        return

    plist_path = f"{_LAUNCHAGENT_PLIST_DIR}/{label}.plist"
    try:
        subprocess.run(["launchctl", "unload", plist_path], timeout=10, check=False)
        time.sleep(2)
        subprocess.run(["launchctl", "load",   plist_path], timeout=10, check=False)
        logger.info("Hard restart triggered for %s (%s)", service, label)
        notifier.send_alert(
            f"🔁 *AGENT_SILENCE hard reset*\n"
            f"Agent *{service}* was silent >{_AGENT_SILENCE_HARD_TIMEOUT_S // 60} min. "
            f"LaunchAgent reloaded automatically.",
            failure_class=f"AGENT_HARD_RESET_{service.upper()}",
        )
    except Exception as exc:
        logger.error("Failed to restart %s LaunchAgent: %s", service, exc)


def _is_market_hours() -> bool:
    """
    Return True if the current time is within regular US market hours (9:30–16:00 ET).

    Weekends always return False. Only Mon–Fri 09:30–16:00 ET return True.
    Used to suppress false-positive alerts (e.g. AGENT_SILENT) outside trading hours.

    Returns:
        True during market hours, False otherwise.
    """
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    open_minutes  = _MARKET_OPEN_H  * 60 + _MARKET_OPEN_M
    close_minutes = _MARKET_CLOSE_H * 60 + _MARKET_CLOSE_M
    current_minutes = now_et.hour * 60 + now_et.minute
    return open_minutes <= current_minutes < close_minutes


def _p99(values: List[float]) -> float:
    """
    Compute P99 of a list of float values.

    Args:
        values: Non-empty list of floats.

    Returns:
        P99 value, or 0.0 for empty lists.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    n = min(len(values), 100)
    qs = quantiles(values, n=n)
    idx = min(int(0.99 * n) - 1, len(qs) - 1)
    return qs[max(idx, 0)]


class FailureClassifier:
    """Detects and records failure conditions across the Nexus pipeline."""

    def __init__(
        self,
        db_path: str,
        stall_window_s: int,
        notifier: TelegramNotifier,
        market_hours_override: Optional[bool] = None,
    ) -> None:
        """
        Initialize the classifier.

        Args:
            db_path:                Path to the SQLite database.
            stall_window_s:         Stall detection window in seconds.
            notifier:               TelegramNotifier for alerts.
            market_hours_override:  If set, bypasses _is_market_hours() check.
                                    Used in tests to force market-hours=True/False.
        """
        self._db_path               = db_path
        self._stall_window          = stall_window_s
        self._notifier              = notifier
        self._market_hours_override = market_hours_override

    def detect_all(self) -> List[str]:
        """
        Run all seven failure detectors. Insert new events and send alerts.

        Returns:
            List of currently active failure class names (including pre-existing ones).
        """
        try:
            traces = get_recent_traces(self._db_path, window_s=900)
            stalls = get_stalled_picks(self._db_path, self._stall_window)

            self._detect_pipeline_stall(stalls)
            self._detect_executor_slow(traces)
            self._detect_brain_timeout(traces)
            self._detect_network_degraded(traces)
            self._detect_agent_silent(traces)
            self._detect_completion_rate_low(traces)
            self._detect_data_stale(traces)

            return get_active_failures(self._db_path)

        except Exception as exc:
            logger.error("detect_all failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Individual detectors
    # ------------------------------------------------------------------

    def _detect_pipeline_stall(self, stalls: List[Dict[str, Any]]) -> None:
        """
        Detect picks that have not progressed in stall_window_s seconds.
        Only fires during market hours — stalls after close are expected.

        Args:
            stalls: Pre-fetched list of stalled picks from database.
        """
        if not _is_market_hours():
            resolve_failure(self._db_path, "PIPELINE_STALL")
            return

        if stalls:
            detail = (
                f"{len(stalls)} pick(s) stalled: "
                + ", ".join(f"{s['ticker']}@{s['last_hop']}" for s in stalls[:3])
            )
            insert_failure_event(self._db_path, "PIPELINE_STALL", detail, stalls[0]["trace_id"])
            self._notifier.send_alert(
                f"🛑 *PIPELINE_STALL detected*\n{detail}\nPick(s) have not progressed in >{self._stall_window}s.",
                failure_class="PIPELINE_STALL",
            )
        else:
            resolve_failure(self._db_path, "PIPELINE_STALL")

    def _detect_executor_slow(self, traces: List[Dict[str, Any]]) -> None:
        """
        Detect elevated Alpaca execution latency.

        Fires if >3 of the last 10 alpaca_submitted→alpaca_confirmed hops
        have latency exceeding 3000ms.

        Args:
            traces: Recent trace hops.
        """
        # Build per-trace timestamps for alpaca hops
        submitted: Dict[str, float] = {}
        latencies: List[float] = []

        for row in traces:
            tid = row["trace_id"]
            if row["hop"] == "alpaca_submitted":
                submitted[tid] = row["ts"]
            elif row["hop"] == _FINAL_HOP and tid in submitted:
                latency_ms = (row["ts"] - submitted[tid]) * 1000
                latencies.append(latency_ms)

        recent = latencies[-10:]
        slow_count = sum(1 for lat in recent if lat > 3000)

        if slow_count >= 3:
            avg_lat = sum(recent) / len(recent)
            insert_failure_event(
                self._db_path,
                "EXECUTOR_SLOW",
                f"{slow_count}/{len(recent)} alpaca hops >3000ms (avg={avg_lat:.0f}ms)",
            )
            self._notifier.send_alert(
                f"⚡ *EXECUTOR_SLOW detected*\n"
                f"{slow_count} of last {len(recent)} Alpaca hops exceeded 3000ms.\n"
                f"Average latency: {avg_lat:.0f}ms",
                failure_class="EXECUTOR_SLOW",
            )
        else:
            resolve_failure(self._db_path, "EXECUTOR_SLOW")

    def _detect_brain_timeout(self, traces: List[Dict[str, Any]]) -> None:
        """
        Detect OMNI synthesis timeouts (started but not completed after 6 minutes).

        Args:
            traces: Recent trace hops.
        """
        now = time.time()
        starts: Dict[str, float] = {}
        completed: Set[str] = set()

        for row in traces:
            tid = row["trace_id"]
            if row["hop"] == "omni_started":
                starts[tid] = row["ts"]
            elif row["hop"] == "omni_completed":
                completed.add(tid)

        timed_out = [
            tid for tid, ts in starts.items()
            if tid not in completed and (now - ts) > _BRAIN_TIMEOUT_S
        ]

        if timed_out:
            insert_failure_event(
                self._db_path,
                "BRAIN_TIMEOUT",
                f"{len(timed_out)} OMNI synthesis timeout(s): {timed_out[:3]}",
                timed_out[0],
            )
            self._notifier.send_alert(
                f"🧠 *BRAIN_TIMEOUT detected*\n"
                f"{len(timed_out)} OMNI synthesis job(s) exceeded {_BRAIN_TIMEOUT_S}s without completing.",
                failure_class="BRAIN_TIMEOUT",
            )
        else:
            resolve_failure(self._db_path, "BRAIN_TIMEOUT")

    def _detect_network_degraded(self, traces: List[Dict[str, Any]]) -> None:
        """
        Detect sustained high inter-hop latency across consecutive picks.

        Fires if P99 inter-hop latency exceeds 2000ms across 3+ consecutive picks.

        Args:
            traces: Recent trace hops.
        """
        # Build per-trace inter-hop gap sequences
        by_trace: Dict[str, List[float]] = {}
        for row in sorted(traces, key=lambda r: r["ts"]):
            tid = row["trace_id"]
            if tid not in by_trace:
                by_trace[tid] = []
            by_trace[tid].append(row["ts"])

        high_latency_picks = 0
        for hops in by_trace.values():
            if len(hops) < 2:
                continue
            gaps_ms = [(hops[i] - hops[i - 1]) * 1000 for i in range(1, len(hops))]
            if _p99(gaps_ms) > 2000:
                high_latency_picks += 1

        if high_latency_picks >= 3:
            insert_failure_event(
                self._db_path,
                "NETWORK_DEGRADED",
                f"{high_latency_picks} picks had P99 inter-hop latency >2000ms",
            )
            self._notifier.send_alert(
                f"🌐 *NETWORK_DEGRADED detected*\n"
                f"{high_latency_picks} picks had P99 inter-hop latency exceeding 2000ms.",
                failure_class="NETWORK_DEGRADED",
            )
        else:
            resolve_failure(self._db_path, "NETWORK_DEGRADED")

    def _detect_agent_silent(self, traces: List[Dict[str, Any]]) -> None:
        """
        Detect agents that have emitted zero hops in the last 15-minute window.
        Only fires during market hours — silence after close is expected.

        Grace period: do not fire AGENT_SILENT in the first 30 minutes after market
        open (09:30–10:00 ET). Agents need time to receive their first Axiom pool
        push and process picks before any hops appear in the pipeline. Firing at
        market open is a guaranteed false positive every day.

        Args:
            traces: Recent trace hops (default window covers 15 minutes).
        """
        in_market = (
            self._market_hours_override
            if self._market_hours_override is not None
            else _is_market_hours()
        )
        if not in_market:
            resolve_failure(self._db_path, "AGENT_SILENT")
            return

        # Grace period: suppress AGENT_SILENT for first 30 min after market open.
        # Skip grace period when market_hours_override is set (test/forced mode).
        if self._market_hours_override is None:
            now_et = datetime.now(_ET)
            open_minutes = _MARKET_OPEN_H * 60 + _MARKET_OPEN_M
            current_minutes = now_et.hour * 60 + now_et.minute
            if current_minutes < open_minutes + 30:   # before 10:00 AM ET
                logger.debug(
                    "AGENT_SILENT suppressed — market open grace period (%d min remaining)",
                    (open_minutes + 30) - current_minutes,
                )
                return

        window_cutoff = time.time() - 900   # 15 minutes
        recent_services = {
            row["service"]
            for row in traces
            if row["ts"] >= window_cutoff and row["service"] in _AGENT_SERVICES
        }

        silent = _AGENT_SERVICES - recent_services
        now = time.time()

        if silent:
            insert_failure_event(
                self._db_path,
                "AGENT_SILENT",
                f"Silent agents (no hops in 15min): {sorted(silent)}",
            )
            self._notifier.send_alert(
                f"🔇 *AGENT_SILENT detected*\n"
                f"Agent(s) with zero activity in last 15 minutes: {', '.join(sorted(silent))}",
                failure_class="AGENT_SILENT",
            )

            # Cipher fix: hard timeout — if silence exceeds threshold, force restart
            for svc in silent:
                if svc not in _agent_silence_since:
                    _agent_silence_since[svc] = now   # start tracking silence onset
                silent_duration = now - _agent_silence_since[svc]
                if (silent_duration >= _AGENT_SILENCE_HARD_TIMEOUT_S
                        and svc not in _agent_hard_reset_fired):
                    _agent_hard_reset_fired.add(svc)
                    logger.critical(
                        "AGENT_SILENCE hard timeout: %s silent %.0f min — attempting restart",
                        svc, silent_duration / 60,
                    )
                    _restart_agent_service(svc, self._notifier)
        else:
            # All agents active — clear silence tracking for any that recovered
            for svc in list(_agent_silence_since.keys()):
                if svc not in silent:
                    del _agent_silence_since[svc]
                    _agent_hard_reset_fired.discard(svc)
            resolve_failure(self._db_path, "AGENT_SILENT")

    def _detect_completion_rate_low(self, traces: List[Dict[str, Any]]) -> None:
        """
        Detect a low pick completion rate in recent history.
        Only fires during market hours — stale completion data after close is expected.

        Fires if fewer than 80% of the last 10 picks reached alpaca_confirmed.

        Args:
            traces: Recent trace hops.
        """
        if not _is_market_hours():
            resolve_failure(self._db_path, "COMPLETION_RATE_LOW")
            return

        # Find last 10 unique picks
        seen: Dict[str, bool] = {}
        for row in sorted(traces, key=lambda r: r["ts"]):
            tid = row["trace_id"]
            if tid not in seen:
                seen[tid] = False
            if row["hop"] == _FINAL_HOP:
                seen[tid] = True

        recent_10 = list(seen.items())[-10:]
        if len(recent_10) < 5:
            # Not enough data to classify
            return

        completed = sum(1 for _, done in recent_10 if done)
        rate = completed / len(recent_10)

        if rate < 0.80:
            insert_failure_event(
                self._db_path,
                "COMPLETION_RATE_LOW",
                f"Only {completed}/{len(recent_10)} recent picks completed ({rate:.0%})",
            )
            self._notifier.send_alert(
                f"📉 *COMPLETION_RATE_LOW detected*\n"
                f"{completed} of last {len(recent_10)} picks completed ({rate:.0%}).\n"
                f"Target: ≥80%",
                failure_class="COMPLETION_RATE_LOW",
            )
        else:
            resolve_failure(self._db_path, "COMPLETION_RATE_LOW")

    def _detect_data_stale(self, traces: List[Dict[str, Any]]) -> None:
        """
        Detect a >15 minute gap in trace activity (possible data feed issue).

        Args:
            traces: Recent trace hops sorted by ts.
        """
        if not traces:
            return   # Empty is not a stale data condition (might be quiet market)

        sorted_ts = sorted(row["ts"] for row in traces)
        max_gap = max(
            (sorted_ts[i] - sorted_ts[i - 1]) for i in range(1, len(sorted_ts))
        ) if len(sorted_ts) > 1 else 0.0

        if max_gap > 900:   # 15 minutes
            insert_failure_event(
                self._db_path,
                "DATA_STALE",
                f"Trace gap of {max_gap / 60:.1f} minutes detected — possible data feed issue",
            )
            self._notifier.send_alert(
                f"📡 *DATA_STALE detected*\n"
                f"No pipeline activity for {max_gap / 60:.1f} minutes.\n"
                f"Possible ORACLE or data feed outage.",
                failure_class="DATA_STALE",
            )
        else:
            resolve_failure(self._db_path, "DATA_STALE")
