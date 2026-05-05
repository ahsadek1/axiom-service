"""
main.py — OMNI Service FastAPI Entry Point

Receives concordance from Alpha and Prime buffers.
Runs Quad Intelligence synthesis. Routes GO to execution.
Notifies Ahmed via Telegram on every synthesis.

Endpoints:
  POST /concordance       — Receive concordance from Alpha or Prime buffer
  GET  /health            — Always 200
  GET  /status            — Recent syntheses + service state
  GET  /synthesis/{id}    — Full synthesis detail by row ID
"""

import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor, Future as _Future
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.sovereign_comms import EscalationLevel, get_instructions, report
from shared.service_state import ServiceStateWriter as _StateWriter
_svc_state = _StateWriter("omni")  # GAP-002: durable state across restarts

import pytz
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import BASE_POSITION_SIZE, MIN_BRAINS_REQUIRED, load_settings
from axiom_client import assess_ticker, get_regime
from database import (
    get_conn,
    get_recent_syntheses,
    get_synthesis_result,
    init_db,
    mark_execution_dispatched,
    save_synthesis_result,
)
from execution_router import calculate_position_size, route_to_execution
from quad_intelligence import run_all_brains
from synthesis import build_context, compute_verdict, _maybe_alert_brain_degradation
from psychology_overlay import apply_psychology_overlay, PsychologyOverlayResult
import oracle_client
from telegram import send_axiom_block_alert, send_synthesis_card

# P0-A: Stale deploy detection
import hashlib as _hashlib
import hashlib as _hashlib, os as _os, glob as _glob

def _compute_module_hash() -> str:
    """Hash all *.py files in this service directory (excluding __pycache__).
    Returns 8-char hex digest. FLAW 1 fix: full module fingerprint, not just main.py.
    """
    _svc_dir = _os.path.dirname(_os.path.abspath(__file__))
    _files = sorted(
        f for f in _glob.glob(_os.path.join(_svc_dir, "*.py"))
        if "__pycache__" not in f
    )
    _h = _hashlib.md5()
    for _f in _files:
        try:
            with open(_f, "rb") as _fh:
                _h.update(_fh.read())
        except Exception:
            pass
    return _h.hexdigest()[:8]

_CODE_HASH = _compute_module_hash()

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("omni.main")


def _restore_omni_state() -> None:
    """GAP-002: Restore durable state across restarts via ServiceStateWriter."""
    prior = _svc_state.restore()
    if prior.get("sovereign_halted"):
        global _sovereign_halted
        _sovereign_halted = True
        logger.info("OMNI: restored sovereign_halted=True from prior session")


def _check_min_brains_required(value: int) -> None:
    """
    Assert that MIN_BRAINS_REQUIRED is at or above the safe minimum of 3.

    Called at startup to catch misconfigured thresholds before the service
    accepts any traffic.

    Args:
        value: The MIN_BRAINS_REQUIRED value to validate.

    Raises:
        RuntimeError: If value < 3.
    """
    if value < 3:
        raise RuntimeError(
            f"MIN_BRAINS_REQUIRED={value} is below safe minimum of 3. "
            "This is a system safety parameter — do not lower it."
        )

ET = pytz.timezone("America/New_York")

settings       = load_settings()
_auto_execute  = os.getenv("NEXUS_AUTO_EXECUTE", "false").lower() == "true"
logger.info("OMNI: NEXUS_AUTO_EXECUTE=%s", _auto_execute)

# ── SOVEREIGN Directive State ─────────────────────────────────────────────────
_sovereign_halted: bool = False   # HALT directive suspends concordance synthesis

# GAP-008: Concurrent synthesis worker pool (2026-04-29)
# 3 workers run synthesis in parallel. P1/STRONG_GO pathways are prioritised.
# Without this: a 90s brain timeout blocks all subsequent concordances.
# With this: 3 concordances process simultaneously — no backlog during peak hours.
_SYNTHESIS_POOL      = _ThreadPoolExecutor(max_workers=3, thread_name_prefix="omni-synth")
_PATHWAY_PRIORITY    = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}  # lower = higher priority
# GAP-14: Semaphore mirrors pool size — ensures release() is ALWAYS called (try/finally)
# even if _run_synthesis() raises uncaught exception inside a pool worker.
_SYNTHESIS_SEMAPHORE = __import__("threading").Semaphore(3)


def _dispatch_sovereign_instruction(instr: dict) -> None:
    """Execute a SOVEREIGN instruction. Supported: HALT, RESUME, STATUS, FLUSH."""
    global _sovereign_halted

    raw = instr.get("message", "").strip()
    directive = raw.split(":", 1)[0].strip().upper() if ":" in raw else raw.upper()

    logger.info("SOVEREIGN directive received — raw: %s", raw[:200])

    if directive == "HALT":
        _sovereign_halted = True
        _svc_state.write("sovereign_halted", True)  # GAP-002
        logger.warning("SOVEREIGN DIRECTIVE: HALT — synthesis suspended")
        report("omni", "ack", {"directive": "HALT", "status": "applied", "halted": True})
    elif directive == "RESUME":
        _sovereign_halted = False
        _svc_state.write("sovereign_halted", False)  # GAP-002
        logger.info("SOVEREIGN DIRECTIVE: RESUME — synthesis resumed")
        report("omni", "ack", {"directive": "RESUME", "status": "applied", "halted": False})
    elif directive == "STATUS":
        with _state_lock:
            snap = {
                "directive": "STATUS",
                "halted": _sovereign_halted,
                "auto_execute": _auto_execute,
                "syntheses_today": app_state.get("syntheses_today", 0),
                "go_verdicts_today": app_state.get("go_verdicts_today", 0),
                "last_synthesis_time": app_state.get("last_synthesis_time"),
            }
        report("omni", "status", snap)
        logger.info("SOVEREIGN DIRECTIVE: STATUS — reported back")
    elif directive == "FLUSH":
        with _state_lock:
            app_state["syntheses_today"] = 0
            app_state["go_verdicts_today"] = 0
            app_state["p4_dispatched_windows"] = set()
            app_state["synthesized_concordances"] = set()
            # FIX: reset consecutive_nogo + alert flag on daily boundary
            # Without this, consecutive_nogo_alerted=True from Friday suppresses Monday alerts
            app_state["consecutive_nogo"] = 0
            app_state["consecutive_nogo_alerted"] = False
            app_state["nogo_block_reasons"] = []
        logger.info("SOVEREIGN DIRECTIVE: FLUSH — daily counters and dedup sets cleared (incl. consecutive_nogo)")
        report("omni", "ack", {"directive": "FLUSH", "status": "applied"})
    else:
        logger.warning("SOVEREIGN DIRECTIVE: unrecognized '%s'", directive[:100])
        report("omni", "ack", {"directive": directive[:100], "status": "unrecognized"})
_state_lock    = threading.Lock()   # Pass B fix (V6): guards app_state counter mutations
app_state = {
    "settings":          settings,
    "start_time":        datetime.now(ET).isoformat(),
    "syntheses_today":   0,
    "go_verdicts_today": 0,
    "last_synthesis_time": None,       # Cipher fix: track last synthesis timestamp for silence detection
    "p4_dispatched_windows": set(),  # INV-15: (ticker, window_id) pairs — max 1 P4 per ticker per window
    "synthesized_concordances": set(),  # Dedup: (ticker, window_id, direction, system, pathway) — max 1 synthesis per concordance pathway per window [GENESIS 2026-04-20]
    # Self-audit: consecutive NO_GO tracking (OMNI Upgrade v2 — Apr 30 2026)
    "consecutive_nogo": 0,
    "consecutive_nogo_alerted": False,
    "nogo_block_reasons": [],   # last 5 blocking reason snippets for pattern detection
}

# ── Synthesis Silence Detector ────────────────────────────────────────────────
# Cipher fix: background thread monitors synthesis activity during market hours.
# If no synthesis fires in SILENCE_THRESHOLD_MIN minutes during open hours,
# alert Ahmed + Sovereign so the issue is caught immediately — not 2.5h later.
_SILENCE_THRESHOLD_MIN = 20   # alert after 20 min silence during market hours
_silence_alerted = False       # rate-limit: one alert per silence episode

# ── Block 2: STANDBY mode ─────────────────────────────────────────────────────
# OMNI enters STANDBY if the Anthropic API key fails at startup.
# /health returns 200 with status: "standby"; /concordance returns 503.
# A background thread retries every 30s and clears standby on success.
_omni_standby_active: bool = False
_omni_standby_reason: str  = ""


def _omni_preflight_check() -> "tuple[bool, str]":
    """Block 2: Verify Anthropic API key is valid before accepting concordances.

    Returns:
        (ok: bool, reason: str) — reason is empty when ok=True.
    """
    try:
        from shared.api_key_validator import ApiKeyValidator as _AKV
        _r = _AKV().validate_anthropic(settings.anthropic_api_key)
        if _r.status == "failed":
            return False, f"Anthropic API key invalid: {_r.message}"
        return True, ""
    except Exception as exc:
        return False, f"Anthropic preflight error: {exc}"


def _omni_preflight_retry_loop() -> None:
    """Block 2: Background thread — retry Anthropic preflight every 30s until ACTIVE."""
    global _omni_standby_active, _omni_standby_reason
    import time as _time
    while True:
        _time.sleep(30)
        with _state_lock:
            if not _omni_standby_active:
                return
        ok, reason = _omni_preflight_check()
        if ok:
            with _state_lock:
                _omni_standby_active = False
                _omni_standby_reason = ""
            logger.info("Block 2: OMNI STANDBY cleared — Anthropic API key now valid")
            try:
                from shared.notification_router import notify_info as _ni
                _ni("omni", "OMNI Active", "Anthropic API key validated — OMNI exiting STANDBY")
            except Exception:
                pass
            return
        logger.warning("Block 2: OMNI preflight retry failed — still in STANDBY: %s", reason)


def _fire_consecutive_nogo_alert(
    count: int,
    reasons: list,
    bot_token: str,
    chat_id: str,
    ticker: str,
) -> None:
    """
    OMNI Self-Audit: fire alert when consecutive NO_GO threshold is hit during market hours.

    Called in a background thread — never blocks synthesis.
    Catches systematic bias (bad context, miscalibrated brain, stale data)
    within one scan cycle instead of at end of day.
    """
    import requests as _req
    _log = logging.getLogger("omni.self_audit")

    # Detect dominant blocking pattern from recent reasons
    all_reasons = " | ".join(reasons).lower()
    if "win rate" in all_reasons or "historical" in all_reasons or "50%" in all_reasons:
        pattern = ("⚠️ CONTEXT CONTAMINATION: 'historical win rate' in NO_GO reasons. "
                   "System win rate is being used to block individual trades. "
                   "Check build_context() in synthesis.py.")
    elif "in_pool" in all_reasons or "not in axiom" in all_reasons:
        pattern = "⚠️ AXIOM POOL: Repeated Axiom pool rejections. Check pool size or Axiom health."
    elif "echo" in all_reasons:
        pattern = "⚠️ ECHO CHAMBER: Recurring echo chamber flag. Agent scoring may be over-correlated."
    elif "timeout" in all_reasons or "error" in all_reasons:
        pattern = "⚠️ API ERRORS: Brain timeouts/errors recurring. Check network or API keys."
    else:
        pattern = "❓ No single dominant pattern. Review verdict_notes in omni.db manually."

    msg = (
        f"🔴 OMNI SELF-AUDIT ALERT\n"
        f"{count} consecutive NO-GO verdicts during market hours\n"
        f"Last ticker: {ticker}\n\n"
        f"{pattern}\n\n"
        f"Recent blocking notes:\n"
        + "\n".join(f"• {r[:120]}" for r in reasons[-3:] if r)
        + "\n\nAction required: diagnose and fix NOW. Do not wait until EOD."
    )
    _log.warning("SELF-AUDIT TRIGGER: %d consecutive NO-GOs | ticker=%s | pattern=%s",
                 count, ticker, pattern[:80])

    if not bot_token:
        return
    try:
        _req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=5,
        )
    except Exception as _e:
        _log.warning("Self-audit alert send failed: %s", _e)


def _is_market_hours() -> bool:
    """Return True if current ET time is within market hours (9:30–16:00 Mon–Fri)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close


def _check_canary_gate() -> Optional[str]:
    """
    CANARY gate check: read today's canary_status.json.

    Returns None if trading is cleared (pass or canary not applicable).
    Returns a block reason string if canary failed and synthesis should be blocked.

    Policy:
    - status=PASS    -> allow (trading cleared)
    - status=RUNNING -> allow (canary mid-run, don't block)
    - status=FAIL    -> BLOCK (canary explicitly failed today)
    - not run yet    -> allow (log only, don't hard-block on missing file)
    """
    import json as _json
    import os as _os
    canary_path = "/Users/ahmedsadek/nexus/data/canary_status.json"
    today = datetime.now(ET).strftime("%Y-%m-%d")
    try:
        if not _os.path.exists(canary_path):
            logger.info("OMNI CANARY: status file not found -- allowing synthesis (canary not yet run)")
            return None
        with open(canary_path) as _f:
            data = _json.load(_f)
        file_date = data.get("date", "")
        if file_date != today:
            logger.info(
                "OMNI CANARY: status file is stale (%s, today=%s) -- allowing synthesis",
                file_date, today,
            )
            return None
        status = data.get("status", "")
        if status == "PASS":
            return None   # Trading cleared
        if status == "RUNNING":
            return None   # Canary mid-run -- don't block
        if status == "FAIL":
            detail = data.get("detail", "unknown failure")
            return f"CANARY FAILED today ({today}): {detail}"
        return None   # Unknown status -- allow
    except Exception as e:
        logger.warning("OMNI CANARY: gate check failed (%s) -- allowing synthesis", e)
        return None


def _process_dlq_cycle() -> None:
    """
    H-19: Process execution DLQ — retry failed trades with backoff.

    Wires execution_dlq.process_dlq() to the alpha-execution /execute route.
    Also checks for SLA breach (pending entry > 30 min) and alerts SOVEREIGN.
    """
    try:
        from execution_dlq import process_dlq, get_dlq_stats
        from execution_router import route_execution

        def _route_fn(payload: dict) -> tuple[bool, Optional[str]]:
            """Route a DLQ retry through the execution router."""
            try:
                return route_execution(payload)
            except Exception as _re:
                return False, str(_re)

        process_dlq(_route_fn)

        # H-19: SLA alert — if any entry pending > 30 min, alert SOVEREIGN
        stats = get_dlq_stats()
        pending = stats.get("pending", 0)
        if pending > 0:
            logger.warning("H-19: DLQ has %d pending entries", pending)
            # Check for SLA breach (pending > 30 min)
            try:
                import sqlite3 as _sq
                from execution_dlq import DLQ_DB_PATH
                from datetime import datetime, timezone, timedelta
                with _sq.connect(DLQ_DB_PATH) as _conn:
                    _conn.row_factory = _sq.Row
                    _breach = _conn.execute("""
                        SELECT COUNT(*) as cnt FROM execution_dlq
                        WHERE status = 'pending'
                        AND created_at <= ?
                    """, ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),)).fetchone()
                    if _breach and _breach["cnt"] > 0:
                        _sov(f"⚠️ H-19 DLQ SLA BREACH: {_breach['cnt']} entries pending > 30 min. "
                             f"Immediate investigation required.")
            except Exception as _sla_e:
                logger.warning("H-19: SLA check failed: %s", _sla_e)
    except Exception as e:
        logger.warning("DLQ cycle error: %s", e)


def _trade_blocker_watchdog() -> None:
    """
    MANDATE v2 (2026-04-28): Autonomously resolve trade blockers every 60s.
    Identifies failures and fixes them immediately — never reports and waits.
    """
    import time as _time
    import requests as _req

    ALPACA_BASE  = "https://paper-api.alpaca.markets"
    ALPACA_HDRS  = {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY",""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY",""),
    }
    NEXUS_SECRET  = os.environ.get("NEXUS_SECRET","")
    PRIME_SECRET  = os.environ.get("NEXUS_PRIME_SECRET", NEXUS_SECRET)
    SOVEREIGN_BUS = os.environ.get("SOVEREIGN_BUS_URL","http://192.168.1.141:9999")
    ALPHA_URL     = os.environ.get("ALPHA_EXECUTION_URL","http://localhost:8005")
    PRIME_URL     = os.environ.get("PRIME_EXECUTION_URL","http://localhost:8006")

    def _sov(msg: str) -> None:
        try:
            _req.post(f"{SOVEREIGN_BUS}/send",
                      json={"from":"omni","to":"sovereign","message":msg}, timeout=5)
        except Exception: pass

    def _resume_paused() -> None:
        for name, url, hdr, sec in [
            ("alpha", ALPHA_URL, "X-Nexus-Secret", NEXUS_SECRET),
            ("prime", PRIME_URL, "X-Nexus-Prime-Secret", PRIME_SECRET),
        ]:
            try:
                h = _req.get(f"{url}/health", timeout=5)
                if h.ok and h.json().get("execution_paused"):
                    r = _req.post(f"{url}/resume", headers={hdr: sec}, timeout=5)
                    logger.warning("WATCHDOG: %s paused → /resume sent resumed=%s",
                                   name, r.json().get("resumed") if r.ok else False)
                    _sov(f"🔧 WATCHDOG: {name} paused → resumed")
            except Exception as e:
                logger.debug("Watchdog pause-check %s: %s", name, e)

    def _retry_failed_executions() -> None:
        """Retry GO/STRONG_GO syntheses that failed execution in last 10 min."""
        try:
            import sqlite3 as _sq
            db_path = os.environ.get("OMNI_DB_PATH","/Users/ahmedsadek/nexus/data/omni.db")
            conn = _sq.connect(db_path)
            conn.row_factory = _sq.Row
            failed = conn.execute("""
                SELECT ticker, pathway, system, window_id,
                       agent_weighted_score, votes_go, verdict, execution_response
                FROM synthesis_results
                WHERE datetime(created_at) > datetime('now','-10 minutes')
                AND execution_response NOT LIKE 'HTTP 200%'
                AND execution_response NOT LIKE 'HTTP 20%'
                AND execution_response NOT LIKE '429:TICKER_DUPLICATE%'
                AND execution_response NOT LIKE '429:CONCURRENT_CAP%'
                AND execution_response IS NOT NULL
                AND verdict IN ('GO','STRONG_GO')
                ORDER BY created_at DESC LIMIT 3
            """).fetchall()
            conn.close()
            for row in failed:
                url = ALPHA_URL if row["system"]=="alpha" else PRIME_URL
                hdr = "X-Nexus-Secret" if row["system"]=="alpha" else "X-Nexus-Prime-Secret"
                sec = NEXUS_SECRET if row["system"]=="alpha" else PRIME_SECRET
                payload = {
                    "ticker":            row["ticker"],
                    "direction":         "bullish",
                    "system":            row["system"],
                    "pathway":           row["pathway"],
                    "agent_scores":      {},
                    "weighted_score":    row["agent_weighted_score"],
                    "verdict":           row["verdict"],
                    "votes_go":          row["votes_go"],
                    "sizing_mult":       0.75,
                    "position_size_usd": 1500,
                    "window_id":         row["window_id"] + "-wr",
                    "auto_execute":      True,
                }
                try:
                    resp = _req.post(f"{url}/execute", json=payload,
                                     headers={hdr: sec}, timeout=30)
                    result = resp.json()
                    executed = result.get("executed", False)
                    logger.warning(
                        "WATCHDOG RETRY: %s/%s was=%s → HTTP %d executed=%s",
                        row["ticker"], row["system"],
                        str(row["execution_response"])[:50],
                        resp.status_code, executed,
                    )
                    _sov(
                        f"🔧 WATCHDOG RETRY: {row['ticker']} {row['system'].upper()} "
                        f"prev_failure={str(row['execution_response'])[:60]} "
                        f"→ executed={executed}"
                    )
                except Exception as retry_err:
                    logger.error("WATCHDOG retry error for %s: %s", row["ticker"], retry_err)
        except Exception as e:
            logger.debug("Watchdog retry-failed error: %s", e)

    def _check_metric_thresholds() -> None:
        """
        GAP-006: Real-time metric threshold monitoring.
        Checks key system metrics every 60s and acts autonomously when thresholds are breached.
        No Prometheus needed — reads directly from our SQLite DBs.

        Thresholds:
          - Execution failure rate >30% in last 30 min → alert SOVEREIGN, pause synthesis
          - 422 rejection rate >50% in last 30 min → recalibrate or alert
          - Brain error rate >40% → alert SOVEREIGN (API issue)
          - Concordances with dispatch_attempts>=3 (lost) > 0 → retry immediately
          - Alpha-exec execution_paused → resume immediately (belt+suspenders with _resume_paused)
        """
        import sqlite3 as _sq

        # --- 1. Execution failure rate ---
        try:
            omni_db = os.environ.get("OMNI_DB_PATH", "/Users/ahmedsadek/nexus/data/omni.db")
            conn = _sq.connect(omni_db); conn.row_factory = _sq.Row
            window = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN execution_response LIKE 'HTTP 20%' THEN 1 ELSE 0 END) as ok,
                    SUM(CASE WHEN execution_response NOT LIKE 'HTTP 20%'
                             AND execution_response NOT LIKE '429:TICKER_DUPLICATE%'
                             AND execution_response NOT LIKE '429:CONCURRENT_CAP%'
                             AND verdict IN ('GO','STRONG_GO')
                             AND execution_response IS NOT NULL THEN 1 ELSE 0 END) as failed
                FROM synthesis_results
                WHERE datetime(created_at) > datetime('now','-30 minutes')
                AND verdict IN ('GO','STRONG_GO')
            """).fetchone()
            conn.close()
            total = window["total"] or 0
            failed = window["failed"] or 0
            if total >= 3 and failed / total > 0.30:
                logger.critical(
                    "THRESHOLD BREACH: execution failure rate %.0f%% (threshold: 30%%) in last 30min",
                    100 * failed / total,
                )
                _sov(
                    f"🚨 OMNI THRESHOLD BREACH: Execution failure rate {100*failed//total}% "
                    f"({failed}/{total} GOs failed in last 30min). "
                    f"Investigating automatically."
                )
        except Exception as e:
            logger.debug("Threshold check (exec rate) error: %s", e)

        # --- 2. Brain error rate ---
        try:
            omni_db = os.environ.get("OMNI_DB_PATH", "/Users/ahmedsadek/nexus/data/omni.db")
            conn = _sq.connect(omni_db); conn.row_factory = _sq.Row
            brain_row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN claude_error IS NOT NULL OR gemini_error IS NOT NULL
                                     OR o3mini_error IS NOT NULL OR deepseek_error IS NOT NULL
                                THEN 1 ELSE 0 END) as errors
                FROM synthesis_results
                WHERE datetime(created_at) > datetime('now','-30 minutes')
            """).fetchone()
            conn.close()
            total = brain_row["total"] or 0
            errors = brain_row["errors"] or 0
            if total >= 3 and errors / total > 0.40:
                logger.warning(
                    "THRESHOLD: brain error rate %.0f%% (threshold: 40%%) in last 30min",
                    100 * errors / total,
                )
                _sov(
                    f"⚠️ OMNI: Brain error rate {100*errors//total}% in last 30min. "
                    f"API issues likely. Synthesis degraded."
                )
        except Exception as e:
            logger.debug("Threshold check (brain rate) error: %s", e)

        # --- 3. Lost concordances (dispatch_attempts >= 3) ---
        try:
            buf_db = os.environ.get("ALPHA_BUFFER_DB", "/Users/ahmedsadek/nexus/data/alpha_buffer.db")
            conn = _sq.connect(buf_db); conn.row_factory = _sq.Row
            lost = conn.execute("""
                SELECT COUNT(*) as n FROM concordance_results
                WHERE omni_dispatched=0 AND dispatch_attempts >= 3
                AND datetime(created_at) > datetime('now','-60 minutes')
            """).fetchone()["n"]
            conn.close()
            if lost > 0:
                logger.error(
                    "THRESHOLD: %d concordance(s) lost (max retries exhausted) in last 60min",
                    lost,
                )
                _sov(
                    f"🚨 OMNI THRESHOLD: {lost} concordance(s) lost to dispatch failures "
                    f"(all retries exhausted). Trade opportunities permanently missed. "
                    f"Investigating OMNI webhook connectivity."
                )
        except Exception as e:
            logger.debug("Threshold check (lost concordances) error: %s", e)

        # --- 4. DLQ stats ---
        try:
            from execution_dlq import get_dlq_stats as _gs
            stats = _gs()
            if stats.get("pending", 0) > 5:
                logger.warning("DLQ backlog: %d pending entries", stats["pending"])
                _sov(f"⚠️ Execution DLQ backlog: {stats['pending']} entries pending retry")
        except Exception as e:
            logger.debug("Threshold check (DLQ) error: %s", e)

    def _scan_log_for_errors() -> None:
        """
        Scan recent service logs for CRITICAL/ERROR patterns.
        Any CRITICAL log = immediate investigation and fix.
        This ensures OMNI does not need an external reminder to act.
        """
        import subprocess as _sp
        import re
        checks = [
            ("/Users/ahmedsadek/nexus/logs/alpha-buffer/stderr.log",   "alpha-buffer"),
            ("/Users/ahmedsadek/nexus/logs/alpha-execution/stderr.log", "alpha-execution"),
            ("/Users/ahmedsadek/nexus/logs/prime-execution/stderr.log", "prime-execution"),
            ("/Users/ahmedsadek/nexus/logs/omni/stderr.log",            "omni"),
        ]
        for log_path, service in checks:
            try:
                # GAP-LOG-FIX: only scan recent lines (last 200) to avoid
                # old historical entries flooding the watchdog with false alarms
                tail_result = _sp.run(
                    ["tail", "-200", log_path],
                    capture_output=True, text=True, timeout=3
                )
                recent_content = tail_result.stdout if tail_result.returncode == 0 else ""
                # GENESIS-FIX-WATCHDOG-001 2026-04-29: Only count lines where CRITICAL
                # is the log *level* (not the watchdog's own scan reports that say
                # 'has N CRITICAL entries' — those are WARNING level and cause
                # self-inflating counts each pass).
                # GENESIS-FIX-WATCHDOG-002 2026-04-30: start_time filter (superseded).
                # GENESIS-FIX-WATCHDOG-003 2026-04-30: Replace start_time filter with
                # 10-minute recency window. Implements CIPHER P2 dispatch (intervention
                # id:77): stale CRITICALs > 10min old are suppressed as acknowledged.
                # A live CRITICAL still fires every 60s for up to 10min — enough for
                # response. Root cause of this fix: 13:51:52 post-restart false-positive
                # silence CRITICALs passed start_time filter by only 63s and flooded
                # SOVEREIGN bus for 90+ min after GENESIS-FIX-RESTART-001 was deployed.
                # Log format: '2026-04-29 14:23:18,499 CRITICAL omni.main: ...'
                from datetime import datetime as _dt, timedelta as _td
                _ten_min_ago_str = (_dt.now() - _td(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
                critical_lines = [
                    line for line in recent_content.split("\n")
                    if re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ CRITICAL ', line)
                    and line[:19] >= _ten_min_ago_str
                ]
                count = len(critical_lines)
                if count > 0:
                    recent = type('R', (), {'stdout': recent_content, 'returncode': 0})()
                    lines = recent.stdout.strip().split("\n")[-5:]
                    logger.warning(
                        "WATCHDOG LOG SCAN: %s has %d CRITICAL entries. Recent: %s",
                        service, count, " | ".join(l[-100:] for l in lines if l)
                    )
                    _sov(
                        f"⚠️ WATCHDOG LOG SCAN: {service} has {count} CRITICAL log entries.\n"
                        f"Recent: {chr(10).join(lines[-3:])}"
                    )
            except Exception as e:
                logger.debug("Log scan error for %s: %s", service, e)

    logger.info("OMNI trade-blocker watchdog started (60s interval)")
    while True:
        try:
            _time.sleep(60)
            if not _is_market_hours():
                _time.sleep(180)
                continue
            _resume_paused()
            _retry_failed_executions()
            _scan_log_for_errors()
            _check_metric_thresholds()  # GAP-006
            # GAP-003: Process execution DLQ — retry failed trades with backoff
            _process_dlq_cycle()
        except Exception as e:
            logger.warning("Trade-blocker watchdog error: %s", e)


def _synthesis_silence_watcher() -> None:
    """
    Background daemon thread: detect OMNI synthesis silence during market hours.

    Polls every 60s. If no synthesis has fired in _SILENCE_THRESHOLD_MIN minutes
    while the market is open, sends a Telegram alert to Ahmed and posts to Sovereign
    via the message bus. Resets the alert flag after synthesis resumes.
    """
    global _silence_alerted
    while True:
        try:
            time.sleep(60)
            if not _is_market_hours():
                _silence_alerted = False  # reset so alert fires fresh next open
                continue

            with _state_lock:
                last_ts = app_state["last_synthesis_time"]

            if last_ts is None:
                # No synthesis since startup — check how long we've been running
                startup_str = app_state["start_time"]
                try:
                    startup_dt = datetime.fromisoformat(startup_str)
                    elapsed_min = (datetime.now(ET) - startup_dt).total_seconds() / 60
                except Exception:
                    elapsed_min = 0
                if elapsed_min < _SILENCE_THRESHOLD_MIN:
                    continue  # still warming up
                # Running long enough with zero syntheses — treat as silence
                silent_min = elapsed_min
            else:
                silent_min = (time.time() - last_ts) / 60

            if silent_min >= _SILENCE_THRESHOLD_MIN:
                # GENESIS 2026-05-04: THESIS DEFENSIVE awareness.
                # Before firing a CRITICAL silence alert, check whether THESIS has
                # zeroed all sizing (sizing_multiplier=0.0). If so, silence is EXPECTED —
                # no concordances will reach OMNI because THESIS blocked them upstream.
                # Firing CRITICAL during intentional stand-aside days creates false-alarm
                # noise and erodes signal quality of CRITICAL alerts.
                _thesis_defensive = False
                try:
                    import sqlite3 as _sq3
                    _ch_db = "/Users/ahmedsadek/nexus/data/chronicle.db"
                    if os.path.exists(_ch_db):
                        _ch = _sq3.connect(_ch_db, timeout=3)
                        _ch.row_factory = _sq3.Row
                        _th_row = _ch.execute(
                            "SELECT sizing_multiplier, risk_reward_gate "
                            "FROM thesis_context ORDER BY created_at DESC LIMIT 1"
                        ).fetchone()
                        _ch.close()
                        if _th_row and float(_th_row["sizing_multiplier"]) == 0.0:
                            _thesis_defensive = True
                            logger.info(
                                "OMNI silence (%0.f min) during THESIS DEFENSIVE posture "
                                "(sizing_multiplier=0.0, gate=%s) — expected stand-aside behavior. "
                                "No CRITICAL alert fired.",
                                silent_min, _th_row["risk_reward_gate"],
                            )
                except Exception as _te:
                    pass  # Chronicle unreachable — proceed with normal alert logic

                if not _silence_alerted and not _thesis_defensive:
                    _silence_alerted = True
                    logger.critical(
                        "OMNI SILENCE DETECTED: no synthesis in %.0f min during market hours",
                        silent_min,
                    )
                    # Mandate v2: diagnose and act, not just alert
                    # Check if concordances are being received but synthesis is failing
                    try:
                        import sqlite3 as _sq
                        _buf_db = os.environ.get("ALPHA_BUFFER_DB","/Users/ahmedsadek/nexus/data/alpha_buffer.db")
                        _conn = _sq.connect(_buf_db)
                        _recent_conc = _conn.execute(
                            "SELECT COUNT(*) FROM concordance_results WHERE datetime(created_at)>datetime('now','-30 minutes')"
                        ).fetchone()[0]
                        _conn.close()
                        _diagnosis = (
                            f"Buffer has {_recent_conc} concordance(s) in last 30min. "
                            f"Pool workers={_SYNTHESIS_POOL._max_workers}. "
                            f"Syntheses_today={app_state['syntheses_today']}."
                        )
                        if _recent_conc > 0 and app_state["syntheses_today"] == 0:
                            _diagnosis += " LIKELY CAUSE: synthesis pool worker crashing silently."
                        elif _recent_conc == 0:
                            _diagnosis += " LIKELY CAUSE: no concordances reaching OMNI — check alpha-buffer dispatch."
                    except Exception as _de:
                        _diagnosis = f"Diagnosis failed: {_de}"

                    logger.critical("SILENCE DIAGNOSIS: %s", _diagnosis)
                elif _thesis_defensive:
                    # Don't set _silence_alerted so that if THESIS posture changes mid-day
                    # and silence continues, the alert fires correctly.
                    pass

                    # Alert Ahmed via Telegram — deduped (30-min cooldown via alert_ahmed)
                    try:
                        from shared.resilience.alerts import alert_ahmed as _aa
                        _aa(
                            f"OMNI SILENCE: No synthesis in {silent_min:.0f} min during market hours.\n"
                            f"Diagnosis: {_diagnosis}",
                            key="omni-silence-alert",
                            severity="CRITICAL",
                        )
                    except Exception as _te:
                        logger.error("Silence alert notification failed: %s", _te)
                    # Also notify Sovereign via message bus
                    try:
                        import requests as _req
                        _req.post(
                            "http://192.168.1.141:9999/send",
                            json={
                                "from": "omni",
                                "to":   "sovereign",
                                "message": (
                                    f"OMNI SILENCE ALERT: no synthesis in {silent_min:.0f} min "
                                    "during market hours. Synthesis loop may be stalled."
                                ),
                            },
                            timeout=3,
                        )
                    except Exception:
                        pass
            else:
                # Synthesis is flowing — reset alert flag so next silence episode fires
                if _silence_alerted:
                    logger.info("OMNI silence resolved — synthesis resumed")
                _silence_alerted = False

        except Exception as _watcher_exc:
            logger.error("Silence watcher error: %s", _watcher_exc)


def verify_secret(secret_header: str) -> None:
    """
    Verify the caller's secret matches the configured NEXUS_WEBHOOK_SECRET.
    Uses constant-time comparison to prevent timing attacks (Cipher Finding 3).

    Raises:
        HTTPException: 403 if missing or invalid.
    """
    import secrets as _sec
    if not secret_header or not _sec.compare_digest(secret_header, settings.nexus_secret):
        raise HTTPException(status_code=403, detail="Forbidden")


def verify_concordance_auth(
    x_nexus_secret: str,
    x_nexus_prime_secret: str,
) -> None:
    """
    Validate inbound /concordance auth for BOTH Alpha and Prime Buffer callers.

    Cipher Finding 4 (INV-11 rotation fix): the original `active_secret or` pattern
    validated both headers against nexus_secret only. On secret rotation this silently
    breaks Prime trading. Each header is now validated against its own secret using
    constant-time comparison.

    Alpha Buffer sends X-Nexus-Secret  → validated against nexus_secret.
    Prime Buffer sends X-Nexus-Prime-Secret → validated against nexus_prime_secret.
    """
    import secrets as _sec
    alpha_ok = bool(x_nexus_secret and _sec.compare_digest(x_nexus_secret, settings.nexus_secret))
    prime_ok = bool(x_nexus_prime_secret and _sec.compare_digest(
        x_nexus_prime_secret, settings.nexus_prime_secret
    ))
    if not (alpha_ok or prime_ok):
        raise HTTPException(status_code=403, detail="Forbidden")



# ── SOVEREIGN Continuous Comms Loop ──────────────────────────────────────────
_comms_stop_omni = threading.Event()
SOVEREIGN_POLL_INTERVAL_S      = 30   # Poll SOVEREIGN inbox every 30 seconds
SOVEREIGN_HEARTBEAT_INTERVAL_S = 300  # Push heartbeat every 5 minutes


def _sovereign_comms_loop_omni() -> None:
    """
    Daemon thread: polls SOVEREIGN inbox every 30s and pushes a heartbeat
    every 5 minutes. Never raises.
    """
    last_heartbeat: float = 0.0
    logger.info("SOVEREIGN comms loop started (poll=%ds heartbeat=%ds)",
                SOVEREIGN_POLL_INTERVAL_S, SOVEREIGN_HEARTBEAT_INTERVAL_S)
    while not _comms_stop_omni.is_set():
        now = time.monotonic()
        try:
            instructions = get_instructions("omni")
            if instructions:
                logger.info("SOVEREIGN comms: %d directive(s) received", len(instructions))
                for instr in instructions:
                    _dispatch_sovereign_instruction(instr)
            if now - last_heartbeat >= SOVEREIGN_HEARTBEAT_INTERVAL_S:
                with _state_lock:
                    snap = {
                        "event":               "heartbeat",
                        "halted":              _sovereign_halted,
                        "syntheses_today":     app_state.get("syntheses_today", 0),
                        "go_verdicts_today":   app_state.get("go_verdicts_today", 0),
                        "last_synthesis_time": app_state.get("last_synthesis_time"),
                        "ts":                  __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    }
                report("omni", "AUTONOMOUS", snap)
                last_heartbeat = now
        except Exception as exc:  # noqa: BLE001
            logger.warning("SOVEREIGN comms loop error: %s", exc)
        _comms_stop_omni.wait(SOVEREIGN_POLL_INTERVAL_S)
    logger.info("SOVEREIGN comms loop stopped")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize DB on startup."""
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from shared.db_guard import assert_unique_db_path  # S4: collision guard
    assert_unique_db_path("omni", settings.omni_db_path)
    logger.info("OMNI service starting...")
    # G8 SYS-1: MIN_BRAINS_REQUIRED safety guard — must be ≥ 3 for system integrity
    _check_min_brains_required(MIN_BRAINS_REQUIRED)
    logger.info("OMNI safety check: MIN_BRAINS_REQUIRED=%d ✓", MIN_BRAINS_REQUIRED)
    _restore_omni_state()  # GAP-002: restore durable state
    # G9 SYS-2: Auth registry validation
    from shared.auth_registry import validate_service_auth_config, AuthConfigError
    try:
        validate_service_auth_config("omni")
        logger.info("Auth registry validation passed for OMNI")
    except AuthConfigError as e:
        logger.critical("Auth config invalid: %s", e)
    # G11 SYS-4 + Block 2: Validate brain API keys at startup.
    # Anthropic is CRITICAL (primary brain) — BLOCKING gate per RESILIENCE_SPEC_v2.
    # Gemini / DeepSeek / OpenAI remain warn-only (non-critical fallback brains).
    from shared.api_key_validator import ApiKeyValidator, ValidationResult as _VR
    _validator = ApiKeyValidator()
    _anthropic_result = _validator.validate_anthropic(settings.anthropic_api_key)
    if _anthropic_result.status == "failed":
        # Block 2: Anthropic failure — enter STANDBY, reject /concordance with 503
        global _omni_standby_active, _omni_standby_reason
        logger.critical(
            "Block 2: Anthropic API key FAILED — OMNI entering STANDBY. (%s)",
            _anthropic_result.message,
        )
        _omni_standby_active = True
        _omni_standby_reason = f"Anthropic API key invalid: {_anthropic_result.message}"
        try:
            from shared.resilience.alerts import alert_ahmed as _aa
            _aa(
                f"OMNI entered STANDBY — Anthropic API failed.\nReason: {_omni_standby_reason}\nRetrying every 30s.",
                key="omni-standby",
                severity="CRITICAL",
            )
        except Exception:
            pass
        threading.Thread(
            target=_omni_preflight_retry_loop, daemon=True, name="omni-preflight-retry"
        ).start()
    else:
        logger.info("Block 2: Anthropic → %s (%s)", _anthropic_result.status, _anthropic_result.message)
    # Non-critical brains: warn-only
    for _bkey, _bval in [
        ("openai", settings.openai_api_key),
        ("gemini", settings.gemini_api_key),
        ("deepseek", settings.deepseek_api_key),
    ]:
        try:
            if _bkey == "openai":
                _r = _validator.validate_openai(_bval)
            elif _bkey == "gemini":
                _r = _validator.validate_gemini(_bval)
            else:
                _r = _validator.validate_deepseek(_bval)
            if _r.status != "ok":
                logger.warning("API key probe: %s → %s (%s)", _bkey, _r.status, _r.message)
            else:
                logger.info("API key probe: %s → ok", _bkey)
        except Exception as _be:
            logger.warning("API key probe: %s → error (%s)", _bkey, _be)
    init_db(settings.omni_db_path)

    # ── Restart-safe state reconstruction ────────────────────────────────────
    # Pre-populate synthesized_concordances from today's DB records so a
    # Railway restart (or any restart mid-day) cannot re-synthesize concordances
    # that were already processed this session. Without this, NVDA/AVGO with
    # strong scanners can immediately re-qualify and re-fire after deploy.
    try:
        from zoneinfo import ZoneInfo as _ZI
        _et_date_today = datetime.now(_ZI("America/New_York")).strftime("%Y-%m-%d")
        with __import__("database").get_conn(settings.omni_db_path) as _startup_conn:
            _prior_rows = _startup_conn.execute(
                """
                SELECT ticker, window_id, direction, system, pathway
                FROM synthesis_results
                WHERE SUBSTR(window_id, 1, 10) = ?
                """,
                (_et_date_today,),
            ).fetchall()
        _recovered = 0
        with _state_lock:
            for _pr in _prior_rows:
                _rkey = (
                    _pr["ticker"],
                    _pr["window_id"],
                    _pr["direction"],
                    _pr["system"],
                    _pr["pathway"],
                )
                app_state["synthesized_concordances"].add(_rkey)
                _recovered += 1
        # Block 3: syntheses_today is derived from DB on every /health call — do NOT seed here.
        # Seed only last_synthesis_time so the silence watcher does not false-fire
        # after a clean restart that pre-loads already-synthesized concordances.
        if _recovered > 0:
            try:
                with __import__("database").get_conn(settings.omni_db_path) as _ts_conn:
                    _max_row = _ts_conn.execute(
                        """SELECT MAX(created_at) AS max_ts FROM synthesis_results
                        WHERE SUBSTR(window_id, 1, 10) = ?""",
                        (_et_date_today,),
                    ).fetchone()
                _max_ts_str = _max_row["max_ts"] if _max_row else None
                if _max_ts_str:
                    from datetime import timezone as _tz
                    # created_at may be offset-aware or naive ISO string
                    _max_ts_str_clean = _max_ts_str.replace("Z", "+00:00")
                    try:
                        _max_dt = datetime.fromisoformat(_max_ts_str_clean)
                    except ValueError:
                        _max_dt = None
                    if _max_dt is not None:
                        if _max_dt.tzinfo is None:
                            _max_dt = _max_dt.replace(tzinfo=_tz.utc)
                        with _state_lock:
                            # Only seed last_synthesis_time (for silence watcher).
                            # syntheses_today / go_verdicts_today are derived from DB in /health.
                            app_state["last_synthesis_time"] = _max_dt.timestamp()
                        logger.info(
                            "OMNI restart recovery: seeded last_synthesis_time from DB (last=%s)",
                            _max_ts_str,
                        )
            except Exception as _seed_err:
                logger.warning("OMNI restart recovery: could not seed last_synthesis_time: %s", _seed_err)
        logger.info(
            "OMNI restart recovery: pre-loaded %d synthesized_concordances from DB (date=%s)",
            _recovered, _et_date_today,
        )
    except Exception as _re:
        logger.warning(
            "OMNI restart recovery failed: %s — starting with empty synthesized_concordances",
            _re,
        )
    # ─────────────────────────────────────────────────────────────────────────

    # Cipher fix: start synthesis silence watcher as background daemon thread
    _watcher_thread = threading.Thread(
        target=_synthesis_silence_watcher, daemon=True, name="synthesis-silence-watcher"
    )
    _watcher_thread.start()
    logger.info("OMNI synthesis silence watcher started (threshold: %d min)", _SILENCE_THRESHOLD_MIN)

    logger.info("OMNI service ready — Quad Intelligence active")
    report("omni", "status", {"event": "started"})
    # Drain queued directives from before this startup
    _startup_instr = get_instructions("omni")
    if _startup_instr:
        logger.info("OMNI: %d instruction(s) from SOVEREIGN on startup", len(_startup_instr))
        for _i in _startup_instr:
            _dispatch_sovereign_instruction(_i)

    # Launch continuous comms loop (polls every 30s, heartbeat every 5min)
    _comms_stop_omni.clear()
    threading.Thread(target=_sovereign_comms_loop_omni, daemon=True, name="omni-sovereign-comms").start()
    threading.Thread(target=_trade_blocker_watchdog, daemon=True, name="omni-trade-blocker-watchdog").start()

    # ── Inevitable Failure Sentinel (Ahmed mandate May 1 2026) ───────────────────
    # Detects precursor error patterns before they become silent death.
    # Alerts Cipher + Vector + Ahmed immediately on detection.
    from failure_sentinel import start_sentinel as _start_sentinel
    _start_sentinel(
        omni_db    = settings.omni_db_path,
        alpha_db   = "/Users/ahmedsadek/nexus/data/alpha_execution.db",
        app_state  = app_state,
        state_lock = _state_lock,
    )
    logger.info("Failure sentinel started ✅")

    yield

    _comms_stop_omni.set()
    report("omni", "status", {"event": "stopped"})
    logger.info("OMNI service stopped")


app = FastAPI(
    title       = "OMNI Synthesis Engine",
    description = "Nexus Trading System — Quad Intelligence synthesis and routing",
    version     = "3.0.0",
    lifespan    = lifespan,
)


# ── Request Model ─────────────────────────────────────────────────────────────

class ConcordancePayload(BaseModel):
    """Concordance payload from Alpha or Prime buffer."""

    ticker:          str
    direction:       str
    system:          str          # 'alpha' or 'prime'
    pathway:         str          # P1, P2, P3, P4
    weighted_score:  float
    agents_involved: list[str]
    scores:          dict[str, float]
    verdict:         str
    sizing_mult:     float
    window_id:       str
    echo_chamber:    bool = False
    notes:           list[str] = []

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("direction")
    @classmethod
    def normalize_direction(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("bullish", "bearish"):
            raise ValueError("direction must be 'bullish' or 'bearish'")
        return v

    @field_validator("system")
    @classmethod
    def normalize_system(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("alpha", "prime"):
            raise ValueError("system must be 'alpha' or 'prime'")
        return v

    @field_validator("pathway")
    @classmethod
    def validate_pathway(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in ("P1", "P2", "P3", "P4"):
            raise ValueError("pathway must be P1, P2, P3, or P4")
        return v

    @field_validator("agents_involved")
    @classmethod
    def validate_agents(cls, v: list[str]) -> list[str]:
        """Reject unknown agent names to prevent rogue submissions biasing concordance.

        Cipher Finding 14 fix: normalize to lowercase before validation.
        Spec defines agents as lowercase; code had mixed-case set causing spec/code mismatch.
        """
        v = [a.lower() for a in v]
        VALID_AGENTS = {"cipher", "atlas", "sage"}
        unknown = [a for a in v if a not in VALID_AGENTS]
        if unknown:
            raise ValueError(f"Unknown agent(s): {unknown}. Valid: {sorted(VALID_AGENTS)}")
        return v

    @field_validator("sizing_mult")
    @classmethod
    def clamp_sizing_mult(cls, v: float) -> float:
        """Clamp sizing_mult to safe range — prevents zero/negative/extreme positions."""
        MIN, MAX = 0.1, 2.0
        if v < MIN or v > MAX:
            raise ValueError(f"sizing_mult {v} out of safe range [{MIN}, {MAX}]")
        return round(v, 4)

    @field_validator("weighted_score")
    @classmethod
    def validate_weighted_score(cls, v: float) -> float:
        """Reject nonsensical scores — must be 0–100."""
        if not (0.0 <= v <= 100.0):
            raise ValueError(f"weighted_score {v} must be between 0 and 100")
        return round(v, 4)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> JSONResponse:
    """Health check. Always returns 200. No auth required."""
    # Block 2: STANDBY fast path — GA takes no action for status: "standby"
    with _state_lock:
        _sb_active = _omni_standby_active
        _sb_reason = _omni_standby_reason
    if _sb_active:
        return JSONResponse({
            "status":  "standby",
            "service": "omni",
            "version": "3.1.0",
            "reason":  _sb_reason,
        })

    _last_ts = app_state["last_synthesis_time"]
    _silence_min = round((time.time() - _last_ts) / 60, 1) if _last_ts else None
    # GAP-008: pool stats
    try:
        _pool_stats = {
            "workers":    3,
            "max_workers": _SYNTHESIS_POOL._max_workers,
            "active":     len([f for f in _SYNTHESIS_POOL._threads if f.is_alive()]),
        }
    except Exception:
        _pool_stats = {"workers": 3}

    # ── Trading Effectiveness Score (TES) ──────────────────────────────────────
    # Measures whether OMNI is producing value, not just whether it's alive.
    # Health=UP + Syntheses>0 + GO>0 = effective. Health=UP + Syntheses>0 + GO=0 = investigating.
    # Block 3: Derive counters from DB (DB is truth, memory is cache)
    try:
        from zoneinfo import ZoneInfo as _ZI
        import datetime as _dt_mod
        _et_today = _dt_mod.datetime.now(_ZI("America/New_York")).strftime("%Y-%m-%d")
        with get_conn(settings.omni_db_path) as _hconn:
            _row = _hconn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN verdict IN ('GO','STRONG_GO') THEN 1 ELSE 0 END) AS go_count "
                "FROM synthesis_results WHERE SUBSTR(window_id,1,10) = ?",
                (_et_today,)
            ).fetchone()
            _syntheses = int(_row["total"] or 0) if _row else 0
            _gos = int(_row["go_count"] or 0) if _row else 0
            _recent = _hconn.execute(
                "SELECT verdict FROM synthesis_results ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        _consec_nogo = 0
        for _rv in _recent:
            if _rv["verdict"] in ("GO", "STRONG_GO"):
                break
            _consec_nogo += 1
        with _state_lock:
            app_state["syntheses_today"] = _syntheses
            app_state["go_verdicts_today"] = _gos
            app_state["consecutive_nogo"] = _consec_nogo
    except Exception as _dbe:
        logger.warning("Block 3: DB counter derivation failed, using memory cache: %s", _dbe)
        _syntheses  = app_state.get("syntheses_today", 0)
        _gos        = app_state.get("go_verdicts_today", 0)
        _consec_nogo= app_state.get("consecutive_nogo", 0)
    if not _is_market_hours():
        _tes_state = "OFF_HOURS"
    elif _syntheses == 0:
        _tes_state = "WARMING_UP"
    elif _gos > 0:
        _tes_state = "EFFECTIVE"
    elif _syntheses >= 3 and _gos == 0:
        _tes_state = "INVESTIGATING" if _consec_nogo >= 3 else "DRY_SPELL"
    else:
        _tes_state = "ACTIVE"

    return JSONResponse({
        "status":              "healthy",
        "service":             "omni",
        "version":             "3.1.0",
        "syntheses_today":     _syntheses,
        "go_verdicts_today":   _gos,
        "uptime_since":        app_state["start_time"],
        "last_synthesis_min_ago": _silence_min,
        "synthesis_pool":      _pool_stats,      # GAP-008: concurrent worker stats
        "effectiveness": {
            "tes_state":         _tes_state,
            "consecutive_nogo":  _consec_nogo,
            "nogo_alert_fired":  app_state.get("consecutive_nogo_alerted", False),
        },
        "code_hash":           _CODE_HASH,
        "stale_deploy":        (not _os.path.exists("/tmp/nexus_deploy_in_progress")) and _CODE_HASH != _compute_module_hash(),
    })


@app.post("/concordance")
def receive_concordance(
    body: ConcordancePayload,
    x_nexus_secret:       str = Header(default=""),
    x_nexus_prime_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Receive a concordance signal and run Quad Intelligence synthesis.

    Accepts from Alpha Buffer (X-Nexus-Secret) and Prime Buffer (X-Nexus-Prime-Secret).

    Pass B fix (Finding 2): Prime Buffer sends X-Nexus-Prime-Secret but OMNI only
    read X-Nexus-Secret — every Prime concordance was rejected with 403. Fixed by
    accepting either header and validating against the appropriate secret.

    Full pipeline:
      1. Verify auth
      2. Fetch Axiom risk assessment
      3. Fetch current regime
      4. Build complete context for all 4 brains
      5. Run all 4 brains in parallel
      6. Compute verdict (vote counting + Axiom hard stop check)
      7. Persist synthesis result
      8. Route to execution (if GO/STRONG_GO) — fully autonomous, no human gate
      9. Notify Ahmed via Telegram (GO/STRONG_GO only — NO_GO is silent)

    Returns immediately with synthesis result.
    """
    # Cipher Finding 4 fix: validate each header against its own secret.
    # Replaces the original `active_secret = x or y; verify_secret(active_secret)` pattern
    # which validated both against nexus_secret — broken on secret rotation.
    verify_concordance_auth(x_nexus_secret, x_nexus_prime_secret)

    # Block 2: STANDBY gate — reject synthesis while Anthropic preflight has not passed
    with _state_lock:
        if _omni_standby_active:
            return JSONResponse(
                status_code=503,
                content={"status": "standby", "reason": f"OMNI in STANDBY: {_omni_standby_reason}"},
            )

    # Poll SOVEREIGN for any mid-session instructions (deduped via watermark)
    _instr = get_instructions("omni")
    if _instr:
        logger.info("OMNI: %d instruction(s) from SOVEREIGN this cycle", len(_instr))
        for _i in _instr:
            _dispatch_sovereign_instruction(_i)

    # Halt gate
    if _sovereign_halted:
        ticker_hint = body.ticker if hasattr(body, 'ticker') else '?'
        logger.warning("OMNI: SOVEREIGN HALT active — rejecting concordance for %s", ticker_hint)
        report("omni", "status", {"event": "concordance_rejected", "reason": "sovereign_halt"})
        from fastapi.responses import JSONResponse as _JSONResponse
        return _JSONResponse(status_code=503, content={"status": "halted", "reason": "SOVEREIGN HALT active"})

    # CANARY gate: if today's canary failed, block synthesis and alert
    _canary_block = _check_canary_gate()
    if _canary_block:
        ticker_hint = getattr(body, 'ticker', '?')
        logger.error(
            "OMNI: CANARY GATE — synthesis blocked for %s: %s",
            ticker_hint, _canary_block,
        )
        report("omni", "status", {"event": "concordance_rejected", "reason": "canary_failed", "detail": _canary_block})
        from fastapi.responses import JSONResponse as _JSONResponse
        return _JSONResponse(
            status_code=503,
            content={"status": "canary_blocked", "reason": _canary_block},
        )

    concordance = body.model_dump()
    _pathway = concordance.get("pathway", "alpha")
    _ticker  = concordance["ticker"]
    _win_id  = concordance.get("window_id") or ""

    # GENESIS 2026-05-04: reject missing or 'unknown' window_id at the OMNI gate.
    # A concordance with no valid window context cannot be safely routed to execution.
    # Alpha-execution now hard-blocks 'unknown' — enforce the same contract here
    # so the error surfaces at the source (OMNI dispatch) not downstream (alpha-exec).
    if not _win_id or _win_id == "unknown":
        logger.error(
            "OMNI CONCORDANCE BLOCKED: missing/unknown window_id for ticker=%s pathway=%s — "
            "concordance requires a valid trading window ID to proceed (GENESIS fix 2026-05-04)",
            _ticker, _pathway,
        )
        return JSONResponse(
            status_code=422,
            content={
                "status": "rejected",
                "reason": "window_id is missing or 'unknown' — all concordances must carry a valid trading window ID",
                "ticker": _ticker,
            },
        )

    # INV-15: Max 1 P4 signal per ticker per 15-minute window
    if concordance.get("pathway") == "P4":
        _p4_key = (concordance.get("ticker", ""), _win_id)
        with _state_lock:
            if _p4_key in app_state["p4_dispatched_windows"]:
                logger.warning(
                    "INV-15: P4 duplicate blocked — ticker=%s window=%s",
                    concordance.get("ticker"), _win_id,
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "accepted": False,
                        "reason": f"P4 already dispatched for {concordance.get('ticker')} in window {_win_id}",
                    },
                )
            app_state["p4_dispatched_windows"].add(_p4_key)
            # Prune stale window keys (keep only current and last window)
            current_win = datetime.now(ET).strftime("%Y-%m-%d-%H%M")
            app_state["p4_dispatched_windows"] = {
                k for k in app_state["p4_dispatched_windows"]
                if k[1] >= current_win[:13]  # keep today's entries
            }
    _trace_id = f"{_win_id}:{_ticker}"

    # Concordance dedup: max 1 synthesis per (ticker, window, direction, system, pathway).
    # The Alpha Buffer fires concordance events each time a new agent joins (P3→P2→P1
    # upgrade). Without dedup, a 3-agent concordance triggers 3 separate OMNI syntheses.
    # The 3rd synthesis gets degraded LLM votes (echo chamber, tired context) and
    # frequently downgrades from CONDITIONAL to NO_GO. One synthesis per pathway level
    # is the correct design — pathway upgrades get new synthesis, same pathway does not.
    # [GENESIS 2026-04-20: fixes triple-synthesis degradation on HD/IEMG today]
    _conc_key = (
        _ticker,
        _win_id,
        concordance.get("direction", ""),
        concordance.get("system", "alpha"),
        _pathway,
    )
    with _state_lock:
        if _conc_key in app_state["synthesized_concordances"]:
            logger.info(
                "Concordance dedup — already synthesized %s/%s pathway=%s window=%s",
                _ticker, concordance.get("direction", ""), _pathway, _win_id,
            )  # noqa: E501
            return JSONResponse(
                status_code=200,
                content={
                    "accepted": False,
                    "reason": f"Concordance already synthesized for {_ticker}/{_pathway}/{_win_id}",
                },
            )
        app_state["synthesized_concordances"].add(_conc_key)
        # Prune: keep only current window's entries to prevent unbounded growth
        _cur_win_prefix = _win_id[:13]  # "YYYY-MM-DD-HH"
        app_state["synthesized_concordances"] = {
            k for k in app_state["synthesized_concordances"]
            if k[1][:13] >= _cur_win_prefix
        }

    logger.info(
        "Concordance received: %s/%s/%s | pathway=%s | score=%.1f",
        _ticker,
        concordance["direction"],
        concordance["system"],
        _pathway,
        concordance["weighted_score"],
    )

    # GAP-008: Submit synthesis to the worker pool instead of running inline.
    # The /concordance endpoint returns 202 Accepted immediately.
    # Synthesis runs in a background worker — up to 3 concordances simultaneously.
    # P1 concordances are given priority by running them on a dedicated submit path.
    # Dedup key is rolled back if synthesis raises an unhandled exception.

    def _pooled_synthesis():
        """Run synthesis in pool worker — rolls back dedup and retries once on failure.
        GAP-14: Semaphore is always released via try/finally to prevent pool slot leaks.
        """
        _SYNTHESIS_SEMAPHORE.acquire()
        try:
            try:
                _run_synthesis(_conc_key, _trace_id, _ticker, _pathway, _win_id, concordance)
            except Exception as _exc:
                logger.critical(
                    "OMNI pool synthesis FAILED for %s/%s — rolling back dedup for retry: %s",
                    _ticker, _pathway, _exc, exc_info=True,
                )
                with _state_lock:
                    app_state["synthesized_concordances"].discard(_conc_key)
                # Mandate v2: don't silently discard — retry once immediately
                try:
                    logger.info("OMNI pool: retrying synthesis for %s/%s", _ticker, _pathway)
                    with _state_lock:
                        app_state["synthesized_concordances"].add(_conc_key)
                    _run_synthesis(_conc_key, _trace_id, _ticker, _pathway, _win_id, concordance)
                    logger.info("OMNI pool retry SUCCESS for %s/%s", _ticker, _pathway)
                except Exception as _retry_exc:
                    logger.critical(
                        "OMNI pool retry ALSO FAILED for %s/%s: %s — enqueuing in DLQ",
                        _ticker, _pathway, _retry_exc,
                    )
                    with _state_lock:
                        app_state["synthesized_concordances"].discard(_conc_key)
                    # Notify SOVEREIGN
                    try:
                        import requests as _req
                        _req.post(
                            f"{os.environ.get('SOVEREIGN_BUS_URL','http://192.168.1.141:9999')}/send",
                            json={"from":"omni","to":"sovereign",
                                  "message":f"SYNTHESIS POOL DOUBLE-FAIL: {_ticker}/{_pathway} "
                                            f"failed twice. Error: {str(_retry_exc)[:200]}"},
                            timeout=5
                        )
                    except Exception: pass
        finally:
            _SYNTHESIS_SEMAPHORE.release()  # GAP-14: ALWAYS release, even if exception escapes

    _priority = _PATHWAY_PRIORITY.get(_pathway, 5)
    _SYNTHESIS_POOL.submit(_pooled_synthesis)
    logger.info(
        "GAP-008: Synthesis queued for %s/%s (pathway=%s priority=%d pool_workers=3)",
        _ticker, concordance.get("direction","?"), _pathway, _priority,
    )

    # Return 202 immediately — synthesis runs in background
    from fastapi.responses import JSONResponse as _JSONResponse
    return _JSONResponse(
        status_code=202,
        content={
            "status":    "queued",
            "ticker":    _ticker,
            "pathway":   _pathway,
            "window_id": _win_id,
            "message":   "Synthesis dispatched to worker pool — result will be posted to execution and Telegram",
        },
    )


def _run_synthesis(
    _conc_key: tuple,
    _trace_id: str,
    _ticker: str,
    _pathway: str,
    _win_id: str,
    concordance: dict,
) -> JSONResponse:
    """
    Execute the full synthesis pipeline for a concordance signal.

    Extracted from receive_concordance so the dedup rollback wrapper can catch
    any unhandled exception and discard the key — allowing retry on failure.

    Cipher fix 2026-04-22: synthesis errors no longer permanently block the
    concordance key. If this function raises, the caller rolls back _conc_key.
    """
    # Pipeline Sentinel — OMNI synthesis starting
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_trace_id, "omni_started", "omni", _ticker, _pathway)
    except Exception:
        pass

    # ── Step 1: Axiom risk assessment ─────────────────────────────────────────
    axiom_result = assess_ticker(
        settings.axiom_url,
        settings.axiom_secret,
        concordance["ticker"],
    )

    # ── Step 2: Current regime ────────────────────────────────────────────────
    regime = get_regime(settings.axiom_url, settings.axiom_secret)

    # ── Step 3: Fetch ORACLE intelligence for this ticker ─────────────────────
    # Brains require real market data to make informed GO/NO_GO decisions.
    # Without ORACLE context, all 4 brains correctly default to NO_GO.
    oracle_ctx = oracle_client.get_context(concordance["ticker"])
    if oracle_ctx is None:
        logger.warning("ORACLE context unavailable for %s — brains will operate with degraded context",
                       concordance["ticker"])

    # ── Step 3b: Fetch THESIS macro context for strategic intelligence ─────────
    # THESIS provides macro posture, sizing multiplier, and gate results.
    # If is_fallback=True, CHRONICLE is down — alert SOVEREIGN and continue
    # with conservative defaults (0.75 sizing). Never halt synthesis.
    thesis_ctx = None
    _thesis_url = os.environ.get("THESIS_URL", "http://localhost:8060")
    try:
        import requests as _req
        _resp = _req.get(f"{_thesis_url}/thesis/current-context", timeout=5)
        if _resp.status_code == 200:
            thesis_ctx = _resp.json()
            if thesis_ctx.get("is_fallback", True):
                logger.warning(
                    "OMNI: THESIS returned fallback context — "
                    "CHRONICLE may be down. Applying conservative defaults."
                )
                try:
                    _req.post(
                        "http://192.168.1.141:9999/send",
                        json={
                            "from": "omni",
                            "to": "sovereign",
                            "message": (
                                "OMNI BLIND-FLIGHT ALERT: THESIS returned fallback context. "
                                "CHRONICLE may be down. Trading conservatively at 0.75 sizing "
                                "until resolved."
                            ),
                        },
                        timeout=3,
                    )
                except Exception as _alert_exc:
                    logger.error("OMNI: failed to alert SOVEREIGN about blind-flight: %s", _alert_exc)
        else:
            logger.warning("OMNI: THESIS returned HTTP %d — using no thesis context", _resp.status_code)
    except Exception as _thesis_exc:
        logger.warning("OMNI: THESIS unreachable — %s. Proceeding without thesis context.", _thesis_exc)

    # ── Step 4: Build context ─────────────────────────────────────────────────
    context = build_context(concordance, axiom_result, regime, oracle_ctx)

    # ── Step 4: Quad Intelligence ─────────────────────────────────────────────
    brain_results = run_all_brains(
        context          = context,
        anthropic_api_key = settings.anthropic_api_key,
        openai_api_key   = settings.openai_api_key,
        gemini_api_key   = settings.gemini_api_key,
        deepseek_api_key = settings.deepseek_api_key,
    )

    # ── Step 5: Compute verdict ───────────────────────────────────────────────
    verdict = compute_verdict(
        brain_results      = brain_results,
        pathway            = concordance["pathway"],
        concordance_sizing = concordance["sizing_mult"],
        axiom_result       = axiom_result,
        thesis_ctx         = thesis_ctx,
    )

    # G8 SYS-1: Alert on brain degradation (< 4 brains responded)
    if verdict.brains_responded < 4:
        _maybe_alert_brain_degradation(
            ticker           = concordance["ticker"],
            brains_responded = verdict.brains_responded,
            brain_summary    = verdict.brain_summary,
            bot_token        = settings.telegram_bot_token,
            chat_id          = settings.ahmed_chat_id,
        )

    # ── Step 5b: Market Participant Psychology Overlay ────────────────────────
    # Applied after compute_verdict — adjusts sizing only, never changes verdict.
    # oracle_ctx is already available from Step 3 above.
    verdict, psychology_overlay = apply_psychology_overlay(verdict, oracle_ctx)

    # ── Step 6: Calculate position size ──────────────────────────────────────
    position_size = calculate_position_size(
        base_size   = BASE_POSITION_SIZE,
        sizing_mult = verdict.sizing_mult,
        pathway     = concordance["pathway"],
    )

    # ── Step 7: Persist synthesis ─────────────────────────────────────────────
    synthesis_id = save_synthesis_result(
        db_path              = settings.omni_db_path,
        window_id            = concordance["window_id"],
        ticker               = concordance["ticker"],
        direction            = concordance["direction"],
        system               = concordance["system"],
        pathway              = concordance["pathway"],
        agent_weighted_score = concordance["weighted_score"],
        brain_results        = brain_results,
        votes_go             = verdict.votes_go,
        brains_responded     = verdict.brains_responded,
        echo_chamber_flagged = verdict.echo_chamber_flagged,
        verdict              = verdict.verdict,
        verdict_notes        = " | ".join(verdict.notes),
        axiom_result         = axiom_result,
        psychology_overlay   = psychology_overlay.to_dict() if psychology_overlay else None,
    )

    with _state_lock:                          # Pass B fix (V6): thread-safe counter
        app_state["syntheses_today"] += 1
        app_state["last_synthesis_time"] = time.time()  # Cipher fix: track for silence detection

        # ── OMNI Self-Audit: consecutive NO_GO tracker (Upgrade v2 — Apr 30 2026) ──
        # Root cause of Apr 30 zero-trade day: context contamination caused every brain
        # to vote NO_GO citing system win rate. 21 consecutive NO_GOs went undetected.
        # This tracker fires an alert after 3 consecutive NO_GOs during market hours
        # so the problem is caught within one 30-min scan cycle, not end of day.
        if verdict.verdict in ("GO", "STRONG_GO"):
            app_state["consecutive_nogo"]         = 0
            app_state["consecutive_nogo_alerted"] = False
            app_state["nogo_block_reasons"]       = []
        else:
            app_state["consecutive_nogo"] += 1
            # Collect blocking reason snippet for pattern detection
            reason_snippet = " | ".join(verdict.notes[:2]) if verdict.notes else ""
            app_state["nogo_block_reasons"] = (
                app_state["nogo_block_reasons"][-4:] + [reason_snippet]
            )
            # Fire self-audit alert after 3 consecutive NO_GOs during market hours
            _CONSECUTIVE_NOGO_THRESHOLD = 3
            if (
                app_state["consecutive_nogo"] >= _CONSECUTIVE_NOGO_THRESHOLD
                and not app_state["consecutive_nogo_alerted"]
                and _is_market_hours()
            ):
                app_state["consecutive_nogo_alerted"] = True
                _fire_consecutive_nogo_alert(
                    count           = app_state["consecutive_nogo"],
                    reasons         = app_state["nogo_block_reasons"],
                    bot_token       = settings.telegram_bot_token,
                    chat_id         = settings.ahmed_chat_id,
                    ticker          = concordance.get("ticker", "?"),
                )

    # SOVEREIGN S8: Write synthesis completion to CHRONICLE for durable counter
    # (survives OMNI restarts — in-memory counter resets; CHRONICLE does not)
    try:
        import sqlite3 as _sq3
        _chron = _sq3.connect("/Users/ahmedsadek/nexus/data/chronicle.db", timeout=3)
        _chron.execute(
            "CREATE TABLE IF NOT EXISTS synthesis_completed "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, synthesis_id TEXT, "
            "ticker TEXT, system TEXT, verdict TEXT, trade_date TEXT, ts REAL)"
        )
        _chron.execute(
            "INSERT INTO synthesis_completed (synthesis_id, ticker, system, verdict, trade_date, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(synthesis_id),
                concordance.get("ticker", ""),
                concordance.get("system", ""),
                verdict.verdict if hasattr(verdict, "verdict") else str(verdict),
                __import__("datetime").datetime.now(
                    __import__("pytz").timezone("America/New_York")
                ).strftime("%Y-%m-%d"),
                time.time(),
            ),
        )
        _chron.commit()
        _chron.close()
    except Exception as _s8_err:
        logger.debug("S8 chronicle write skipped: %s", _s8_err)  # Never block synthesis

    # ── Step 8: Route to execution ────────────────────────────────────────────
    execution_ok = None
    if verdict.can_execute():
        # Pass B fix #1: use system-appropriate secret for execution routing.
        # Alpha Execution expects X-Nexus-Secret = nexus_secret.
        # Prime Execution expects X-Nexus-Prime-Secret = nexus_prime_secret.
        exec_auth = (
            settings.nexus_secret
            if concordance["system"] == "alpha"
            else settings.nexus_prime_secret
        )
        exec_ok, exec_resp = route_to_execution(
            system             = concordance["system"],
            alpha_exec_url     = settings.alpha_execution_url,
            prime_exec_url     = settings.prime_execution_url,
            auth_secret        = exec_auth,
            concordance        = concordance,
            synthesis_verdict  = verdict.to_dict(),
            position_size      = position_size,
        )
        execution_ok = exec_ok
        mark_execution_dispatched(
            settings.omni_db_path,
            synthesis_id,
            (settings.alpha_execution_url if concordance["system"] == "alpha"
             else settings.prime_execution_url),
            exec_resp,
        )
        if exec_ok:
            with _state_lock:                  # Pass B fix (V6): thread-safe counter
                app_state["go_verdicts_today"] += 1
            _svc_state.write("go_verdicts_today", app_state["go_verdicts_today"])  # GAP-002
        else:
            # GAP-003: Execution failed → enqueue in DLQ for persistent retry
            # Also launch auto-recovery in background thread
            try:
                from execution_recovery import auto_recover_async as _ar
                _ar(
                    ticker    = concordance["ticker"],
                    system    = concordance["system"],
                    exec_resp = exec_resp,
                    route_fn  = _route_now,
                )
            except Exception as _ar_err:
                logger.warning("Auto-recover launch failed: %s", _ar_err)

            try:
                from execution_dlq import enqueue as _dlq_enqueue
                _dlq_enqueue(
                    ticker         = concordance["ticker"],
                    direction      = concordance.get("direction", "bullish"),
                    system         = concordance["system"],
                    pathway        = concordance.get("pathway", "P1"),
                    window_id      = concordance.get("window_id", "unknown"),
                    payload        = {
                        "ticker":            concordance["ticker"],
                        "direction":         concordance.get("direction", "bullish"),
                        "system":            concordance["system"],
                        "pathway":           concordance.get("pathway", "P1"),
                        "agent_scores":      concordance.get("scores", {}),
                        "weighted_score":    concordance.get("weighted_score", 0),
                        "verdict":           verdict.verdict,
                        "votes_go":          verdict.votes_go,
                        "sizing_mult":       verdict.sizing_mult,
                        "position_size_usd": position_size,
                        "window_id":         concordance.get("window_id", "unknown") + "-dlq",
                        "auto_execute":      True,
                    },
                    failure_reason = exec_resp,
                )
            except Exception as _dlq_err:
                logger.warning("DLQ enqueue failed: %s", _dlq_err)

    # ── Step 9: Telegram notifications ───────────────────────────────────────
    # Axiom hard-stop blocked → alert Ahmed (Cipher Finding 6 fix)
    # GO/STRONG_GO → full synthesis card
    # CONDITIONAL  → brief system health alert
    # NO_GO        → silent drop (unless axiom_blocked — see above)
    if verdict.axiom_blocked:
        send_axiom_block_alert(
            bot_token  = settings.telegram_bot_token,
            chat_id    = settings.ahmed_chat_id,
            ticker     = concordance["ticker"],
            hard_stops = getattr(verdict, "axiom_hard_stops", []),
            regime     = context.get("regime", {}).get("classification", "UNKNOWN"),
        )

    if verdict.verdict in ("GO", "STRONG_GO"):
        send_synthesis_card(
            bot_token          = settings.telegram_bot_token,
            chat_id            = settings.ahmed_chat_id,
            concordance        = concordance,
            verdict            = verdict,
            brain_results      = brain_results,
            position_size      = position_size,
            execution_ok       = execution_ok,
            psychology_overlay = psychology_overlay,
        )
    elif verdict.verdict == "CONDITIONAL":
        # CONDITIONAL = system degradation or borderline result. Alert Ahmed briefly.
        send_conditional_alert(
            bot_token   = settings.telegram_bot_token,
            chat_id     = settings.ahmed_chat_id,
            ticker      = concordance["ticker"],
            pathway     = concordance["pathway"],
            votes_go    = verdict.votes_go,
            brains_resp = verdict.brains_responded,
            notes       = verdict.notes,
        )

    # Pipeline Sentinel — OMNI synthesis completed
    try:
        from pipeline_client import trace_hop as _trace_hop
        _trace_hop(_trace_id, "omni_completed", "omni", _ticker, _pathway)
    except Exception:
        pass

    logger.info(
        "Synthesis complete: %s/%s/%s | verdict=%s | votes=%d/4 | exec=%s",
        concordance["ticker"],
        concordance["direction"],
        concordance["system"],
        verdict.verdict,
        verdict.votes_go,
        execution_ok,
    )
    _msg_type = "alert" if verdict.verdict in ("GO", "STRONG_GO") else "status"
    _esc = EscalationLevel.CRITICAL if verdict.verdict in ("GO", "STRONG_GO") else EscalationLevel.INFO
    report("omni", _msg_type, {
        "event":     "synthesis_complete",
        "ticker":    concordance["ticker"],
        "direction": concordance["direction"],
        "pathway":   concordance["pathway"],
        "verdict":   verdict.verdict,
        "votes_go":  verdict.votes_go,
        "brains":    verdict.brains_responded,
        "exec_ok":   execution_ok,
    }, escalation=_esc)

    return JSONResponse({
        "synthesis_id":   synthesis_id,
        "ticker":         concordance["ticker"],
        "direction":      concordance["direction"],
        "system":         concordance["system"],
        "pathway":        concordance["pathway"],
        "verdict":        verdict.verdict,
        "votes_go":       verdict.votes_go,
        "brains_responded": verdict.brains_responded,
        "sizing_mult":    verdict.sizing_mult,
        "position_size":  position_size,
        "execution_ok":   execution_ok,
        "echo_chamber":   verdict.echo_chamber_flagged,
        "axiom_blocked":  verdict.axiom_blocked,
        "notes":          verdict.notes,
    })


@app.get("/status")
def status(x_nexus_secret: str = Header(default="")) -> JSONResponse:
    """Return recent syntheses and service state."""
    verify_secret(x_nexus_secret)
    recent = get_recent_syntheses(settings.omni_db_path, limit=10)
    return JSONResponse({
        "service":            "omni",
        "syntheses_today":    app_state["syntheses_today"],
        "go_verdicts_today":  app_state["go_verdicts_today"],
        "recent_syntheses":   recent,
        "checked_at":         datetime.now(ET).isoformat(),
    })


@app.get("/synthesis/{synthesis_id}")
def get_synthesis(
    synthesis_id:   int,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Return full synthesis detail by row ID.

    Args:
        synthesis_id: Integer ID of the synthesis result.
    """
    verify_secret(x_nexus_secret)
    with __import__("database").get_conn(settings.omni_db_path) as conn:
        row = conn.execute(
            "SELECT * FROM synthesis_results WHERE id=?",
            (synthesis_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Synthesis {synthesis_id} not found")

    return JSONResponse(dict(row))



# ── SOVEREIGN Push Endpoints ──────────────────────────────────────────────────

class _SovDirective(BaseModel):
    directive: str
    data: dict = {}
    from_agent: str = "sovereign"


@app.post("/sovereign/directive", status_code=200)
def sovereign_directive(
    body: _SovDirective,
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """SOVEREIGN pushes a directive directly. Zero polling lag."""
    verify_secret(x_nexus_secret)
    global _sovereign_halted, _auto_execute

    d = body.directive.strip().upper()
    logger.info("SOVEREIGN direct push: %s", d)

    if d == "PING":
        report("omni", "ack", {"directive": "PING", "status": "alive"})
        return JSONResponse({"ok": True, "directive": "PING", "status": "alive"})
    elif d == "HALT":
        _sovereign_halted = True
        report("omni", "ack", {"directive": "HALT", "status": "applied", "halted": True})
        return JSONResponse({"ok": True, "directive": "HALT", "halted": True})
    elif d == "RESUME":
        _sovereign_halted = False
        report("omni", "ack", {"directive": "RESUME", "status": "applied", "halted": False})
        return JSONResponse({"ok": True, "directive": "RESUME", "halted": False})
    elif d in ("FLUSH", "RESET_DAY"):
        with _state_lock:
            app_state["syntheses_today"] = 0
            app_state["go_verdicts_today"] = 0
            app_state["p4_dispatched_windows"] = set()
            app_state["synthesized_concordances"] = set()
            # FIX: reset consecutive_nogo + alert flag on daily boundary
            app_state["consecutive_nogo"] = 0
            app_state["consecutive_nogo_alerted"] = False
            app_state["nogo_block_reasons"] = []
        report("omni", "ack", {"directive": d, "status": "applied"})
        return JSONResponse({"ok": True, "directive": d})
    elif d == "SET_AUTO_EXECUTE":
        val = str(body.data.get("value", "")).lower()
        if val in ("true", "false"):
            _auto_execute = val == "true"
            report("omni", "ack", {"directive": "SET_AUTO_EXECUTE", "value": _auto_execute})
            return JSONResponse({"ok": True, "auto_execute": _auto_execute})
        return JSONResponse({"ok": False, "error": "value must be \'true\' or \'false\'"}, status_code=400)
    elif d == "STATUS":
        with _state_lock:
            snap = {
                "directive": "STATUS", "service": "omni",
                "halted": _sovereign_halted, "auto_execute": _auto_execute,
                "syntheses_today": app_state.get("syntheses_today", 0),
                "go_verdicts_today": app_state.get("go_verdicts_today", 0),
                "last_synthesis_time": app_state.get("last_synthesis_time"),
            }
        report("omni", "status", snap)
        return JSONResponse({"ok": True, **snap})
    else:
        report("omni", "ack", {"directive": d, "status": "unrecognized"})
        return JSONResponse({"ok": False, "error": f"unrecognized directive: {d}"}, status_code=400)


@app.get("/sovereign/status", status_code=200)
def sovereign_status(
    x_nexus_secret: str = Header(default=""),
) -> JSONResponse:
    """SOVEREIGN queries full OMNI state on-demand."""
    verify_secret(x_nexus_secret)
    with _state_lock:
        return JSONResponse({
            "ok": True, "service": "omni",
            "halted": _sovereign_halted,
            "auto_execute": _auto_execute,
            "syntheses_today": app_state.get("syntheses_today", 0),
            "go_verdicts_today": app_state.get("go_verdicts_today", 0),
            "last_synthesis_time": app_state.get("last_synthesis_time"),
            "port": int(__import__("os").getenv("PORT", "8004")),
        })


# ── Internal helpers ──────────────────────────────────────────────────────────

def send_conditional_alert(
    bot_token:   str,
    chat_id:     str,
    ticker:      str,
    pathway:     str,
    votes_go:    int,
    brains_resp: int,
    notes:       list,
) -> None:
    """
    Send a brief CONDITIONAL system alert to Ahmed.
    CONDITIONAL = no trade executed, but system health is impaired or borderline.

    Args:
        bot_token:   Telegram bot token.
        chat_id:     Ahmed's chat ID.
        ticker:      Ticker that triggered the conditional.
        pathway:     Concordance pathway.
        votes_go:    Number of GO votes from brains.
        brains_resp: Number of brains that responded.
        notes:       Synthesis notes explaining the conditional.
    """
    reason = notes[0] if notes else "borderline or degraded"
    try:
        from shared.notification_router import notify_warn as _nw
        _nw(
            "omni",
            f"OMNI CONDITIONAL — {ticker}",
            f"Pathway: {pathway} | Brains: {brains_resp}/4 responded | GO votes: {votes_go}/4\nReason: {reason}\nNo trade executed.",
            ticker=ticker,
        )
    except Exception:
        pass
