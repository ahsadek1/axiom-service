"""
nexus_health_monitor.py — Nexus Health Monitor v2 (Mac Mini)
=============================================================
Full instantaneous detection + diagnostic + self-healing.

Detection flow:
  1. Each agent pushes heartbeat every 30s → _receive_heartbeat()
     (Mac mini version: heartbeats POSTed to local Flask receiver on port 9191)
  2. Missed heartbeat within 75s → IMMEDIATE outage response
  3. Diagnostic classifier runs → identifies failure type + error site
  4. Tiered heal action: Railway API redeploy → or alert-only escalation
  5. Recovery confirmed to Health Group when service restores

Backup: poll-based check every 5 min (catches services with no heartbeat emitter)

Run:
    python3 nexus_health_monitor.py

Author: OMNI 🌐  |  Date: 2026-04-08 v2
"""

import os
import sys
import time
import threading
import datetime
import requests
import pytz
import sys as _sys

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv("/Users/ahmedsadek/nexus/.env")
except ImportError:
    pass

_sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
from alert_client import send_alert as _send_alert

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN",   "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc")
HEALTH_GROUP   = os.getenv("HEALTH_GROUP_ID", "-5184172590")
AHMED_DM       = os.getenv("AHMED_CHAT_ID",   "8573754783")
NEXUS_SECRET   = os.getenv("NEXUS_SECRET",    "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
RAILWAY_TOKEN  = os.getenv("RAILWAY_TOKEN",   "")

HEARTBEAT_PORT         = 9191
HEARTBEAT_TIMEOUT_SEC  = 75
POLL_INTERVAL_SEC      = 300
FAILURE_THRESHOLD      = 2
ET_TZ                  = pytz.timezone("America/New_York")
MARKET_OPEN            = datetime.time(9, 25)
MARKET_CLOSE           = datetime.time(16, 5)

# ── Services ──────────────────────────────────────────────────────────────────

# G3: all Railway URLs from env vars — no hardcoded fallbacks
_AXIOM_BASE = os.getenv("RAILWAY_AXIOM_URL", "")  # e.g. https://axiom-production-334c.up.railway.app
_ALPHA_BASE = os.getenv("RAILWAY_ALPHA_URL", "")  # e.g. https://worker-production-2060.up.railway.app
_PRIME_BASE = os.getenv("RAILWAY_PRIME_URL", "")  # e.g. https://nexus-prime-bot-production.up.railway.app

SERVICES = {
    "Axiom": {
        "display":    "Axiom 🔷",
        "url":        f"{_AXIOM_BASE}/health" if _AXIOM_BASE else "",
        "railway_id": os.getenv("AXIOM_RAILWAY_SERVICE_ID", ""),
    },
    "Alpha": {
        "display":    "Alpha 🔵",
        "url":        f"{_ALPHA_BASE}/health" if _ALPHA_BASE else "",
        "railway_id": os.getenv("ALPHA_RAILWAY_SERVICE_ID", ""),
    },
    "Prime": {
        "display":    "Prime 🟢",
        "url":        f"{_PRIME_BASE}/health" if _PRIME_BASE else "",
        "railway_id": os.getenv("PRIME_RAILWAY_SERVICE_ID", ""),
    },
}

# ── State ─────────────────────────────────────────────────────────────────────

_state = {
    name: {
        "status":           "UNKNOWN",
        "latency_ms":       0,
        "failure_count":    0,
        "alert_sent":       False,
        "last_heartbeat":   None,
        "heartbeat_missed": False,
        "last_up":          None,
        "last_check":       None,
        "failure_type":     None,
        "heal_attempts":    0,
    }
    for name in SERVICES
}
_state_lock      = threading.Lock()
_monitor_start   = datetime.datetime.utcnow()
_last_pulse_hour = -1

# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg(chat_id, text):
    """Route through Alert Broker (chat_id kept for signature compat, ignored)."""
    _send_alert(
        source="nexus-health-monitor",
        level="WARNING",
        title=text[:200],
        body=text[200:] if len(text) > 200 else "",
        targets=["nexus_health_group"],
    )
    return True

def _alert(text, tier=2):
    level = "CRITICAL" if tier >= 3 else "WARNING"
    targets = ["ahmed", "nexus_health_group"] if tier >= 3 else ["nexus_health_group"]
    _send_alert(
        source="nexus-health-monitor",
        level=level,
        title=text[:200],
        body=text[200:] if len(text) > 200 else "",
        targets=targets,
    )
    if tier >= 3:
        _tg(AHMED_DM, text)

# ── Diagnostic Classifier ─────────────────────────────────────────────────────

FAILURE_TYPES = {
    "CONNECTION_REFUSED": {
        "label":  "Connection Refused",
        "emoji":  "🔌",
        "cause":  "Service process crashed or port not open",
        "action": "REDEPLOY",
        "detail": "The service is not accepting connections. Process likely crashed.",
    },
    "TIMEOUT": {
        "label":  "Request Timeout",
        "emoji":  "⏱️",
        "cause":  "Service overloaded, frozen, or out of memory",
        "action": "REDEPLOY",
        "detail": "Service not responding within 8s. Possible OOM or deadlock.",
    },
    "HTTP_500": {
        "label":  "Internal Server Error",
        "emoji":  "💥",
        "cause":  "Application-level crash in health endpoint",
        "action": "REDEPLOY",
        "detail": "Service is up but returning 500 — application error.",
    },
    "HTTP_503": {
        "label":  "Service Unavailable",
        "emoji":  "🚫",
        "cause":  "Service starting up or overloaded",
        "action": "WAIT_THEN_CHECK",
        "detail": "Returning 503 — may be restarting. Waiting 60s before redeploying.",
    },
    "HTTP_OTHER": {
        "label":  "Unexpected HTTP Error",
        "emoji":  "⚠️",
        "cause":  "Unexpected HTTP status",
        "action": "REDEPLOY",
        "detail": "Unexpected HTTP response — abnormal service behavior.",
    },
    "DNS_FAILURE": {
        "label":  "DNS Resolution Failure",
        "emoji":  "🌐",
        "cause":  "Railway domain unreachable — possible infrastructure issue",
        "action": "ALERT_ONLY",
        "detail": "Cannot resolve hostname. May be Railway-wide — not isolated to this service.",
    },
    "HEARTBEAT_MISSED": {
        "label":  "Heartbeat Missed",
        "emoji":  "💓",
        "cause":  "Agent stopped emitting — process may be frozen or crashed",
        "action": "DIAGNOSE_THEN_HEAL",
        "detail": "No heartbeat in 75s. Running full diagnostic before healing.",
    },
    "UNKNOWN": {
        "label":  "Unknown Failure",
        "emoji":  "❓",
        "cause":  "Unclassified error",
        "action": "REDEPLOY",
        "detail": "Cannot classify failure. Attempting redeploy.",
    },
}

def _classify(error_str, status_code=0):
    err = (error_str or "").lower()
    if "connection refused" in err: return "CONNECTION_REFUSED"
    if "timeout" in err or "timed out" in err: return "TIMEOUT"
    if "name or service not known" in err or "dns" in err: return "DNS_FAILURE"
    if status_code == 500: return "HTTP_500"
    if status_code == 503: return "HTTP_503"
    if status_code and status_code != 200: return "HTTP_OTHER"
    return "UNKNOWN"

def _run_diagnostic(name, url):
    # G3: skip Railway checks when URL env var is not set
    if not url:
        return {"failure_type": None, "status_code": 0, "latency_ms": 0,
                "heal_action": "NONE", "detail": "URL not configured (RAILWAY_*_URL not set)", "error": None}
    t0 = time.time()
    try:
        r = requests.get(url, timeout=8)
        lat = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            return {"failure_type": None, "status_code": 200, "latency_ms": lat,
                    "heal_action": "NONE", "detail": "Service UP — already recovered", "error": None}
        ft   = _classify("", r.status_code)
        info = FAILURE_TYPES[ft]
        return {"failure_type": ft, "status_code": r.status_code, "latency_ms": lat,
                "heal_action": info["action"], "detail": info["detail"], "error": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError as e:
        ft = _classify(str(e))
        return {"failure_type": ft, "status_code": 0, "latency_ms": int((time.time()-t0)*1000),
                "heal_action": FAILURE_TYPES[ft]["action"], "detail": FAILURE_TYPES[ft]["detail"],
                "error": "Connection refused"}
    except requests.exceptions.Timeout:
        return {"failure_type": "TIMEOUT", "status_code": 0, "latency_ms": 8000,
                "heal_action": "REDEPLOY", "detail": FAILURE_TYPES["TIMEOUT"]["detail"],
                "error": "Timeout after 8s"}
    except Exception as e:
        return {"failure_type": "UNKNOWN", "status_code": 0, "latency_ms": 0,
                "heal_action": "REDEPLOY", "detail": FAILURE_TYPES["UNKNOWN"]["detail"],
                "error": str(e)[:100]}

# ── Railway redeploy ──────────────────────────────────────────────────────────

def _redeploy(name, service_id):
    if not RAILWAY_TOKEN or not service_id:
        return False
    try:
        r = requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={
                "query": "mutation serviceInstanceRedeploy($serviceId: String!) { serviceInstanceRedeploy(serviceId: $serviceId) }",
                "variables": {"serviceId": service_id},
            },
            timeout=15,
        )
        return r.status_code == 200 and "errors" not in r.json()
    except Exception:
        return False

# ── Heal dispatcher ───────────────────────────────────────────────────────────

def _execute_heal(name, config, diag):
    action     = diag.get("heal_action", "REDEPLOY")
    service_id = config.get("railway_id", "")
    display    = config["display"]

    with _state_lock:
        _state[name]["heal_attempts"] += 1
        attempts = _state[name]["heal_attempts"]

    if action == "ALERT_ONLY":
        return "⚠️ Alert-only (DNS/infra issue) — manual check required"

    if action == "WAIT_THEN_CHECK":
        threading.Timer(60, lambda: _deferred_check(name, config)).start()
        return "⏳ HTTP 503 — waiting 60s then re-diagnosing"

    if action in ("REDEPLOY", "DIAGNOSE_THEN_HEAL", "UNKNOWN"):
        if service_id and RAILWAY_TOKEN:
            ok = _redeploy(name, service_id)
            if ok:
                return f"🔄 Railway redeploy triggered (attempt #{attempts})"
            return f"⚠️ Railway redeploy failed (attempt #{attempts}) — manual restart required"
        return "⚠️ No RAILWAY_TOKEN/service ID — manual Railway restart required"

    return f"⚠️ Unknown action: {action}"

def _deferred_check(name, config):
    diag = _run_diagnostic(name, config["url"])
    if diag["failure_type"] is not None:
        result = _execute_heal(name, config, {"heal_action": "REDEPLOY"})
        _tg(HEALTH_GROUP, f"⚠️ {config['display']} still DOWN after 60s wait. {result}")

# ── Outage handler ────────────────────────────────────────────────────────────

def _handle_outage(name, config, trigger="POLL"):
    display = config["display"]
    url     = config["url"]
    print(f"[MONITOR] 🔴 OUTAGE: {display} | trigger={trigger} | diagnosing...")

    diag = _run_diagnostic(name, url)
    ft   = diag.get("failure_type", "UNKNOWN")
    info = FAILURE_TYPES.get(ft, FAILURE_TYPES["UNKNOWN"])

    with _state_lock:
        _state[name]["failure_type"] = ft

    now = _et_full()
    alert_msg = (
        f"🔴 <b>NEXUS CRITICAL — {display} OFFLINE</b>\n\n"
        f"{info['emoji']} <b>Failure Type:</b> {info['label']}\n"
        f"📍 <b>Error Site:</b> {info['cause']}\n"
        f"📋 <b>Diagnostic:</b> {info['detail']}\n"
        f"🔧 <b>Heal Action:</b> {info['action']}\n"
        f"⚡ <b>Triggered by:</b> {trigger}\n"
        f"🕐 <b>Time:</b> {now}\n\n"
        f"<i>Raw: {diag.get('error', 'N/A')}</i>"
    )
    _alert(alert_msg, tier=3)

    heal_result = _execute_heal(name, config, diag)
    _tg(HEALTH_GROUP, f"🔧 <b>Healing {display}:</b> {heal_result}")
    print(f"[MONITOR] Heal dispatched: {heal_result}")

# ── Heartbeat receiver (lightweight HTTP) ─────────────────────────────────────

def _start_heartbeat_receiver():
    """
    Lightweight HTTP server on port 9191.
    Agents POST to http://<mac-mini-ip>:9191/heartbeat
    (Mac mini is primary; Railway cloud monitor has its own receiver built-in)
    """
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import json as _json

        class HBHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/heartbeat":
                    self.send_response(404)
                    self.end_headers()
                    return
                length  = int(self.headers.get("Content-Length", 0))
                body    = self.rfile.read(length)
                try:
                    data    = _json.loads(body)
                    service = data.get("service", "")
                    if service in _state:
                        now_iso = datetime.datetime.utcnow().isoformat()
                        with _state_lock:
                            s             = _state[service]
                            prev_missed   = s["heartbeat_missed"]
                            prev_alerted  = s["alert_sent"]
                            s["last_heartbeat"]   = now_iso
                            s["heartbeat_missed"] = False
                            if prev_missed:
                                s["status"]        = "UP"
                                s["failure_count"] = 0
                                s["alert_sent"]    = False
                                s["failure_type"]  = None
                                s["heal_attempts"] = 0

                        if prev_missed and prev_alerted:
                            display = SERVICES[service]["display"]
                            _tg(HEALTH_GROUP, f"🟢 <b>{display} RESTORED</b>\nHeartbeat resumed | {_et_now()}")
                            _tg(AHMED_DM,     f"✅ {display} back online — heartbeat at {_et_now()}")

                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"status":"ok"}')
                    else:
                        self.send_response(400)
                        self.end_headers()
                except Exception:
                    self.send_response(500)
                    self.end_headers()

            def log_message(self, format, *args):
                pass  # suppress access logs

        server = HTTPServer(("0.0.0.0", HEARTBEAT_PORT), HBHandler)
        print(f"[MONITOR] 💓 Heartbeat receiver listening on :{HEARTBEAT_PORT}")
        server.serve_forever()
    except Exception as e:
        print(f"[MONITOR] Heartbeat receiver failed to start: {e}")

# ── Heartbeat watchdog ────────────────────────────────────────────────────────

def _heartbeat_watchdog():
    print("[MONITOR] 💓 Heartbeat watchdog started")
    while True:
        time.sleep(30)
        now = datetime.datetime.utcnow()
        for name, config in SERVICES.items():
            with _state_lock:
                s       = _state[name]
                last_hb = s["last_heartbeat"]
                already = s["heartbeat_missed"]
                alerted = s["alert_sent"]

            if last_hb is None:
                continue  # never received heartbeat — rely on poll

            age = (now - datetime.datetime.fromisoformat(last_hb)).total_seconds()
            if age > HEARTBEAT_TIMEOUT_SEC and not already:
                print(f"[MONITOR] 💔 Heartbeat missed: {config['display']} ({age:.0f}s ago)")
                with _state_lock:
                    _state[name]["heartbeat_missed"] = True
                    _state[name]["status"]           = "DOWN"
                    if not alerted:
                        _state[name]["alert_sent"] = True
                if not alerted:
                    _handle_outage(name, config, trigger=f"HEARTBEAT_MISSED ({age:.0f}s)")

# ── Poll-based backup ─────────────────────────────────────────────────────────

def _poll_cycle():
    for name, config in SERVICES.items():
        url = config["url"]
        t0  = time.time()
        try:
            r   = requests.get(url, timeout=8)
            lat = int((time.time() - t0) * 1000)
            up  = r.status_code == 200
            err = None if up else f"HTTP {r.status_code}"
        except requests.exceptions.ConnectionError:
            up, lat, err = False, 0, "Connection refused"
        except requests.exceptions.Timeout:
            up, lat, err = False, 8000, "Timeout"
        except Exception as e:
            up, lat, err = False, 0, str(e)[:80]

        with _state_lock:
            s    = _state[name]
            prev = s["status"]
            s["last_check"] = _et_full()
            s["latency_ms"] = lat

        if not up:
            with _state_lock:
                _state[name]["status"]        = "DOWN"
                _state[name]["failure_count"] += 1
                count   = _state[name]["failure_count"]
                alerted = _state[name]["alert_sent"]

            print(f"[MONITOR] [POLL] ⚠️ {config['display']} DOWN #{count} — {err}")
            if count >= FAILURE_THRESHOLD and not alerted:
                with _state_lock:
                    _state[name]["alert_sent"] = True
                _handle_outage(name, config, trigger=f"POLL (#{count})")
        else:
            with _state_lock:
                s           = _state[name]
                prev        = s["status"]
                was_alerted = s["alert_sent"]
                if prev == "DOWN":
                    s["status"]        = "UP"
                    s["failure_count"] = 0
                    s["alert_sent"]    = False
                    s["failure_type"]  = None
                    s["heal_attempts"] = 0
                elif s["status"] == "UNKNOWN":
                    s["status"]        = "UP"

            if prev == "DOWN" and was_alerted:
                display = config["display"]
                _tg(HEALTH_GROUP, f"🟢 <b>{display} RESTORED</b>\nDetected via poll | {lat}ms | {_et_full()}")
                _tg(AHMED_DM,     f"✅ {display} back online at {_et_now()}")

            print(f"[MONITOR] [POLL] ✅ {config['display']} UP | {lat}ms")

# ── Hourly pulse ──────────────────────────────────────────────────────────────

def _et_now():
    return datetime.datetime.now(ET_TZ).strftime("%H:%M ET")

def _et_full():
    return datetime.datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M ET")

def _is_market_hours():
    t = datetime.datetime.now(ET_TZ).time()
    return MARKET_OPEN <= t <= MARKET_CLOSE

def _uptime():
    d = datetime.datetime.utcnow() - _monitor_start
    h, r = divmod(int(d.total_seconds()), 3600)
    return f"{h}h {r//60}m"

def _send_pulse():
    lines = []
    for name, config in SERVICES.items():
        s    = _state[name]
        icon = "✅" if s["status"] == "UP" else ("❌" if s["status"] == "DOWN" else "❓")
        lat  = f"{s['latency_ms']}ms"
        hb   = "💓" if (s["last_heartbeat"] and not s["heartbeat_missed"]) else ("💔" if s["heartbeat_missed"] else "⚪")
        lines.append(f"  {icon} {config['display']}: {s['status']} | {lat} | {hb}")

    all_up = all(s["status"] == "UP" for s in _state.values())
    msg = (
        f"{'🟢' if all_up else '🔴'} <b>NEXUS HEALTH PULSE — {_et_full()}</b>\n\n"
        + "\n".join(lines) +
        f"\n\n💓 = heartbeat live | 💔 = heartbeat missed | ⚪ = no heartbeat\n"
        f"<b>Uptime:</b> {_uptime()} | <b>Source:</b> Mac mini watchdog\n\n"
        f"{'✅ All systems nominal' if all_up else '⚠️ DEGRADED — see alerts above'}"
    )
    _tg(HEALTH_GROUP, msg)
    print("[MONITOR] Pulse sent")

# ── Main loop ─────────────────────────────────────────────────────────────────

def _monitor_loop():
    global _last_pulse_hour
    print(f"[MONITOR] 🟢 Nexus Health Monitor v2 (Mac mini) started — {_et_full()}")

    _tg(HEALTH_GROUP, (
        f"🟢 <b>NEXUS HEALTH MONITOR v2 ONLINE (Mac mini)</b>\n\n"
        f"Started: {_et_full()}\n"
        f"Watching: {' | '.join(c['display'] for c in SERVICES.values())}\n\n"
        f"<b>Detection:</b>\n"
        f"  💓 Heartbeat — alert in &lt;75s of missed ping\n"
        f"  🔍 Poll — backup every 5 min\n\n"
        f"<b>On failure:</b>\n"
        f"  → Diagnostic identifies failure type + error site\n"
        f"  → Tiered heal: redeploy / wait / alert-only\n"
        f"  → Recovery confirmed when restored\n\n"
        f"Auto-redeploy: {'✅ enabled' if RAILWAY_TOKEN else '⚠️ needs RAILWAY_TOKEN'}"
    ))

    while True:
        try:
            _poll_cycle()
            if _is_market_hours():
                h = datetime.datetime.now(ET_TZ).hour
                if h != _last_pulse_hour:
                    _last_pulse_hour = h
                    _send_pulse()
        except Exception as e:
            print(f"[MONITOR] Loop error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


def run_single_check():
    """Run a single health check poll cycle. Used by cron/wrapper scripts.
    Returns: 0 if all services UP, 1 if any service DOWN.
    """
    _poll_cycle()
    
    # Check if any service is DOWN
    with _state_lock:
        any_down = any(s["status"] == "DOWN" for s in _state.values())
    
    return 1 if any_down else 0


def start_health_monitor():
    """Start all monitor components as background daemon threads."""
    threading.Thread(target=_start_heartbeat_receiver, daemon=True, name="hb-receiver").start()
    threading.Thread(target=_heartbeat_watchdog,       daemon=True, name="hb-watchdog").start()
    t = threading.Thread(target=_monitor_loop,         daemon=True, name="poll-loop")
    t.start()
    return t


if __name__ == "__main__":
    # Start heartbeat receiver + watchdog as background threads
    threading.Thread(target=_start_heartbeat_receiver, daemon=True).start()
    threading.Thread(target=_heartbeat_watchdog,       daemon=True).start()
    # Run poll loop in main thread
    _monitor_loop()
