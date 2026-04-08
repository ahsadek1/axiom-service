"""
heartbeat_emitter.py — Nexus Agent Heartbeat Emitter
=====================================================
Each agent imports this and calls start_heartbeat_emitter() at startup.
Pushes a heartbeat to the health monitor every 30 seconds.

If the agent process freezes, crashes, or goes silent — the health monitor
detects the missed heartbeat within 75 seconds and triggers the full
outage response sequence (diagnostic → alert → self-heal).

Usage:
    from shared.heartbeat_emitter import start_heartbeat_emitter
    start_heartbeat_emitter(service_name="Axiom")   # call once at startup

Author: OMNI 🌐
Date: 2026-04-08
"""

import os
import time
import threading
import datetime
import requests

# ── Config ────────────────────────────────────────────────────────────────────

HEALTH_MONITOR_URL  = os.getenv(
    "HEALTH_MONITOR_URL",
    "https://nexus-health-monitor.up.railway.app"   # set once deployed
)
HEARTBEAT_INTERVAL  = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "30"))
HEARTBEAT_ENDPOINT  = "/heartbeat"
HEARTBEAT_TIMEOUT   = 5     # seconds — don't block the agent if monitor is slow

# ── Internal state ─────────────────────────────────────────────────────────── 

_emitter_thread: threading.Thread = None
_running:        bool              = False
_last_success:   str               = None
_fail_count:     int               = 0

# ── Emitter ───────────────────────────────────────────────────────────────────

def _emit(service_name: str, version: str, loop_active: bool, extra: dict):
    """Send a single heartbeat POST to the health monitor."""
    global _last_success, _fail_count
    url = HEALTH_MONITOR_URL.rstrip("/") + HEARTBEAT_ENDPOINT
    try:
        payload = {
            "service":      service_name,
            "version":      version,
            "loop_active":  loop_active,
            "extra":        extra or {},
        }
        r = requests.post(url, json=payload, timeout=HEARTBEAT_TIMEOUT)
        if r.status_code == 200:
            _last_success = datetime.datetime.utcnow().isoformat()
            _fail_count   = 0
        else:
            _fail_count += 1
            print(f"[HEARTBEAT] ⚠️ Monitor returned {r.status_code} (fail #{_fail_count})")
    except requests.exceptions.ConnectionError:
        _fail_count += 1
        if _fail_count <= 3 or _fail_count % 10 == 0:
            print(f"[HEARTBEAT] ⚠️ Cannot reach health monitor (fail #{_fail_count}) — continuing normally")
    except requests.exceptions.Timeout:
        _fail_count += 1
        print(f"[HEARTBEAT] ⚠️ Health monitor timeout (fail #{_fail_count})")
    except Exception as e:
        _fail_count += 1
        print(f"[HEARTBEAT] ⚠️ Emit error: {e} (fail #{_fail_count})")


def _heartbeat_loop(
    service_name: str,
    version:      str,
    get_loop_active: callable,
    get_extra:       callable,
):
    """Background loop — emits heartbeat every HEARTBEAT_INTERVAL seconds."""
    global _running
    print(f"[HEARTBEAT] 💓 Emitter started for {service_name} → {HEALTH_MONITOR_URL} every {HEARTBEAT_INTERVAL}s")
    while _running:
        try:
            loop_active = get_loop_active() if get_loop_active else True
            extra       = get_extra()        if get_extra        else {}
            _emit(service_name, version, loop_active, extra)
        except Exception as e:
            print(f"[HEARTBEAT] Loop error: {e}")
        time.sleep(HEARTBEAT_INTERVAL)
    print(f"[HEARTBEAT] Emitter stopped for {service_name}")


# ── Public API ────────────────────────────────────────────────────────────────

def start_heartbeat_emitter(
    service_name:    str,
    version:         str      = "1.0.0",
    get_loop_active: callable = None,   # optional: lambda returning bool
    get_extra:       callable = None,   # optional: lambda returning dict of extra state
) -> threading.Thread:
    """
    Start the heartbeat emitter as a background daemon thread.
    Call once at service startup.

    Args:
        service_name:    Must match SERVICES key in health monitor: "Axiom" | "Alpha" | "Prime"
        version:         Service version string (informational)
        get_loop_active: Optional callable returning bool — is the main loop active?
        get_extra:       Optional callable returning dict — extra state to include in heartbeat

    Returns:
        The running Thread object.
    """
    global _emitter_thread, _running

    if _emitter_thread and _emitter_thread.is_alive():
        print(f"[HEARTBEAT] Emitter already running for {service_name}")
        return _emitter_thread

    _running = True
    _emitter_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(service_name, version, get_loop_active, get_extra),
        daemon=True,
        name=f"heartbeat-{service_name.lower()}",
    )
    _emitter_thread.start()
    return _emitter_thread


def stop_heartbeat_emitter():
    """Stop the emitter (called on graceful shutdown)."""
    global _running
    _running = False
    print("[HEARTBEAT] Emitter stop requested")


def emitter_status() -> dict:
    """Return current emitter status (for /health endpoint enrichment)."""
    return {
        "heartbeat_active":    _emitter_thread is not None and _emitter_thread.is_alive(),
        "last_heartbeat_sent": _last_success,
        "consecutive_failures": _fail_count,
        "monitor_url":         HEALTH_MONITOR_URL,
    }
