"""
db.py — NSP Telemetry Database (WAL SQLite, single writer thread).

Architecture:
- One dedicated writer thread dequeues all SQL writes — no concurrent writes ever.
- WAL mode enables concurrent readers without blocking the writer.
- Per-thread read connections via threading.local() for safe concurrent reads.
- Every telemetry record carries expires_at = created_at + 4h.
- Purge job runs every 30 minutes, deleting WHERE expires_at < NOW().
- Write lag is measured per job (enqueue time → completion time) and exposed
  for the self-preservation gate in NSP.

Public API:
    init_db()               — create schema, set WAL + NORMAL sync
    start_writer_thread()   — launch background writer
    stop_writer_thread()    — graceful shutdown (drains queue)
    start_purge_job()       — launch background purge scheduler
    enqueue_write()         — fire-and-forget write (telemetry)
    enqueue_write_sync()    — blocking write, raises on error (state/intervention)
    read_db()               — execute SELECT, return list of sqlite3.Row
    get_last_write_lag()    — last measured write lag in seconds
"""

import logging
import queue
import sqlite3
import threading
import time
from typing import Any, List, Optional, Tuple

import config

logger = logging.getLogger("nsp.db")

# ---------------------------------------------------------------------------
# Write job dataclass
# ---------------------------------------------------------------------------

class _WriteJob:
    """A single SQL write job queued to the writer thread."""

    __slots__ = ("sql", "params", "done", "error", "enqueue_time", "fire_and_forget")

    def __init__(
        self,
        sql: str,
        params: Tuple[Any, ...],
        fire_and_forget: bool = True,
    ) -> None:
        """Initialise a write job.

        Args:
            sql: SQL statement to execute.
            params: Bound parameters for the statement.
            fire_and_forget: If True, caller does not wait for completion.
        """
        self.sql: str = sql
        self.params: Tuple[Any, ...] = params
        self.done: threading.Event = threading.Event()
        self.error: Optional[Exception] = None
        self.enqueue_time: float = time.monotonic()
        self.fire_and_forget: bool = fire_and_forget


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_write_queue: queue.Queue = queue.Queue(maxsize=2000)
_writer_thread: Optional[threading.Thread] = None
_purge_thread: Optional[threading.Thread] = None
_stop_event: threading.Event = threading.Event()
_thread_local: threading.local = threading.local()

# Last measured write lag (seconds) — read by self-preservation gate
_last_write_lag: float = 0.0
_write_lag_lock: threading.Lock = threading.Lock()

# Schema version — bump when adding columns
_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL: str = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

-- Telemetry snapshots: one JSON record per service per poll cycle
CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    service    TEXT    NOT NULL,
    poll_state TEXT    NOT NULL,
    signals    TEXT    NOT NULL,  -- JSON object of all collected signals
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL   -- created_at + 4h; purged by purge job
);
CREATE INDEX IF NOT EXISTS idx_snap_service_time
    ON telemetry_snapshots(service, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_snap_expires
    ON telemetry_snapshots(expires_at);

-- NSP persistent key-value state (calibration mode, freeze mode, etc.)
CREATE TABLE IF NOT EXISTS nsp_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Intervention log: written BEFORE every autonomous action
CREATE TABLE IF NOT EXISTS intervention_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT    NOT NULL,
    service        TEXT    NOT NULL,
    failure_class  TEXT    NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    action         TEXT    NOT NULL,
    outcome        TEXT,
    state_snapshot TEXT,           -- JSON snapshot of system state at intervention time
    created_at     REAL    NOT NULL,
    expires_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intervention_service_time
    ON intervention_log(service, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intervention_expires
    ON intervention_log(expires_at);

-- Chronicle buffer: local queue for async sync to 192.168.1.42:8020
CREATE TABLE IF NOT EXISTS chronicle_buffer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payload    TEXT    NOT NULL,  -- JSON
    created_at REAL    NOT NULL,
    synced     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chronicle_unsynced
    ON chronicle_buffer(synced, created_at);
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_read_conn() -> sqlite3.Connection:
    """Return a per-thread read-only SQLite connection.

    Uses threading.local() so each thread gets its own connection.
    WAL mode allows these to coexist safely with the writer thread.

    Returns:
        sqlite3.Connection with Row factory set.
    """
    if not hasattr(_thread_local, "conn"):
        conn = sqlite3.connect(config.NSP_DB_PATH, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _thread_local.conn = conn
    return _thread_local.conn


def _writer_loop() -> None:
    """Writer thread main loop.

    Opens a single dedicated connection, dequeues _WriteJob items, and
    executes them one at a time.  Measures write lag per job and exposes
    the latest value via _last_write_lag.

    Runs until _stop_event is set AND the queue is fully drained.
    """
    global _last_write_lag

    logger.info("Writer thread started — connecting to %s", config.NSP_DB_PATH)
    try:
        conn = sqlite3.connect(config.NSP_DB_PATH, check_same_thread=True)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        logger.critical("Writer thread failed to open DB: %s", exc)
        return

    try:
        while True:
            # Exit only after stop is set AND queue is fully empty
            if _stop_event.is_set() and _write_queue.empty():
                break

            try:
                job: _WriteJob = _write_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                conn.execute(job.sql, job.params)
                conn.commit()
                lag = time.monotonic() - job.enqueue_time
                with _write_lag_lock:
                    _last_write_lag = lag
                if lag > 1.0:
                    logger.warning("DB write lag %.3fs — SQL: %s", lag, job.sql[:80])
            except sqlite3.Error as exc:
                job.error = exc
                logger.error(
                    "Writer thread DB error: %s | SQL: %.120s | params: %s",
                    exc,
                    job.sql,
                    job.params,
                )
            finally:
                if not job.fire_and_forget:
                    job.done.set()
                _write_queue.task_done()

    except Exception as exc:
        logger.critical("Writer thread crashed unexpectedly: %s", exc, exc_info=True)
    finally:
        conn.close()
        logger.info("Writer thread exited — DB connection closed")


def _purge_loop() -> None:
    """Purge scheduler: runs every PURGE_INTERVAL_S, deletes expired rows.

    TTL tables purged: telemetry_snapshots, intervention_log.
    chronicle_buffer rows are purged separately once synced.
    """
    logger.info(
        "Purge scheduler started — interval %ds, TTL %dh",
        config.PURGE_INTERVAL_S,
        config.TELEMETRY_TTL_HOURS,
    )
    while not _stop_event.is_set():
        _stop_event.wait(config.PURGE_INTERVAL_S)
        if _stop_event.is_set():
            break
        _run_purge()


def _run_purge() -> None:
    """Execute one purge cycle across all TTL tables.

    Fires fire-and-forget writes (not sync) so purge never blocks callers.
    """
    now = time.time()
    for table in ("telemetry_snapshots", "intervention_log"):
        enqueue_write(
            f"DELETE FROM {table} WHERE expires_at < ?",  # noqa: S608
            (now,),
        )
    # Also purge already-synced chronicle rows older than 24h
    cutoff_24h = now - 86400.0
    enqueue_write(
        "DELETE FROM chronicle_buffer WHERE synced = 1 AND created_at < ?",
        (cutoff_24h,),
    )
    logger.debug("Purge cycle fired at %.0f", now)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Initialise the NSP database: create schema and apply WAL mode.

    Called once at startup before the writer thread is started, so this
    runs direct SQL (not through the queue).

    Raises:
        sqlite3.Error: If schema creation fails (hard failure — NSP cannot start).
    """
    logger.info("Initialising NSP database at %s", config.NSP_DB_PATH)
    try:
        conn = sqlite3.connect(config.NSP_DB_PATH)
        conn.executescript(_SCHEMA_SQL)

        # Record schema version if not already present
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, time.time()),
            )
            conn.commit()
            logger.info("Schema version %d applied", _SCHEMA_VERSION)
        else:
            logger.info("Schema version %d already present", row[0])

        conn.close()
    except sqlite3.Error as exc:
        logger.critical("DB init failed: %s", exc)
        raise


def start_writer_thread() -> None:
    """Start the single background writer thread.

    Must be called after init_db() and before any enqueue_write() calls.
    No-op if already running.
    """
    global _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        logger.warning("Writer thread already running — skipping start")
        return
    _stop_event.clear()
    _writer_thread = threading.Thread(
        target=_writer_loop,
        name="nsp-db-writer",
        daemon=True,
    )
    _writer_thread.start()
    logger.info("Writer thread started (tid=%d)", _writer_thread.ident or -1)


def stop_writer_thread(timeout_s: float = 5.0) -> None:
    """Signal the writer thread to stop after draining the queue.

    Args:
        timeout_s: Maximum seconds to wait for the thread to exit.
    """
    logger.info("Stopping writer thread (draining queue: %d items)", _write_queue.qsize())
    _stop_event.set()
    if _writer_thread is not None:
        _writer_thread.join(timeout=timeout_s)
        if _writer_thread.is_alive():
            logger.warning("Writer thread did not exit within %.1fs", timeout_s)
        else:
            logger.info("Writer thread stopped cleanly")


def start_purge_job() -> None:
    """Start the background purge scheduler thread.

    Must be called after start_writer_thread(). No-op if already running.
    """
    global _purge_thread
    if _purge_thread is not None and _purge_thread.is_alive():
        logger.warning("Purge thread already running — skipping start")
        return
    _purge_thread = threading.Thread(
        target=_purge_loop,
        name="nsp-db-purge",
        daemon=True,
    )
    _purge_thread.start()
    logger.info("Purge scheduler started")


def enqueue_write(sql: str, params: Tuple[Any, ...] = ()) -> None:
    """Fire-and-forget SQL write. Used for telemetry inserts.

    The write is executed asynchronously by the writer thread.
    Caller does NOT wait for completion.

    Args:
        sql: SQL statement to execute.
        params: Bound parameters.
    """
    job = _WriteJob(sql, params, fire_and_forget=True)
    try:
        _write_queue.put_nowait(job)
    except queue.Full:
        logger.error(
            "Write queue full (%d items) — dropping telemetry write: %.80s",
            _write_queue.qsize(),
            sql,
        )


def enqueue_write_sync(
    sql: str,
    params: Tuple[Any, ...] = (),
    timeout_s: float = 5.0,
) -> None:
    """Blocking SQL write. Used for state/intervention persistence.

    Caller waits until the writer thread has committed the row.
    Raises on write error or timeout. MUST be used for all intervention
    state writes (written BEFORE action per spec).

    Args:
        sql: SQL statement to execute.
        params: Bound parameters.
        timeout_s: Seconds to wait for confirmation before raising.

    Raises:
        RuntimeError: If the write times out or the writer thread reports an error.
        sqlite3.Error: If the writer thread encountered a DB-level error.
    """
    job = _WriteJob(sql, params, fire_and_forget=False)
    _write_queue.put(job, timeout=timeout_s)
    completed = job.done.wait(timeout=timeout_s)
    if not completed:
        raise RuntimeError(
            f"enqueue_write_sync timed out after {timeout_s}s — SQL: {sql[:80]}"
        )
    if job.error is not None:
        raise job.error


def read_db(
    sql: str,
    params: Tuple[Any, ...] = (),
) -> List[sqlite3.Row]:
    """Execute a SELECT and return all matching rows.

    Uses a per-thread read connection (WAL mode — safe alongside writer).

    Args:
        sql: SELECT statement.
        params: Bound parameters.

    Returns:
        List of sqlite3.Row objects (access by column name or index).

    Raises:
        sqlite3.Error: On query failure (logged + re-raised).
    """
    try:
        conn = _get_read_conn()
        cursor = conn.execute(sql, params)
        return cursor.fetchall()
    except sqlite3.Error as exc:
        logger.error("read_db error: %s | SQL: %.120s", exc, sql)
        raise


def get_last_write_lag() -> float:
    """Return the most recently measured DB write lag in seconds.

    Used by the self-preservation gate: if lag > 2s, stand down all
    interventions.

    Returns:
        Write lag in seconds (0.0 before first write).
    """
    with _write_lag_lock:
        return _last_write_lag


def insert_telemetry_snapshot(
    service: str,
    poll_state: str,
    signals: str,
) -> None:
    """Insert one telemetry snapshot (fire-and-forget).

    Args:
        service: Service name (e.g. 'axiom').
        poll_state: Current polling state string ('HEALTHY', 'AMBER', 'POST_RESTART').
        signals: JSON-encoded dict of collected signal values.
    """
    now = time.time()
    expires_at = now + config.TELEMETRY_TTL_S
    enqueue_write(
        "INSERT INTO telemetry_snapshots (service, poll_state, signals, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (service, poll_state, signals, now, expires_at),
    )


def get_recent_snapshots(service: str, limit: int = 10) -> List[sqlite3.Row]:
    """Return recent non-expired snapshots for a service.

    Args:
        service: Service name.
        limit: Maximum number of rows to return.

    Returns:
        Rows ordered newest-first.
    """
    now = time.time()
    return read_db(
        "SELECT * FROM telemetry_snapshots "
        "WHERE service = ? AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT ?",
        (service, now, limit),
    )
