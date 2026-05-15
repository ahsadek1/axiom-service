#!/usr/bin/env python3
"""
nexus_sovereign_pulse.py — SOVEREIGN Full-Domain Parallel Pulse
================================================================
Checks all 20+ Nexus service ports in parallel.
Runs every 5 minutes via launchd — NO LLM overhead.
Runtime target: < 3 seconds.

Writes state to: /Users/ahmedsadek/nexus/data/sovereign_pulse_state.json
Posts to Alert Broker on any failure.

Author: OMNI 🌐 | Permanent root-cause fix | 2026-05-05
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE   = Path("/Users/ahmedsadek/nexus/data/sovereign_pulse_state.json")
LOG_FILE     = Path("/Users/ahmedsadek/nexus/logs/sovereign-pulse/stdout.log")
ALERT_BROKER = "http://localhost:9998/alert"
ALERT_SECRET = "ab_secret_f4e2d1c8b7a3e9f5d2c4b6a8e0f3d5c7b9a1e4f6d8c0b2a4e6f8d0c2b4a6e8"

BASE = "http://192.168.1.141"

SERVICES: list[dict] = [
    # Nexus V2 Core
    {"name": "axiom",          "url": f"{BASE}:8001/health", "critical": True},
    {"name": "alpha-buffer",   "url": f"{BASE}:8002/health", "critical": True},
    {"name": "prime-buffer",   "url": f"{BASE}:8003/health", "critical": False},
    {"name": "omni-v2",        "url": f"{BASE}:8004/health", "critical": True},
    {"name": "alpha-exec",     "url": f"{BASE}:8005/health", "critical": True},
    {"name": "prime-exec",     "url": f"{BASE}:8006/health", "critical": True},
    {"name": "oracle",         "url": f"{BASE}:8007/health", "critical": False},
    {"name": "sentinel",       "url": f"{BASE}:8010/health", "critical": False},
    # Scanners
    {"name": "cipher-scan",    "url": f"{BASE}:9001/health", "critical": False},
    {"name": "sage-scan",      "url": f"{BASE}:9002/health", "critical": False},
    {"name": "atlas-scan",     "url": f"{BASE}:9003/health", "critical": False},
    {"name": "atm-multiweek",  "url": f"{BASE}:9004/health", "critical": False},
    {"name": "atg-swing",      "url": f"{BASE}:9006/health", "critical": False},
    {"name": "nsp",            "url": f"{BASE}:9007/health", "critical": False},
    # Capital Router (Railway)
    {"name": "capital-router", "url": "https://alpha-capital-router-production.up.railway.app/health", "critical": True},
    # Alert Broker local
    {"name": "alert-broker",   "url": "http://localhost:9998/health", "critical": True},
    # Nexus Bus
    {"name": "nexus-bus",      "url": "http://localhost:9999/health", "critical": False},
]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    line = f"[{_now_utc()}] {msg}\n"
    sys.stdout.write(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line)
    except Exception:
        pass


def _check(svc: dict) -> dict:
    url     = svc["url"]
    timeout = 4
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
            body = json.loads(r.read()) if r.status == 200 else None
    except urllib.error.HTTPError as e:
        code, body = e.code, None
    except Exception:
        code, body = 0, None

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    healthy    = (200 <= code < 300)

    # Deeper: check execution_paused / circuit_breaker flags
    paused  = False
    breaker = False
    if isinstance(body, dict):
        paused  = body.get("execution_paused", False)
        _cb = body.get("circuit_breaker", False)
        if isinstance(_cb, dict):
            _cb = _cb.get("tripped", False)
        breaker = body.get("global_circuit_breaker", False) or bool(_cb)
        if paused or breaker:
            healthy = False

    return {
        "name":             svc["name"],
        "healthy":          healthy,
        "http_code":        code,
        "elapsed_ms":       elapsed_ms,
        "critical":         svc.get("critical", False),
        "execution_paused": paused,
        "circuit_breaker":  breaker,
        "checked_at":       _now_utc(),
    }


def _alert(title: str, body: str, level: str = "CRITICAL") -> None:
    payload = json.dumps({
        "source":    "sovereign-pulse",
        "level":     level,
        "title":     title,
        "body":      body,
        "dedup_key": f"sovereign-pulse:{title[:40]}",
        "targets":   ["ahmed", "nexus_health_group"],
    }).encode()
    req = urllib.request.Request(
        ALERT_BROKER, data=payload,
        headers={"Content-Type": "application/json", "X-Alert-Secret": ALERT_SECRET},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=4)
    except Exception as e:
        _log(f"[WARN] Alert Broker unreachable: {e}")


def run() -> int:
    t0 = time.monotonic()
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=len(SERVICES)) as ex:
        futures = {ex.submit(_check, svc): svc["name"] for svc in SERVICES}
        for fut in as_completed(futures, timeout=6):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                results[name] = {"name": name, "healthy": False, "http_code": 0,
                                 "critical": True, "error": str(e), "checked_at": _now_utc()}

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    failures   = [r for r in results.values() if not r["healthy"]]
    critical   = [r for r in failures if r.get("critical")]
    warnings   = [r for r in failures if not r.get("critical")]
    paused     = [r for r in results.values() if r.get("execution_paused")]
    breakers   = [r for r in results.values() if r.get("circuit_breaker")]

    # overall_healthy = no critical failures AND no execution pauses
    # circuit breakers on non-critical services = warning only (not overall failure)
    critical_breakers = [r for r in breakers if r.get("critical")]
    overall = len(critical) == 0 and len(paused) == 0 and len(critical_breakers) == 0

    state = {
        "schema":             "nexus.sovereign_pulse.v1",
        "checked_at":         _now_utc(),
        "elapsed_ms":         elapsed_ms,
        "overall_healthy":    overall,
        "critical_failures":  [r["name"] for r in critical],
        "warning_failures":   [r["name"] for r in warnings],
        "execution_paused":   [r["name"] for r in paused],
        "circuit_breakers":   [r["name"] for r in breakers],
        "services":           results,
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

    if critical:
        names  = ", ".join(r["name"] for r in critical)
        detail = "\n".join(f"  • {r['name']}: HTTP {r['http_code']} ({r.get('elapsed_ms',0)}ms)"
                           for r in critical)
        _alert(f"🔴 NEXUS PULSE CRITICAL: {names}", f"Critical services down:\n{detail}")
    if paused:
        names = ", ".join(paused)
        _alert(f"🔴 EXECUTION PAUSED: {names}",
               f"Services have execution_paused=true: {names}", level="CRITICAL")
    if breakers:
        bnames = ", ".join(r["name"] for r in breakers)
        _alert(f"🔴 CIRCUIT BREAKER: {bnames}",
               f"Circuit breaker OPEN on: {bnames}", level="CRITICAL")
    if warnings:
        names = ", ".join(r["name"] for r in warnings)
        _alert(f"🟡 NEXUS PULSE WARN: {names}",
               "\n".join(f"  • {r['name']}: HTTP {r['http_code']}" for r in warnings),
               level="WARNING")

    label = "✅" if overall else "🔴"
    crit_list  = [r["name"] for r in critical]
    _log(f"{label} elapsed={elapsed_ms}ms critical={crit_list} warnings={[r['name'] for r in warnings]}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(run())
