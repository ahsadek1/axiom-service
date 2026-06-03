"""
autonomous_action_engine.py — NEXUS V2 Autonomous Action Engine
================================================================
Runs every 60 seconds. Detects problems. Fixes them immediately.
Reports what it did. Never asks for permission.

Philosophy:
  DETECT → FIX → LOG → NOTIFY (FYI only)
  Never: DETECT → NOTIFY → WAIT → ASK

Ahmed's directive May 2026:
  "The system is very good at detecting errors but total blindness
   to alerts is horrible. Reluctance to immediately act is one of
   the major issues. I want a fully automated system that detects
   and deploys fix immediately. Reporting is FYI, not asking for help."

Known failure modes and their deterministic fixes:
  SERVICE DOWN          → launchctl restart (respects startup order)
  VIX BRAKE 999         → restart alpha-execution after Axiom confirmed
  CB STOP (paper)       → auto-reset overnight, no permission needed
  OMNI SILENCE >20min   → restart OMNI
  AXIOM POOL EMPTY      → trigger pool refresh during market hours
  DLQ BACKLOG >5        → drain and retry immediately
  ALPHA-BUFFER UNHEALTHY→ restart alpha-buffer
  BACKTEST DB LOCKED    → reconnect
  PHANTOM POSITIONS     → reconcile with Alpaca

Things that are NOT auto-fixed (require Ahmed):
  - CB STOP in LIVE trading mode (real money)
  - Service fails restart 3+ times in 30 minutes
  - Alpaca account blocked

Usage:
  python3 autonomous_action_engine.py          # runs forever
  python3 autonomous_action_engine.py --once   # single scan
  launchctl load ai.nexus.autonomous.plist     # scheduled every 60s
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("nexus.autonomous")

_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEXUS_SECRET   = os.getenv("NEXUS_WEBHOOK_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
HEALTH_GROUP   = os.getenv("NEXUS_HEALTH_GROUP_ID", "-5241272802")
IS_PAPER       = os.getenv("ALPACA_PAPER", "true").lower() == "true"
BACKTEST_DB    = "/Users/ahmedsadek/nexus/data/backtest.db"
ALPHA_BUFFER_DB = "/Users/ahmedsadek/nexus/data/alpha_buffer.db"
ALPACA_KEY     = os.getenv("ALPACA_API_KEY", "PKGGXWNZTITUTZUNVK2QBLZJQL")
ALPACA_SECRET  = os.getenv("ALPACA_SECRET_KEY", "DWwCxfRpv92ZLkGX8Ao4mDsAATafLwcuw8EeFAM7s89i")
ALPACA_URL     = "https://paper-api.alpaca.markets"

# Restart cooldown — don't restart same service more than 3x in 30 min
_restart_log: dict[str, list[float]] = {}
MAX_RESTARTS_PER_WINDOW = 3
RESTART_WINDOW_SECONDS  = 1800  # 30 minutes

# Service startup order matters — axiom must be up before buffers, etc.
SERVICES = [
    ("axiom",           8001, "ai.nexus.axiom"),
    ("oracle",          8007, "ai.nexus.oracle"),
    ("ails",            8008, "ai.nexus.ails"),
    ("alpha-buffer",    8002, "ai.nexus.alpha-buffer"),
    ("prime-buffer",    8003, "ai.nexus.prime-buffer"),
    ("omni",            8004, "ai.nexus.omni"),
    ("alpha-execution", 8005, "ai.nexus.alpha-execution"),
    ("prime-execution", 8006, "ai.nexus.prime-execution"),
    ("guardian-angel",  8009, "ai.nexus.guardian-angel"),
    ("thesis-trader",   8070, "ai.nexus.thesis-trader"),
]


# ---------------------------------------------------------------------------
# Telegram FYI (not a request — a report)
# ---------------------------------------------------------------------------

def _fyi(msg: str) -> None:
    """Send FYI notification. Fire and forget — never blocks action."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": HEALTH_GROUP, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass  # notification failure never blocks action


# ---------------------------------------------------------------------------
# Restart throttle
# ---------------------------------------------------------------------------

def _can_restart(service: str) -> bool:
    """Return True if service hasn't been restarted too many times recently."""
    now = time.time()
    times = _restart_log.get(service, [])
    # Remove entries outside the window
    times = [t for t in times if now - t < RESTART_WINDOW_SECONDS]
    _restart_log[service] = times
    return len(times) < MAX_RESTARTS_PER_WINDOW


def _record_restart(service: str) -> None:
    if service not in _restart_log:
        _restart_log[service] = []
    _restart_log[service].append(time.time())


def _restart_count(service: str) -> int:
    now = time.time()
    times = _restart_log.get(service, [])
    return len([t for t in times if now - t < RESTART_WINDOW_SECONDS])


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

def _service_healthy(port: int) -> bool:
    try:
        resp = requests.get(f"http://localhost:{port}/health", timeout=4)
        if resp.status_code == 200:
            return resp.json().get("status") in ("healthy", "ok")
    except Exception:
        pass
    return False


def _restart_service(name: str, plist: str, port: int) -> bool:
    """Restart a service via launchctl. Returns True if healthy after restart."""
    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{plist}.plist")
    try:
        subprocess.run(["launchctl", "unload", plist_path],
                      capture_output=True, timeout=10)
        time.sleep(2)
        subprocess.run(["launchctl", "load", plist_path],
                      capture_output=True, timeout=10)
        time.sleep(6)  # wait for startup
        return _service_healthy(port)
    except Exception as exc:
        log.error("Restart %s failed: %s", name, exc)
        return False


# ---------------------------------------------------------------------------
# Market hours helper
# ---------------------------------------------------------------------------

def _market_hours() -> bool:
    now = datetime.now(_ET)
    return (
        now.weekday() < 5
        and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
        and now.hour < 16
    )


def _off_hours() -> bool:
    return not _market_hours()


# ---------------------------------------------------------------------------
# Individual fix actions
# ---------------------------------------------------------------------------

def fix_service_down(name: str, port: int, plist: str) -> None:
    """Service is down — restart immediately."""
    if not _can_restart(name):
        count = _restart_count(name)
        log.warning(
            "THROTTLE: %s has been restarted %d times in 30min — not retrying",
            name, count,
        )
        _fyi(
            f"🚨 <b>{name.upper()} REPEATED FAILURE</b>\n"
            f"Restarted {count}x in 30min — giving up auto-heal.\n"
            f"<b>Manual intervention may be needed.</b>"
        )
        return

    log.info("ACTION: Restarting %s (was down)", name)
    _record_restart(name)
    success = _restart_service(name, plist, port)

    if success:
        log.info("FIXED: %s restarted successfully", name)
        _fyi(f"✅ <b>AUTO-FIXED: {name.upper()}</b>\nService was down — restarted successfully.")
    else:
        log.error("FAILED: Could not restart %s", name)
        _fyi(
            f"❌ <b>RESTART FAILED: {name.upper()}</b>\n"
            f"Service still down after restart attempt.\n"
            f"Retrying next cycle."
        )


def fix_vix_brake_999(port: int = 8005) -> None:
    """
    VIX=999 means alpha-execution couldn't reach Axiom at startup.
    Fix: confirm Axiom is healthy, then restart alpha-execution.
    """
    if not _service_healthy(8001):
        log.warning("VIX brake 999 but Axiom is also down — fixing Axiom first")
        return  # service loop will handle Axiom

    if not _can_restart("alpha-execution"):
        return

    log.info("ACTION: Clearing VIX brake 999 — restarting alpha-execution")
    _record_restart("alpha-execution")
    success = _restart_service("alpha-execution", "ai.nexus.alpha-execution", port)

    if success:
        _fyi("✅ <b>AUTO-FIXED: VIX BRAKE CLEARED</b>\nAlpha-execution restarted — VIX brake race condition resolved.")
    else:
        _fyi("❌ <b>VIX BRAKE FIX FAILED</b>\nalpha-execution still degraded after restart.")


def fix_circuit_breaker_stop() -> None:
    """
    CB in STOP state.
    Paper mode + off hours → auto-reset.
    Paper mode + market hours → alert only (something went wrong today).
    Live mode → alert only (real money, needs Ahmed).
    """
    if not IS_PAPER:
        _fyi(
            "🚨 <b>CIRCUIT BREAKER STOP — LIVE MODE</b>\n"
            "Real money account — NOT auto-resetting.\n"
            "<b>Ahmed manual review required.</b>"
        )
        return

    if _market_hours():
        _fyi(
            "⚠️ <b>CIRCUIT BREAKER STOP — MARKET HOURS</b>\n"
            "Paper mode but market is open.\n"
            "CB tripped during trading — reviewing automatically next cycle.\n"
            "Will auto-reset after market close."
        )
        return

    # Paper mode, off hours — reset immediately
    log.info("ACTION: Auto-resetting circuit breaker (paper mode, off hours)")
    try:
        resp = requests.post(
            "http://localhost:8002/circuit-breaker/reset",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("FIXED: Circuit breaker reset to NORMAL")
            _fyi("✅ <b>AUTO-FIXED: CIRCUIT BREAKER RESET</b>\nPaper mode overnight reset — NORMAL. V2 ready for tomorrow.")
        else:
            _fyi(f"❌ <b>CB RESET FAILED</b>\nHTTP {resp.status_code}")
    except Exception as exc:
        _fyi(f"❌ <b>CB RESET ERROR</b>\n{exc}")


def fix_omni_silence() -> None:
    """OMNI hasn't synthesized in 20+ minutes during market hours — restart it."""
    if not _can_restart("omni"):
        return

    log.info("ACTION: Restarting OMNI — synthesis silence detected")
    _record_restart("omni")
    success = _restart_service("omni", "ai.nexus.omni", 8004)

    if success:
        _fyi("✅ <b>AUTO-FIXED: OMNI RESTARTED</b>\nSynthesis was silent 20+ min — OMNI restarted and healthy.")
    else:
        _fyi("❌ <b>OMNI RESTART FAILED</b>\nOMNI still unhealthy after restart attempt.")


def fix_alpha_buffer_unhealthy() -> None:
    """Alpha-buffer resilience status is UNHEALTHY (not CB related)."""
    if not _can_restart("alpha-buffer"):
        return

    log.info("ACTION: Restarting alpha-buffer — UNHEALTHY resilience status")
    _record_restart("alpha-buffer")
    success = _restart_service("alpha-buffer", "ai.nexus.alpha-buffer", 8002)

    if success:
        _fyi("✅ <b>AUTO-FIXED: ALPHA-BUFFER RESTARTED</b>\nResilience status was UNHEALTHY — resolved.")
    else:
        _fyi("❌ <b>ALPHA-BUFFER RESTART FAILED</b>\nStill unhealthy after restart.")


def fix_drain_dlq() -> None:
    """DLQ has backlogged failed executions — drain them."""
    try:
        resp = requests.post(
            "http://localhost:8005/dlq/drain",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            drained = data.get("drained", 0)
            log.info("FIXED: DLQ drained %d items", drained)
            if drained > 0:
                _fyi(f"✅ <b>AUTO-FIXED: DLQ DRAINED</b>\n{drained} failed executions retried automatically.")
        else:
            log.warning("DLQ drain returned HTTP %d", resp.status_code)
    except Exception as exc:
        log.warning("DLQ drain failed: %s", exc)


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------

def detect_vix_brake_999() -> bool:
    try:
        resp = requests.get("http://localhost:8005/health", timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            vix = data.get("vix", 0)
            full = data.get("vix_brake_full", False)
            suspended = (data.get("resilience_status") or {}).get("suspended_reason", "")
            return full and (vix >= 500 or "999" in str(suspended))
    except Exception:
        pass
    return False


def detect_cb_stop() -> bool:
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=3)
        row = conn.execute(
            "SELECT status FROM circuit_breaker_state LIMIT 1"
        ).fetchone()
        conn.close()
        return row and row[0] == "STOP"
    except Exception:
        return False


def detect_omni_silence() -> bool:
    """OMNI hasn't synthesized in 20+ minutes during market hours."""
    if not _market_hours():
        return False
    try:
        resp = requests.get("http://localhost:8004/health", timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            mins_ago = data.get("last_synthesis_min_ago", 0)
            syntheses = data.get("syntheses_today", 0)
            # Only flag silence if we're past 10 AM (enough time to have synthesized)
            now_et = datetime.now(_ET)
            if now_et.hour >= 10 and mins_ago > 20:
                return True
    except Exception:
        pass
    return False


def detect_alpha_buffer_unhealthy() -> bool:
    """Alpha-buffer is up but resilience is UNHEALTHY for non-CB reasons."""
    try:
        resp = requests.get("http://localhost:8002/health", timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            cb = data.get("cb_status", "NORMAL")
            resilience = (data.get("resilience_status") or {}).get("status", "HEALTHY")
            # Only flag if unhealthy for non-CB reasons
            return resilience == "UNHEALTHY" and cb == "NORMAL"
    except Exception:
        pass
    return False


def detect_dlq_backlog() -> bool:
    """DLQ has 5+ failed executions waiting."""
    try:
        resp = requests.get(
            "http://localhost:8005/dlq/status",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=4,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("pending", 0) >= 5
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------

def run_scan() -> None:
    """Run one complete scan. Detect and fix everything found."""
    now = datetime.now(_ET)
    log.debug("Scan starting — %s ET", now.strftime("%H:%M:%S"))

    # ── 1. Service health — fix in startup order ──────────────────────────────
    for name, port, plist in SERVICES:
        if not _service_healthy(port):
            log.warning("DETECTED: %s is DOWN on port %d", name, port)
            fix_service_down(name, port, plist)
            time.sleep(2)  # brief pause between restarts

    # ── 2. VIX brake 999 (startup race condition) ─────────────────────────────
    if detect_vix_brake_999():
        log.warning("DETECTED: VIX brake 999 — startup race condition")
        fix_vix_brake_999()

    # ── 3. Circuit breaker STOP ───────────────────────────────────────────────
    if detect_cb_stop():
        log.warning("DETECTED: Circuit breaker STOP")
        fix_circuit_breaker_stop()

    # ── 4. OMNI synthesis silence ─────────────────────────────────────────────
    if detect_omni_silence():
        log.warning("DETECTED: OMNI synthesis silence > 20 min")
        fix_omni_silence()

    # ── 5. Alpha-buffer unhealthy ─────────────────────────────────────────────
    if detect_alpha_buffer_unhealthy():
        log.warning("DETECTED: Alpha-buffer UNHEALTHY")
        fix_alpha_buffer_unhealthy()

    # ── 6. DLQ backlog ────────────────────────────────────────────────────────
    if detect_dlq_backlog():
        log.warning("DETECTED: DLQ backlog >= 5 items")
        fix_drain_dlq()

    log.debug("Scan complete — %s ET", datetime.now(_ET).strftime("%H:%M:%S"))



# ---------------------------------------------------------------------------
# Deterministic Trading Engine — fires every 15 min during market hours
# ---------------------------------------------------------------------------

_last_det_window: str = ""   # track last fired window to prevent double-fire

def _deterministic_trading_loop() -> None:
    """
    Background thread: fires deterministic scoring window every 15 min.
    Replaces agent concordance cycle entirely.
    Ahmed directive May 2026: deterministic from A to Z.
    """
    import sys as _sys2
    _sys2.path.insert(0, "/Users/ahmedsadek/nexus/nexus-deterministic")
    global _last_det_window

    log.info("Deterministic trading loop started")

    while True:
        try:
            now = datetime.now(_ET)
            # Market hours only: 9:35 AM - 3:55 PM ET, weekdays
            is_market = (
                now.weekday() < 5 and
                (now.hour > 9 or (now.hour == 9 and now.minute >= 35)) and
                (now.hour < 15 or (now.hour == 15 and now.minute <= 55))
            )

            if is_market:
                # Build 15-minute window_id
                minute_slot = (now.minute // 15) * 15
                window_id   = now.strftime(f"%Y-%m-%d-%H{minute_slot:02d}")

                if window_id != _last_det_window:
                    _last_det_window = window_id
                    log.info("Deterministic window: %s", window_id)

                    try:
                        from alpha_buffer_integration import run_and_dispatch
                        result = run_and_dispatch(
                            window_id  = window_id,
                            direction  = "bullish",
                            max_trades = 5,
                        )
                        n_signals    = result.get("signals_found", 0)
                        n_dispatched = result.get("signals_dispatched", 0)
                        elapsed      = result.get("elapsed_seconds", 0)
                        tickers      = result.get("tickers", [])

                        log.info(
                            "Window %s: %d signals, %d dispatched to OMNI in %.1fs %s",
                            window_id, n_signals, n_dispatched, elapsed,
                            tickers or ""
                        )

                        if n_dispatched > 0:
                            msg = "<b>NEXUS DETERMINISTIC</b> Window " + window_id + " | " + str(n_dispatched) + " dispatched | " + ", ".join(tickers)
                            _fyi(msg)
                    except Exception as exc:
                        log.error("Deterministic window failed: %s", exc)
                        _fyi("DETERMINISTIC WINDOW FAILED: " + str(exc))

        except Exception as exc:
            log.error("Deterministic loop error: %s", exc)

        time.sleep(60)   # check every 60s, fires only on new window_id



# ---------------------------------------------------------------------------
# Universal Data Source Manager — monitors all external APIs
# ---------------------------------------------------------------------------

def _start_data_source_manager() -> None:
    """Start universal data source health monitoring."""
    import sys as _sys3
    _sys3.path.insert(0, "/Users/ahmedsadek/nexus")
    try:
        from shared.data_sources.manager import start_data_source_manager
        start_data_source_manager(port=8097)
        log.info("Universal Data Source Manager started — port 8097")
        _fyi("<b>DATA SOURCE MANAGER ONLINE</b>\nMonitoring: Polygon, ORATS, Alpaca, Alpha Vantage, Unusual Whales, Axiom, Telegram\nAuto-fallback active for all sources.")
    except Exception as exc:
        log.error("Data Source Manager failed: %s", exc)
        _fyi("DATA SOURCE MANAGER FAILED: " + str(exc))

def _start_deterministic_trader() -> None:
    """Start deterministic trading engine in background thread."""
    import threading as _thr
    t = _thr.Thread(
        target = _deterministic_trading_loop,
        daemon = True,
        name   = "deterministic-trader",
    )
    t.start()
    log.info("Deterministic trading engine started")
    _fyi("<b>DETERMINISTIC TRADER ONLINE</b>\nNo agents. Pure math. A to Z.")

def run_forever(interval_seconds: int = 60) -> None:
    """Run scan loop forever. This is the main daemon mode."""
    log.info("=" * 60)
    log.info("NEXUS AUTONOMOUS ACTION ENGINE — STARTED")
    log.info("Scan interval: %ds | Paper mode: %s", interval_seconds, IS_PAPER)
    log.info("=" * 60)
    _fyi(
        "🤖 <b>AUTONOMOUS ACTION ENGINE ONLINE</b>\n"
        f"Scanning every {interval_seconds}s\n"
        "Detect → Fix → Report. No permissions needed."
    )

    # Start Alpaca Guardian for all accounts
    _start_alpaca_guardians()
    # Start per-service self-sovereign health monitors
    _start_all_health_monitors()
    # Start agent heartbeat monitor in background thread
    _start_heartbeat_thread()
    # Start universal data source health monitor
    _start_data_source_manager()
    # Start deterministic trading engine (replaces agent concordance)
    _start_deterministic_trader()

    while True:
        try:
            run_scan()
        except Exception as exc:
            log.error("Scan error (continuing): %s", exc)
        time.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_alpaca_guardians = {}
_health_monitors  = []


def _launch_alpaca_status_api() -> None:
    import threading, json as _j
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import sys as _s
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/alpaca-status":
                try:
                    _s.path.insert(0, "/Users/ahmedsadek/nexus")
                    from shared.alpaca.registry import get_all_status
                    body = _j.dumps(get_all_status(), indent=2).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode())
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, *a): pass
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", 8098), H).serve_forever(),
        daemon=True, name="alpaca-status-api"
    ).start()
    log.info("Alpaca status API on port 8098")


def _start_alpaca_guardians() -> None:
    global _alpaca_guardians
    import sys as _sys
    _sys.path.insert(0, "/Users/ahmedsadek/nexus")
    try:
        from shared.alpaca.registry import start_all_guardians
        _alpaca_guardians = start_all_guardians()
        count   = len(_alpaca_guardians)
        healthy = sum(1 for g in _alpaca_guardians.values() if g.is_healthy)
        log.info("Alpaca Guardian: %d systems, %d healthy", count, healthy)
        _fyi("<b>ALPACA GUARDIAN ONLINE</b> %d/%d systems healthy. Ghost hunters active." % (healthy, count))
        _launch_alpaca_status_api()
    except Exception as exc:
        log.error("Alpaca Guardian failed to start: %s", exc)
        _fyi("ALPACA GUARDIAN FAILED: " + str(exc))


def _launch_health_mesh_api(monitors_list) -> None:
    import threading, json as _j
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import sys as _s
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health-mesh":
                try:
                    _s.path.insert(0, "/Users/ahmedsadek/nexus")
                    from shared.health.incident_store import get_health_mesh_status
                    body = _j.dumps(get_health_mesh_status(monitors_list), indent=2).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode())
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, *a): pass
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", 8099), H).serve_forever(),
        daemon=True, name="health-mesh-api"
    ).start()
    log.info("Health mesh API on port 8099")


def _start_all_health_monitors() -> None:
    global _health_monitors
    import sys as _sys
    _sys.path.insert(0, "/Users/ahmedsadek/nexus")
    try:
        from shared.health.service_monitors import start_all_monitors
        _health_monitors = start_all_monitors()
        log.info("Started %d service health monitors", len(_health_monitors))
        _fyi("<b>HEALTH MESH ONLINE</b> %d monitors. Detect-Fix-Dispatch-FYI" % len(_health_monitors))
        _launch_health_mesh_api(_health_monitors)
    except Exception as exc:
        log.error("Health monitors failed to start: %s", exc)
        _fyi("HEALTH MONITORS FAILED: " + str(exc))


def _start_heartbeat_thread() -> None:
    import threading, sys as _sys
    _sys.path.insert(0, "/Users/ahmedsadek/nexus/sentinel")
    try:
        from agent_heartbeat_monitor import run_heartbeat_loop
        t = threading.Thread(
            target=run_heartbeat_loop,
            args=(_fyi,),
            daemon=True,
            name="agent-heartbeat",
        )
        t.start()
        log.info("Agent heartbeat monitor started (background thread)")
    except Exception as exc:
        log.error("Failed to start heartbeat monitor: %s", exc)
        _fyi("HEARTBEAT MONITOR FAILED: " + str(exc))


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_scan()
    else:
        run_forever(interval_seconds=60)
# ---------------------------------------------------------------------------
# Agent Heartbeat Integration
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Health Mesh HTTP endpoint (port 8099)
# ---------------------------------------------------------------------------