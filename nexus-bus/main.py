#!/usr/bin/env python3
"""Nexus SIGNAL Bus v3 — port 9999
POST /signal  GET /recv  POST /ack  GET /pending  GET /history
POST /send    GET /inbox/{agent}  GET /status  GET /health  (legacy, preserved)
"""
import json, logging, sqlite3, threading, time, uuid, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

DB_PATH    = "/Users/ahmedsadek/nexus/data/message_bus.db"
HOST, PORT = "0.0.0.0", 9999
VERSION    = "3.0.0"
BOOT_TIME  = datetime.now(timezone.utc).isoformat()

ALERT_BROKER = "http://localhost:9998/alert"
AHMED_ID     = "8573754783"

TTL_DEFAULTS     = {"P0": 5, "P1": 10, "P2": 30, "P3": 120}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
VALID_ACK_STATUS = {"received", "resolved", "escalated", "rejected"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [signal-bus] %(message)s")
log = logging.getLogger("signal-bus")
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with _db_lock, db() as conn:
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
            CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_agent, delivered);

            CREATE TABLE IF NOT EXISTS signals (
                signal_id    TEXT PRIMARY KEY,
                from_agent   TEXT NOT NULL,
                to_agent     TEXT NOT NULL,
                priority     TEXT NOT NULL,
                subject      TEXT NOT NULL,
                message      TEXT NOT NULL,
                context_json TEXT,
                ack_required INTEGER NOT NULL DEFAULT 1,
                escalate_to  TEXT,
                ttl_minutes  INTEGER NOT NULL,
                queued_at    TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                ack_status   TEXT NOT NULL DEFAULT 'pending',
                ack_by       TEXT,
                ack_at       TEXT,
                ack_note     TEXT,
                escalated_at TEXT,
                p0_alerted   INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sig_to     ON signals(to_agent, ack_status);
            CREATE INDEX IF NOT EXISTS idx_sig_expires ON signals(ack_status, expires_at);
        """)
    log.info("DB ready: %s", DB_PATH)


# ── Signal ops ────────────────────────────────────────────────────────────────

def signal_create(from_agent, to_agent, priority, subject, message,
                  context, ack_required, escalate_to, ttl_minutes):
    now     = datetime.now(timezone.utc)
    sig_id  = f"sig_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    expires = (now + timedelta(minutes=ttl_minutes)).isoformat()
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig_id, from_agent, to_agent, priority, subject, message,
             json.dumps(context) if context else None,
             1 if ack_required else 0, escalate_to, ttl_minutes,
             now.isoformat(), expires, "pending", None, None, None, None, 0)
        )
    log.info("SIGNAL %s | %s→%s [%s] %s", sig_id, from_agent, to_agent, priority, subject)
    return sig_id, now.isoformat()


def signal_recv(agent):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE to_agent=? AND ack_status='pending' "
            "ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "WHEN 'P2' THEN 2 ELSE 3 END, queued_at ASC", (agent,)
        ).fetchall()
    return [dict(r) for r in rows]


def signal_ack(signal_id, agent, status, note):
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock, db() as conn:
        row = conn.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
        if not row:
            return None, "not_found"
        if row["ack_status"] not in ("pending", "received"):
            return dict(row), "already_resolved"
        conn.execute(
            "UPDATE signals SET ack_status=?, ack_by=?, ack_at=?, ack_note=? WHERE signal_id=?",
            (status, agent, now, note, signal_id)
        )
    log.info("ACK %s | %s → %s", signal_id, agent, status)
    return {"signal_id": signal_id, "ack_status": status, "ack_at": now}, None


def signal_pending():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE ack_status IN ('pending','received') "
            "ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "WHEN 'P2' THEN 2 ELSE 3 END, queued_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def signal_history(agent, limit):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE to_agent=? OR from_agent=? "
            "ORDER BY queued_at DESC LIMIT ?", (agent, agent, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Legacy message ops ────────────────────────────────────────────────────────

def msg_send(from_agent, to_agent, message):
    mid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO messages(id,from_agent,to_agent,message,created_at) VALUES(?,?,?,?,?)",
            (mid, from_agent, to_agent.lower(), message, now)
        )
    return mid


def msg_inbox(agent):
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock, db() as conn:
        rows = conn.execute(
            "SELECT id,from_agent,message,created_at FROM messages "
            "WHERE to_agent=? AND delivered=0 ORDER BY created_at ASC", (agent.lower(),)
        ).fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            conn.execute(
                f"UPDATE messages SET delivered=1,delivered_at=? WHERE id IN ({','.join('?'*len(ids))})",
                [now] + ids
            )
    return [{"id": r["id"], "from": r["from_agent"], "message": r["message"], "timestamp": r["created_at"]} for r in rows]


def msg_depths():
    with db() as conn:
        rows = conn.execute(
            "SELECT to_agent, COUNT(*) c FROM messages WHERE delivered=0 GROUP BY to_agent"
        ).fetchall()
    return {r["to_agent"]: r["c"] for r in rows}


# ── Daemons ───────────────────────────────────────────────────────────────────

def _broker_alert(severity, subject, message):
    try:
        payload = json.dumps({"service": "signal-bus", "alert_type": subject,
                              "severity": severity, "message": message}).encode()
        req = urllib.request.Request(ALERT_BROKER, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.error("Broker alert failed: %s", e)


def _run_escalation_once():
    now = datetime.now(timezone.utc)
    with db() as conn:
        expired = conn.execute(
            "SELECT * FROM signals WHERE ack_status='pending' AND ack_required=1 AND expires_at<?",
            (now.isoformat(),)
        ).fetchall()
    for row in [dict(r) for r in expired]:
        log.warning("ESCALATE %s [%s] %s expired unACKed", row["signal_id"], row["priority"], row["subject"])
        with _db_lock, db() as conn:
            conn.execute("UPDATE signals SET ack_status='expired',escalated_at=? WHERE signal_id=?",
                         (now.isoformat(), row["signal_id"]))
        if row["escalate_to"]:
            bump = {"P3": "P2", "P2": "P1", "P1": "P0", "P0": "P0"}
            new_pri = bump.get(row["priority"], row["priority"])
            signal_create("signal-bus-escalation", row["escalate_to"], new_pri,
                          f"ESCALATED:{row['subject']}",
                          f"TTL expired unACKed after {row['ttl_minutes']}min. Original: {row['message']}",
                          {"original_signal_id": row["signal_id"]}, True,
                          "ahmed" if row["escalate_to"] != "ahmed" else None,
                          TTL_DEFAULTS.get(new_pri, 10))


def _escalation_daemon():
    while True:
        time.sleep(30)
        try:
            _run_escalation_once()
        except Exception as e:
            log.error("Escalation daemon: %s", e)


def _p0_alert_daemon():
    while True:
        time.sleep(15)
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE priority='P0' AND ack_status='pending' "
                    "AND ack_required=1 AND queued_at<? AND p0_alerted=0", (cutoff,)
                ).fetchall()
            for row in [dict(r) for r in rows]:
                log.warning("P0 UNACKED >60s: %s — alerting broker", row["signal_id"])
                _broker_alert("CRITICAL", row["subject"],
                              f"P0 UNACKED >60s | {row['from_agent']}→{row['to_agent']} | {row['message']}")
                with _db_lock, db() as conn:
                    conn.execute("UPDATE signals SET p0_alerted=1 WHERE signal_id=?", (row["signal_id"],))
        except Exception as e:
            log.error("P0 daemon: %s", e)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a): pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return None

    def do_POST(self):
        b = self._body()
        if b is None:
            return self._json(400, {"ok": False, "error": "invalid JSON"})

        if self.path == "/signal":
            priority = b.get("priority", "P2")
            if priority not in VALID_PRIORITIES:
                return self._json(400, {"ok": False, "error": f"priority must be P0/P1/P2/P3"})
            to, subject, message = b.get("to","").strip(), b.get("subject","").strip(), b.get("message","").strip()
            if not to or not subject or not message:
                return self._json(400, {"ok": False, "error": "to, subject, message required"})
            ttl = b.get("ttl_minutes", TTL_DEFAULTS[priority])
            ack_required = b.get("ack_required", priority in ("P0","P1"))
            sig_id, queued_at = signal_create(
                b.get("from","unknown").strip(), to, priority, subject, message[:500],
                b.get("context"), ack_required, b.get("escalate_to"), ttl
            )
            return self._json(200, {"ok": True, "signal_id": sig_id, "queued_at": queued_at})

        elif self.path == "/ack":
            sig_id = b.get("signal_id","").strip()
            agent  = b.get("agent","").strip()
            status = b.get("status","").strip()
            if not sig_id or not agent or status not in VALID_ACK_STATUS:
                return self._json(400, {"ok": False, "error": f"signal_id, agent, status({VALID_ACK_STATUS}) required"})
            result, err = signal_ack(sig_id, agent, status, b.get("note",""))
            if err == "not_found":
                return self._json(404, {"ok": False, "error": "signal not found"})
            return self._json(200, {"ok": True, **result})

        elif self.path == "/send":
            to, msg = b.get("to","").strip(), b.get("message","").strip()
            if not to or not msg:
                return self._json(400, {"ok": False, "error": "to and message required"})
            mid = msg_send(b.get("from","unknown"), to, msg)
            log.info("%s → %s: %s", b.get("from","unknown"), to, msg[:80])
            return self._json(200, {"ok": True, "message_id": mid})

        self._json(404, {"error": "not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        qs, path = parse_qs(parsed.query), parsed.path

        if path == "/recv":
            agent = qs.get("agent", [""])[0].strip()
            if not agent: return self._json(400, {"error": "agent param required"})
            return self._json(200, {"agent": agent, "signals": signal_recv(agent)})

        elif path == "/pending":
            sigs = signal_pending()
            return self._json(200, {"total": len(sigs), "signals": sigs})

        elif path == "/history":
            agent = qs.get("agent", [""])[0].strip()
            limit = int(qs.get("limit", ["50"])[0])
            if not agent: return self._json(400, {"error": "agent param required"})
            return self._json(200, {"agent": agent, "signals": signal_history(agent, limit)})

        elif path.startswith("/inbox/"):
            agent = path.split("/inbox/")[1].strip("/")
            return self._json(200, {"agent": agent, "messages": msg_inbox(agent)})

        elif path == "/status":
            return self._json(200, {"ok": True, "pending": msg_depths()})

        elif path == "/health":
            with db() as conn:
                sig_pending = conn.execute("SELECT COUNT(*) FROM signals WHERE ack_status='pending'").fetchone()[0]
                sig_total   = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
                msg_total   = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                msg_pending = conn.execute("SELECT COUNT(*) FROM messages WHERE delivered=0").fetchone()[0]
            return self._json(200, {
                "ok": True, "service": "nexus-signal-bus", "version": VERSION,
                "uptime_since": BOOT_TIME,
                "db": {"ok": True, "total_messages": msg_total, "pending": msg_pending},
                "signals": {"total": sig_total, "pending": sig_pending}
            })

        else:
            self._json(404, {"error": "not found"})


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    init_db()
    for name, target in [("escalation", _escalation_daemon), ("p0-alert", _p0_alert_daemon)]:
        threading.Thread(target=target, daemon=True, name=name).start()
        log.info("Thread started: %s", name)
    log.info("Nexus SIGNAL Bus v%s on port %d", VERSION, PORT)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()


# ── Legacy test compatibility aliases ────────────────────────────────────────
send_message      = msg_send
get_queue_depths  = msg_depths
consume_inbox     = msg_inbox
ThreadedHTTPServer = Server
BusHandler        = Handler
NEVER_READ_TTL_HOURS = 72
DELIVERED_TTL_HOURS  = 24
CLEANUP_INTERVAL_S   = 3600

def get_db_stats():
    try:
        with db() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM messages WHERE delivered=0").fetchone()[0]
            today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            delivered_today = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE delivered=1 AND delivered_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]
        return {"ok": True, "total_messages": total, "pending": pending, "delivered_today": delivered_today}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _run_cleanup():
    now = datetime.now(timezone.utc)
    cutoff_unread    = (now - timedelta(hours=NEVER_READ_TTL_HOURS)).isoformat()
    cutoff_delivered = (now - timedelta(hours=DELIVERED_TTL_HOURS)).isoformat()
    with _db_lock, db() as conn:
        conn.execute("UPDATE messages SET delivered=1,delivered_at=? WHERE delivered=0 AND created_at<?",
                     (now.isoformat(), cutoff_unread))
        conn.execute("DELETE FROM messages WHERE delivered=1 AND delivered_at<?", (cutoff_delivered,))
