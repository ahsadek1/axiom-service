"""
dedup.py — Deduplication and batching engine for Alert Broker.

Dedup window: 60 seconds per dedup_key.
Batch window: 10 seconds for WARNING/INFO (CRITICAL bypasses immediately).
All state in SQLite — survives broker restarts.
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("alert_broker.dedup")

DB_PATH: str = os.getenv("ALERT_BROKER_DB", "/Users/ahmedsadek/nexus/data/alert_broker.db")
DEDUP_WINDOW_S: int = 300   # 5 minutes per dedup policy
BATCH_WINDOW_S: int = 30    # 30s batch window (SOVEREIGN directive 2026-05-05)


@dataclass
class AlertIn:
    source: str
    level: str          # CRITICAL | WARNING | INFO
    title: str
    body: str
    dedup_key: str
    targets: List[str]
    received_at: float = field(default_factory=time.time)


@dataclass
class BatchEntry:
    alert: AlertIn
    queued_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dedup_log (
            dedup_key   TEXT    NOT NULL,
            sent_at     REAL    NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_key ON dedup_log(dedup_key)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            date        TEXT    PRIMARY KEY,
            sent        INTEGER NOT NULL DEFAULT 0,
            suppressed  INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class DedupEngine:
    """
    Check whether an alert should be sent or suppressed.

    Uses SQLite for persistence across restarts.
    In-memory cache (dict) reduces DB hits in steady state.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, float] = {}   # dedup_key → sent_at timestamp
        self._lock = threading.Lock()
        self._load_cache()

    def _load_cache(self) -> None:
        """Load recent dedup entries from DB into memory cache."""
        try:
            with _db_lock:
                conn = _connect()
                cutoff = time.time() - DEDUP_WINDOW_S
                rows = conn.execute(
                    "SELECT dedup_key, MAX(sent_at) FROM dedup_log WHERE sent_at > ? GROUP BY dedup_key",
                    (cutoff,),
                ).fetchall()
                conn.close()
            with self._lock:
                for key, ts in rows:
                    self._cache[key] = ts
            logger.info("dedup: loaded %d active keys from DB", len(rows))
        except Exception as exc:
            logger.warning("dedup: failed to load cache: %s — starting empty", exc)

    def is_duplicate(self, dedup_key: str) -> bool:
        """
        Return True if the same dedup_key was sent within DEDUP_WINDOW_S seconds.
        Never raises.
        """
        now = time.time()
        with self._lock:
            last = self._cache.get(dedup_key)
            if last and (now - last) < DEDUP_WINDOW_S:
                return True
        return False

    def record_sent(self, dedup_key: str) -> None:
        """Record that an alert with this key was sent. Never raises."""
        now = time.time()
        with self._lock:
            self._cache[dedup_key] = now
        try:
            with _db_lock:
                conn = _connect()
                conn.execute(
                    "INSERT INTO dedup_log (dedup_key, sent_at) VALUES (?, ?)",
                    (dedup_key, now),
                )
                # Prune old entries
                conn.execute(
                    "DELETE FROM dedup_log WHERE sent_at < ?",
                    (now - DEDUP_WINDOW_S * 10,),
                )
                conn.commit()
                conn.close()
        except Exception as exc:
            logger.warning("dedup: DB record_sent failed: %s", exc)

    def increment_suppressed(self) -> None:
        """Increment today's suppressed counter. Never raises."""
        try:
            with _db_lock:
                conn = _connect()
                today = _today()
                conn.execute(
                    "INSERT INTO stats (date, sent, suppressed) VALUES (?, 0, 1) "
                    "ON CONFLICT(date) DO UPDATE SET suppressed = suppressed + 1",
                    (today,),
                )
                conn.commit()
                conn.close()
        except Exception as exc:
            logger.warning("dedup: increment_suppressed failed: %s", exc)

    def increment_sent(self) -> None:
        """Increment today's sent counter. Never raises."""
        try:
            with _db_lock:
                conn = _connect()
                today = _today()
                conn.execute(
                    "INSERT INTO stats (date, sent, suppressed) VALUES (?, 1, 0) "
                    "ON CONFLICT(date) DO UPDATE SET sent = sent + 1",
                    (today,),
                )
                conn.commit()
                conn.close()
        except Exception as exc:
            logger.warning("dedup: increment_sent failed: %s", exc)

    def get_stats(self) -> Dict[str, int]:
        """Return today's sent/suppressed counts. Never raises."""
        try:
            with _db_lock:
                conn = _connect()
                row = conn.execute(
                    "SELECT sent, suppressed FROM stats WHERE date = ?",
                    (_today(),),
                ).fetchone()
                conn.close()
            if row:
                return {"sent": row[0], "suppressed": row[1]}
        except Exception as exc:
            logger.warning("dedup: get_stats failed: %s", exc)
        return {"sent": 0, "suppressed": 0}


# ---------------------------------------------------------------------------
# Batch engine
# ---------------------------------------------------------------------------

class BatchEngine:
    """
    Hold WARNING/INFO alerts for BATCH_WINDOW_S seconds, then flush grouped.
    CRITICAL alerts bypass immediately.
    """

    def __init__(self, flush_callback) -> None:
        """
        :param flush_callback: Callable[[List[AlertIn]], None] — called with batch to send.
        """
        self._queue: List[BatchEntry] = []
        self._lock = threading.Lock()
        self._flush_cb = flush_callback
        self._thread = threading.Thread(
            target=self._batch_loop,
            daemon=True,
            name="alert-broker-batcher",
        )
        self._thread.start()

    def add(self, alert: AlertIn) -> None:
        """Add alert to batch queue. CRITICAL alerts flush immediately."""
        if alert.level == "CRITICAL":
            # Bypass batch — flush immediately in caller thread
            try:
                self._flush_cb([alert])
            except Exception as exc:
                logger.error("batcher: immediate flush failed: %s", exc)
            return

        with self._lock:
            self._queue.append(BatchEntry(alert=alert))

    def _batch_loop(self) -> None:
        """Background thread: flushes queue every BATCH_WINDOW_S seconds."""
        while True:
            try:
                time.sleep(BATCH_WINDOW_S)
                self._flush()
            except Exception as exc:
                logger.error("batcher: loop error: %s", exc)

    def _flush(self) -> None:
        with self._lock:
            if not self._queue:
                return
            batch = [e.alert for e in self._queue]
            self._queue.clear()

        # Group by source for compact formatting
        by_source: Dict[str, List[AlertIn]] = {}
        for alert in batch:
            by_source.setdefault(alert.source, []).append(alert)

        for source, alerts in by_source.items():
            if len(alerts) == 1:
                self._flush_cb([alerts[0]])
            else:
                # Merge into one combined alert
                titles = " | ".join(a.title for a in alerts)
                combined = AlertIn(
                    source=source,
                    level=max(a.level for a in alerts),   # escalate to highest
                    title=f"[{len(alerts)} alerts] {titles[:200]}",
                    body="\n".join(f"• {a.title}: {a.body}" for a in alerts if a.body),
                    dedup_key=alerts[0].dedup_key,
                    targets=alerts[0].targets,
                )
                self._flush_cb([combined])

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)
