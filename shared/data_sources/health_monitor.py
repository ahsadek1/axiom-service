"""
health_monitor.py — Data Source Health Monitor
===============================================
Probes every external data source every 5 minutes.
Maintains state machine: UNKNOWN→HEALTHY→DEGRADED→FAILED→RECOVERING→HEALTHY
Triggers fallback manager on status changes.
Alerts Ahmed on every transition.

Never silent. Never guessing. Always measuring.
"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .registry import (
    ALL_SOURCES, DataSource, SourceStatus, SourceTier,
    SourceCategory, get_primary_sources, get_by_category,
)

log = logging.getLogger("nexus.data_sources.monitor")

DB_PATH  = os.getenv("PRIME_V2_DB",
           "/Users/ahmedsadek/nexus/data/prime_v2.db")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.getenv("AHMED_CHAT_ID", "8573754783")

PROBE_INTERVAL_S = 300   # 5 minutes
FAST_PROBE_S     = 60    # 1 minute when source is FAILED (faster recovery detection)


# ── Persistent state ───────────────────────────────────────────────────────────

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS data_source_status (
            source_id        TEXT PRIMARY KEY,
            name             TEXT,
            category         TEXT,
            tier             TEXT,
            status           TEXT DEFAULT 'UNKNOWN',
            last_probe_at    TEXT,
            last_success_at  TEXT,
            last_failure_at  TEXT,
            consecutive_ok   INTEGER DEFAULT 0,
            consecutive_fail INTEGER DEFAULT 0,
            avg_latency_ms   REAL DEFAULT 0,
            error_rate_1h    REAL DEFAULT 0,
            active           INTEGER DEFAULT 1,
            updated_at       TEXT
        );
        CREATE TABLE IF NOT EXISTS data_source_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id   TEXT,
            event       TEXT,
            from_status TEXT,
            to_status   TEXT,
            latency_ms  REAL,
            detail      TEXT,
            ts          REAL,
            created_at  TEXT
        );
    """)
    # Seed all sources
    now = datetime.now(timezone.utc).isoformat()
    for sid, source in ALL_SOURCES.items():
        conn.execute("""
            INSERT OR IGNORE INTO data_source_status
            (source_id, name, category, tier, status, updated_at)
            VALUES (?,?,?,?,?,?)
        """, (sid, source.name, source.category.value,
              source.tier.value, SourceStatus.UNKNOWN.value, now))
    conn.commit()
    conn.close()


def _save_status(
    source_id:        str,
    status:           SourceStatus,
    latency_ms:       float,
    consecutive_ok:   int,
    consecutive_fail: int,
    detail:           str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        success = status in (SourceStatus.HEALTHY, SourceStatus.RECOVERING)
        conn.execute("""
            UPDATE data_source_status SET
                status           = ?,
                last_probe_at    = ?,
                last_success_at  = CASE WHEN ? THEN ? ELSE last_success_at END,
                last_failure_at  = CASE WHEN NOT ? THEN ? ELSE last_failure_at END,
                consecutive_ok   = ?,
                consecutive_fail = ?,
                avg_latency_ms   = (avg_latency_ms * 0.8 + ? * 0.2),
                updated_at       = ?
            WHERE source_id = ?
        """, (
            status.value, now,
            success, now,
            success, now,
            consecutive_ok, consecutive_fail,
            latency_ms, now,
            source_id,
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("DB save failed for %s: %s", source_id, exc)


def _log_event(
    source_id:   str,
    event:       str,
    from_status: SourceStatus,
    to_status:   SourceStatus,
    latency_ms:  float,
    detail:      str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("""
            INSERT INTO data_source_events
            (source_id, event, from_status, to_status, latency_ms, detail, ts, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            source_id, event,
            from_status.value, to_status.value,
            latency_ms, detail,
            time.time(), now,
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.debug("Event log failed: %s", exc)


# ── Notification ───────────────────────────────────────────────────────────────

def _notify(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        log.info("ALERT (no TG): %s", msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg[:4000], "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        log.info("ALERT (TG failed): %s", msg)


def _alert_transition(
    source:      DataSource,
    from_status: SourceStatus,
    to_status:   SourceStatus,
    detail:      str = "",
) -> None:
    """Alert Ahmed on every status transition."""
    emoji = {
        SourceStatus.HEALTHY:    "✅",
        SourceStatus.DEGRADED:   "⚠️",
        SourceStatus.FAILED:     "🔴",
        SourceStatus.RECOVERING: "🔄",
    }.get(to_status, "ℹ️")

    action = {
        SourceStatus.FAILED:     "Switched to fallback automatically.",
        SourceStatus.RECOVERING: "Primary recovering — monitoring.",
        SourceStatus.HEALTHY:    "Switched back to primary.",
        SourceStatus.DEGRADED:   "Performance degraded — watching.",
    }.get(to_status, "")

    msg = (
        f"{emoji} <b>DATA SOURCE: {source.name}</b>\n"
        f"Category: {source.category.value}\n"
        f"Status: {from_status.value} → {to_status.value}\n"
        f"{action}\n"
        f"{detail}"
    )
    _notify(msg)
    log.warning("DATA SOURCE TRANSITION: %s %s→%s %s",
                source.source_id, from_status.value, to_status.value, detail)


# ── Probe logic ────────────────────────────────────────────────────────────────

def _probe_source(source: DataSource) -> tuple[bool, float, str]:
    """
    Probe one data source.
    Returns (success, latency_ms, detail).
    """
    probe = source.probe
    start = time.time()

    try:
        r = requests.request(
            method  = probe.method,
            url     = probe.url,
            headers = probe.headers,
            params  = probe.params,
            timeout = probe.timeout_s,
        )
        latency_ms = (time.time() - start) * 1000

        if r.status_code != probe.expect_status:
            return False, latency_ms, f"HTTP {r.status_code}"

        if probe.expect_key:
            try:
                data = r.json()
                if probe.expect_key not in data:
                    return False, latency_ms, f"Missing key: {probe.expect_key}"
            except Exception:
                return False, latency_ms, "Invalid JSON response"

        return True, latency_ms, f"OK ({latency_ms:.0f}ms)"

    except requests.exceptions.Timeout:
        latency_ms = probe.timeout_s * 1000
        return False, latency_ms, f"Timeout after {probe.timeout_s}s"
    except requests.exceptions.ConnectionError as exc:
        latency_ms = (time.time() - start) * 1000
        return False, latency_ms, f"Connection error: {str(exc)[:60]}"
    except Exception as exc:
        latency_ms = (time.time() - start) * 1000
        return False, latency_ms, f"Error: {str(exc)[:60]}"


def _evaluate_new_status(
    source:       DataSource,
    success:      bool,
    latency_ms:   float,
    consec_ok:    int,
    consec_fail:  int,
) -> SourceStatus:
    """Determine new status from probe result + counters."""
    current = source.status

    if not success:
        if consec_fail >= 2:
            return SourceStatus.FAILED
        return SourceStatus.DEGRADED if current == SourceStatus.HEALTHY else current

    # Success path
    if latency_ms > source.failed_latency_s * 1000:
        return SourceStatus.DEGRADED

    if current == SourceStatus.FAILED:
        if consec_ok >= source.recover_consecutive:
            return SourceStatus.RECOVERING
        return SourceStatus.FAILED

    if current == SourceStatus.RECOVERING:
        if consec_ok >= source.healthy_consecutive:
            return SourceStatus.HEALTHY
        return SourceStatus.RECOVERING

    if latency_ms > source.degraded_latency_s * 1000:
        return SourceStatus.DEGRADED

    return SourceStatus.HEALTHY


# ── Per-source monitor thread ──────────────────────────────────────────────────

class SourceMonitor:
    """Self-sovereign monitor for one data source."""

    def __init__(self, source: DataSource, fallback_manager=None):
        self.source           = source
        self.fallback_manager = fallback_manager
        self.consec_ok        = 0
        self.consec_fail      = 0
        self._thread          = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target = self._run,
            daemon = True,
            name   = f"dsm-{self.source.source_id}",
        )
        self._thread.start()
        log.info("Monitor started: %s", self.source.source_id)

    def _run(self) -> None:
        # Initial probe immediately
        time.sleep(2)
        while True:
            try:
                self._probe_and_update()
            except Exception as exc:
                log.error("Monitor error for %s: %s", self.source.source_id, exc)

            # Faster probing when failed
            interval = (
                FAST_PROBE_S
                if self.source.status == SourceStatus.FAILED
                else PROBE_INTERVAL_S
            )
            time.sleep(interval)

    def _probe_and_update(self) -> None:
        success, latency_ms, detail = _probe_source(self.source)

        if success:
            self.consec_ok   += 1
            self.consec_fail  = 0
        else:
            self.consec_fail += 1
            self.consec_ok    = 0

        old_status = self.source.status
        new_status = _evaluate_new_status(
            self.source, success, latency_ms,
            self.consec_ok, self.consec_fail,
        )

        # Update source object
        self.source.status = new_status

        # Save to DB
        _save_status(
            self.source.source_id, new_status,
            latency_ms, self.consec_ok, self.consec_fail, detail,
        )

        # Handle transitions
        if old_status != new_status:
            _log_event(
                self.source.source_id, "TRANSITION",
                old_status, new_status, latency_ms, detail,
            )
            _alert_transition(self.source, old_status, new_status, detail)

            # Trigger fallback on failure
            if new_status == SourceStatus.FAILED and self.fallback_manager:
                self.fallback_manager.on_source_failed(self.source)

            # Restore primary on recovery
            if new_status == SourceStatus.HEALTHY and self.fallback_manager:
                self.fallback_manager.on_source_recovered(self.source)

        log.debug("%s: %s %.0fms consec_ok=%d consec_fail=%d",
                  self.source.source_id, new_status.value,
                  latency_ms, self.consec_ok, self.consec_fail)


# ── Start all monitors ─────────────────────────────────────────────────────────

_monitors: dict[str, SourceMonitor] = {}


def start_all_monitors(fallback_manager=None) -> dict[str, SourceMonitor]:
    """Start monitors for all registered data sources."""
    global _monitors
    init_db()

    for source_id, source in ALL_SOURCES.items():
        monitor = SourceMonitor(source, fallback_manager)
        monitor.start()
        _monitors[source_id] = monitor

    log.info("Data source monitors started: %d sources", len(_monitors))
    return _monitors


def get_all_status() -> dict:
    """Get current status of all data sources."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM data_source_status ORDER BY category, tier
        """).fetchall()
        conn.close()

        by_category: dict = {}
        for row in rows:
            cat = row["category"]
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(dict(row))

        healthy = sum(1 for r in rows if r["status"] == "HEALTHY")
        failed  = sum(1 for r in rows if r["status"] == "FAILED")
        total   = len(rows)

        return {
            "total":       total,
            "healthy":     healthy,
            "failed":      failed,
            "degraded":    sum(1 for r in rows if r["status"] == "DEGRADED"),
            "by_category": by_category,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_recent_events(hours: int = 24) -> list[dict]:
    """Get recent status transition events."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM data_source_events
            WHERE ts > ?
            ORDER BY ts DESC
            LIMIT 100
        """, (time.time() - hours * 3600,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
