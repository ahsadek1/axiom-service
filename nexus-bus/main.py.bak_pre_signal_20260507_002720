#!/usr/bin/env python3
"""
main.py — Nexus Agent Message Bus v2

HTTP relay service for inter-agent communication.
SQLite-backed for persistence across restarts.

Endpoints:
  POST /send              — Route a message to a recipient's inbox
  GET  /inbox/{agent}     — Consume queued messages (read + remove in one transaction)
  GET  /status            — Queue depth per agent
  GET  /health            — Service health + DB stats

Design principles:
  - No external dependencies (stdlib only)
  - Messages written to SQLite before 200 is returned to sender
  - Consume-on-read: GET /inbox atomically reads and removes messages
  - Messages TTL: auto-expire 72h if never read, deleted 24h after delivery
  - Background cleanup thread runs every 60 minutes
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional

# ── Configuration ─────────────────────────────────────────────────────────────
DB_PATH = "/Users/ahmedsadek/nexus/data/message_bus.db"
HOST = "0.0.0.0"
PORT = 9999
VERSION = "2.0.0"
STARTUP_TIME = datetime.now(timezone.utc).isoformat()

# TTL settings
DELIVERED_TTL_HOURS = 24   # Delete delivered messages after this many hours
NEVER_READ_TTL_HOURS = 72  # Expire unread messages after this many hours
CLEANUP_INTERVAL_S = 3600  # Run cleanup every 60 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [nexus-bus] %(message)s",
)
logger = logging.getLogger("nexus-bus")


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """
    Open a SQLite connection with WAL mode and row factory.

    Returns:
        Configured sqlite3.Connection.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """
    Create the messages table and indexes if they don't exist.
    Safe to call on every startup.
    """
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id           TEXT PRIMARY KEY,
            from_agent   TEXT NOT NULL,
            to_agent     TEXT NOT NULL,
            message      TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            delivered    INTEGER NOT NULL DEFAULT 0,
            delivered_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_to_delivered
            ON messages(to_agent, delivered);
        CREATE INDEX IF NOT EXISTS idx_created
            ON messages(created_at);
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized: %s", DB_PATH)


def send_message(from_agent: str, to_agent: str, message: str) -> str:
    """
    Persist a new message to the database.

    Args:
        from_agent: Sender agent name.
        to_agent:   Recipient agent name.
        message:    Message body string.

    Returns:
        UUID string assigned to this message.

    Raises:
        sqlite3.Error: If the DB write fails. Callers must handle this.
    """
    msg_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO messages (id, from_agent, to_agent, message, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg_id, from_agent, to_agent, message, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


def consume_inbox(agent: str) -> List[Dict[str, Any]]:
    """
    Atomically read and mark-delivered all pending messages for an agent.

    Args:
        agent: Recipient agent name (case-insensitive normalized to lower).

    Returns:
        List of message dicts with id, from, message, timestamp fields.

    Raises:
        sqlite3.Error: If the DB operation fails.
    """
    agent = agent.lower().strip()
    delivered_at = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, from_agent, message, created_at FROM messages "
            "WHERE to_agent = ? AND delivered = 0 "
            "ORDER BY created_at ASC",
            (agent,),
        ).fetchall()

        if rows:
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE messages SET delivered = 1, delivered_at = ? "
                f"WHERE id IN ({placeholders})",
                [delivered_at] + ids,
            )
            conn.commit()

        return [
            {
                "id": r["id"],
                "from": r["from_agent"],
                "message": r["message"],
                "timestamp": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_queue_depths() -> Dict[str, int]:
    """
    Return count of pending (undelivered) messages per agent.

    Returns:
        Dict mapping agent name → pending message count.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT to_agent, COUNT(*) as cnt FROM messages "
            "WHERE delivered = 0 GROUP BY to_agent"
        ).fetchall()
        return {r["to_agent"]: r["cnt"] for r in rows}
    finally:
        conn.close()


def get_db_stats() -> Dict[str, Any]:
    """
    Return database statistics for the /health endpoint.

    Returns:
        Dict with total_messages, pending, delivered_today counts and db_ok flag.
    """
    try:
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE delivered = 0"
        ).fetchone()[0]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        delivered_today = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE delivered = 1 AND delivered_at LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]
        conn.close()
        return {
            "ok": True,
            "total_messages": total,
            "pending": pending,
            "delivered_today": delivered_today,
        }
    except Exception as e:
        logger.error("DB stats failed: %s", e)
        return {"ok": False, "error": str(e)}


# ── Background cleanup ────────────────────────────────────────────────────────

def _cleanup_loop() -> None:
    """
    Background thread: expire stale messages on a fixed interval.

    Never-read messages older than NEVER_READ_TTL_HOURS are marked delivered.
    Delivered messages older than DELIVERED_TTL_HOURS are deleted.
    """
    while True:
        time.sleep(CLEANUP_INTERVAL_S)
        try:
            _run_cleanup()
        except Exception as e:
            logger.error("Cleanup thread error: %s", e)


def _run_cleanup() -> None:
    """
    Execute one cleanup pass: expire and delete stale messages.
    Called by the background thread and directly in tests.
    """
    now = datetime.now(timezone.utc)
    never_read_cutoff = (now - timedelta(hours=NEVER_READ_TTL_HOURS)).isoformat()
    delivered_cutoff = (now - timedelta(hours=DELIVERED_TTL_HOURS)).isoformat()
    expired_at = now.isoformat()

    conn = get_db()
    try:
        # Expire never-read messages older than TTL
        cur = conn.execute(
            "UPDATE messages SET delivered = 1, delivered_at = ? "
            "WHERE delivered = 0 AND created_at < ?",
            (expired_at, never_read_cutoff),
        )
        expired = cur.rowcount

        # Delete delivered messages past retention window
        cur = conn.execute(
            "DELETE FROM messages WHERE delivered = 1 AND delivered_at < ?",
            (delivered_cutoff,),
        )
        deleted = cur.rowcount

        conn.commit()
        if expired or deleted:
            logger.info("Cleanup: expired=%d deleted=%d", expired, deleted)
    finally:
        conn.close()


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BusHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Nexus message bus."""

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress default BaseHTTPRequestHandler access logs; use our logger."""
        logger.debug("HTTP %s %s %s", *args)

    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        """
        Send a JSON response with the given HTTP status code.

        Args:
            status: HTTP status code.
            body:   Dict to serialize as JSON response body.
        """
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_body(self) -> Optional[Dict[str, Any]]:
        """
        Read and parse the request body as JSON.

        Returns:
            Parsed dict, or None if missing or malformed.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return None
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def do_POST(self) -> None:
        """Handle POST /send."""
        if self.path != "/send":
            self._send_json(404, {"error": "not found"})
            return

        body = self._read_body()
        if not body:
            self._send_json(400, {"ok": False, "error": "invalid or empty JSON body"})
            return

        from_agent = str(body.get("from", "")).strip()
        to_agent = str(body.get("to", "")).strip()
        message = str(body.get("message", "")).strip()

        if not to_agent or not message:
            self._send_json(
                400,
                {"ok": False, "error": "missing required fields: 'to' and 'message'"},
            )
            return

        if not from_agent:
            from_agent = "unknown"

        try:
            msg_id = send_message(from_agent, to_agent.lower(), message)
            logger.info("%s → %s: %s", from_agent, to_agent, message[:80])
            self._send_json(200, {"ok": True, "message_id": msg_id})
        except Exception as e:
            logger.error("DB write failed for %s → %s: %s", from_agent, to_agent, e)
            self._send_json(500, {"ok": False, "error": "database write failed"})

    def do_GET(self) -> None:
        """Handle GET /inbox/{agent}, GET /status, GET /health."""
        if self.path.startswith("/inbox/"):
            self._handle_inbox()
        elif self.path == "/status":
            self._handle_status()
        elif self.path == "/health":
            self._handle_health()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_inbox(self) -> None:
        """Consume and return all pending messages for an agent."""
        agent = self.path.split("/inbox/")[1].strip("/").strip()
        if not agent:
            self._send_json(400, {"error": "agent name required"})
            return
        try:
            messages = consume_inbox(agent)
            self._send_json(200, {"agent": agent.lower(), "messages": messages})
        except Exception as e:
            logger.error("Inbox read failed for %s: %s", agent, e)
            self._send_json(500, {"error": "database read failed"})

    def _handle_status(self) -> None:
        """Return queue depth per agent."""
        try:
            depths = get_queue_depths()
            self._send_json(200, {"ok": True, "pending": depths})
        except Exception as e:
            logger.error("Status query failed: %s", e)
            self._send_json(500, {"error": "database query failed"})

    def _handle_health(self) -> None:
        """Return service health and DB statistics."""
        stats = get_db_stats()
        status = 200 if stats["ok"] else 500
        self._send_json(
            status,
            {
                "ok": stats["ok"],
                "service": "nexus-message-bus",
                "version": VERSION,
                "uptime_since": STARTUP_TIME,
                "db": stats,
            },
        )


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server with per-request thread spawning for concurrent handling."""
    daemon_threads = True


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Initialize the database, start cleanup thread, and serve forever."""
    init_db()

    cleanup_thread = threading.Thread(
        target=_cleanup_loop, daemon=True, name="bus-cleanup"
    )
    cleanup_thread.start()
    logger.info("Cleanup thread started (interval: %ds)", CLEANUP_INTERVAL_S)

    server = ThreadedHTTPServer((HOST, PORT), BusHandler)
    logger.info(
        "Nexus Message Bus v%s listening on %s:%d (db: %s)",
        VERSION, HOST, PORT, DB_PATH,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
