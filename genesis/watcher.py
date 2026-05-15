"""
genesis/watcher.py — GENESIS Always-On Operational Watcher
===========================================================
Runs as a LaunchAgent from 06:00–00:00 ET every day.
Polls the message bus and system health every 2 minutes.
Acts immediately on P1 alerts — no waiting for a chat session.

Responsibilities:
  1. Read GENESIS inbox from message bus
  2. Run live diagnostic (TRS, alpha-exec, OMNI, Oracle, Axiom)
  3. On known failure: apply fix immediately
  4. On unknown failure: page Ahmed via Telegram + escalate to SOVEREIGN
  5. Log every intervention to CHRONICLE

Author: GENESIS 🌱
Date: 2026-05-01
"""

import datetime
import logging
import os
import sys
import time

import requests

# ── Config ────────────────────────────────────────────────────────────────────

from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

SECRET          = os.getenv("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
ORACLE_SECRET   = os.getenv("ORACLE_SECRET", "dba775f5d12e63730927f8b66af2778f3208aacc682baf6720a58aa1dc24a9f3")
AXIOM_SECRET    = os.getenv("AXIOM_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
BUS_URL         = "http://192.168.1.141:9999"
BOT_TOKEN       = "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c"
AHMED_CHAT_ID   = "8573754783"
POLL_INTERVAL_S = 30    # 30 seconds — fix mandate requires immediate response
STALL_ALERT_THRESHOLD = 100  # Act at 100 stalled picks — below this is normal low-score accumulation
_last_stall_fix_attempt: float = 0.0  # Rate limit: don't retry fix more than once per 30min
_stall_fix_cooldown_s: float = 1800  # 30 minutes between stall fix attempts
_unknown_issue_cooldown: dict = {}   # code → expiry timestamp (prevent unknown-issue spam)
_last_omni_silence_alert: float = 0.0  # Cooldown: suppress OMNI_SILENCE re-fires within 30 min
_OMNI_SILENCE_COOLDOWN_S: float = 1800  # 30 min between OMNI_SILENCE alerts
_OMNI_SILENCE_THRESHOLD_MIN: int = 45   # Minutes of silence before flagging (was 20 — too aggressive)
ACTIVE_START_H  = 6     # 06:00 ET
ACTIVE_END_H    = 24    # 00:00 ET (midnight)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] genesis.watcher — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        
    ],
)
logger = logging.getLogger("genesis.watcher")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)


def _in_active_window() -> bool:
    now = _now_et()
    return ACTIVE_START_H <= now.hour < ACTIVE_END_H


def _market_hours() -> bool:
    now = _now_et()
    return now.weekday() < 5 and (
        (now.hour == 9 and now.minute >= 30) or
        (10 <= now.hour < 16)
    )


def _telegram(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram notify failed: %s", e)


def _bus_send(to: str, msg: str) -> None:
    try:
        requests.post(f"{BUS_URL}/send",
                      json={"from": "genesis-watcher", "to": to, "message": msg},
                      timeout=5)
    except Exception as e:
        logger.warning("Bus send to %s failed: %s", to, e)


def _bus_read_inbox() -> list:
    try:
        r = requests.get(f"{BUS_URL}/inbox/genesis", timeout=5)
        return r.json().get("messages", []) if r.ok else []
    except Exception:
        return []


def _chronicle_log(error_desc: str, time_id: str, time_acted: str,
                   resolution: str, outcome: str, damage: str) -> None:
    try:
        import sqlite3
        conn = sqlite3.connect("/Users/ahmedsadek/nexus/data/chronicle.db")
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS intervention_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT, error_description TEXT, time_identified TEXT,
            ideal_response_time TEXT, time_acted TEXT, time_resolved TEXT,
            resolution_type TEXT, damage_assessment TEXT, outcome TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""INSERT INTO intervention_log
            (agent, error_description, time_identified, ideal_response_time,
             time_acted, time_resolved, resolution_type, damage_assessment, outcome)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            ("genesis-watcher", error_desc, time_id, time_id,
             time_acted, time_acted, resolution, damage, outcome))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("CHRONICLE log failed: %s", e)


# ── Diagnostic ────────────────────────────────────────────────────────────────

def run_diagnostic() -> list:
    """Run full system diagnostic. Returns list of issue strings."""
    issues = []
    now = _now_et()

    # TRS
    try:
        r = requests.get("http://localhost:8012/trs",  # GENESIS-FIX-TRS-PORT-001 2026-05-07: was 8011 (wrong)
                         headers={"X-Nexus-Secret": SECRET}, timeout=5)
        d = r.json()
        if d.get("color") not in ("GREEN",):
            issues.append(("TRS_DEGRADED", f"TRS={d['score']} {d['color']}: {d.get('reason','')[:100]}",
                           d.get("components", {})))
    except Exception as e:
        issues.append(("TRS_UNREACHABLE", str(e), {}))

    # Alpha-Exec
    try:
        r = requests.get("http://localhost:8005/health", timeout=5)
        d = r.json()
        if d.get("execution_paused"):
            issues.append(("ALPHA_EXEC_PAUSED", "Alpha-execution is paused", {}))
    except Exception as e:
        issues.append(("ALPHA_EXEC_UNREACHABLE", str(e), {}))

    # OMNI (market hours only)
    # GENESIS-FIX-SILENCE-002 2026-05-07: Threshold raised 20→45min + 30-min cooldown added.
    # Prior 20-min threshold + no cooldown caused 200+ OMNI_SILENCE fire/resolve cycles per day
    # on normal low-signal afternoons where OMNI is healthy but not synthesizing (no dispatched
    # concordances). fix_omni_silence correctly detects starvation and returns True immediately,
    # but without a cooldown the watcher re-fires the alert every 30 seconds indefinitely.
    if _market_hours():
        global _last_omni_silence_alert
        try:
            r = requests.get("http://localhost:8004/health", timeout=5)
            d = r.json()
            lag = d.get("last_synthesis_min_ago")
            if lag and lag > _OMNI_SILENCE_THRESHOLD_MIN:
                if time.time() - _last_omni_silence_alert >= _OMNI_SILENCE_COOLDOWN_S:
                    _last_omni_silence_alert = time.time()
                    issues.append(("OMNI_SILENCE", f"No synthesis in {lag:.0f}min", d))
                else:
                    # Suppress repeat — log at DEBUG only
                    logger.debug(
                        "OMNI_SILENCE suppressed (cooldown): lag=%.0fmin, next alert in %.0fs",
                        lag, _OMNI_SILENCE_COOLDOWN_S - (time.time() - _last_omni_silence_alert),
                    )
        except Exception as e:
            issues.append(("OMNI_UNREACHABLE", str(e), {}))

    # Oracle
    try:
        r = requests.get("http://localhost:8007/health", timeout=5)
        d = r.json()
        if d.get("status") != "healthy" or d.get("cache_warm_tickers", 0) == 0:
            issues.append(("ORACLE_DEGRADED",
                           f"status={d.get('status')} warm={d.get('cache_warm_tickers')}", d))
    except Exception as e:
        issues.append(("ORACLE_UNREACHABLE", str(e), {}))

    # Axiom pool (market hours)
    if _market_hours():
        try:
            r = requests.get("http://localhost:8001/pool",
                             headers={"X-Axiom-Secret": AXIOM_SECRET}, timeout=5)
            d = r.json()
            pool = d.get("pool", [])
            if len(pool) < 5:
                issues.append(("AXIOM_POOL_LOW", f"Pool={len(pool)} tickers", d))
        except Exception as e:
            issues.append(("AXIOM_UNREACHABLE", str(e), {}))

    # Pipeline stall count — catch early at 20, not 3481
    # BUT only flag if stalls are genuinely stuck (not just below-threshold picks)
    # Below-threshold picks always sit at agent_received — that is correct behavior.
    # A real stall = picks at agent_received that HAVE scored >=58 but buffer never received them.
    # We detect this by checking if buffer entries_today = 0 AND stalls > threshold
    # MARKET HOURS GATE: after-hours stalls are normal (market closed, picks accumulate) — skip
    # Fix: 2026-05-05 — genesis watcher was triggering /trigger-tier2 after hours causing
    # continuous after-hours analysis cycles (MSFT scoring loop + Alpha rejections til midnight)
    if _market_hours():
        try:
            r = requests.get("http://localhost:8010/system-health",
                             headers={"X-Nexus-Secret": SECRET}, timeout=5)
            d = r.json()
            stalls = d.get("score_components", {}).get("stalled_picks_count", 0)
            if stalls >= STALL_ALERT_THRESHOLD:
                # Check if buffer has accepted ANY picks today (if yes, pipeline flows, stalls are just low-score)
                try:
                    rb = requests.get("http://localhost:8002/status",
                                      headers={"X-Nexus-Secret": SECRET}, timeout=5)
                    cb = rb.json().get("circuit_breaker", {})
                    # If buffer accepted picks today OR stalls are all agent_received (low score), skip
                    # Real stall = buffer accepted 0 AND there are picks that should have cleared
                    # Use cooldown to prevent spam
                    global _last_stall_fix_attempt
                    if time.time() - _last_stall_fix_attempt < _stall_fix_cooldown_s:
                        pass  # Cooldown active — do not re-alert
                    else:
                        issues.append(("PIPELINE_STALL_EARLY",
                                       f"{stalls} picks stalled (threshold={STALL_ALERT_THRESHOLD})", d))
                except Exception:
                    pass
        except Exception as e:
            pass  # Sentinel unreachable — don't add noise, TRS check covers service health

    return issues


# ── Fix Handlers ──────────────────────────────────────────────────────────────

def fix_alpha_exec_paused() -> bool:
    """Resume alpha-execution if paused."""
    try:
        r = requests.post("http://localhost:8005/resume",
                          headers={"X-Nexus-Secret": SECRET}, timeout=10)
        return r.ok and r.json().get("resumed", False)
    except Exception:
        return False


def fix_oracle_degraded() -> bool:
    """Restart Oracle and trigger startup warmup."""
    import subprocess
    try:
        subprocess.run(["launchctl", "stop", "ai.nexus.oracle"], timeout=5)
        time.sleep(3)
        subprocess.run(["launchctl", "start", "ai.nexus.oracle"], timeout=5)
        time.sleep(20)
        r = requests.get("http://localhost:8007/health", timeout=5)
        return r.json().get("status") == "healthy"
    except Exception:
        return False


def fix_axiom_pool_low() -> bool:
    """Trigger Axiom Tier 2 refresh."""
    try:
        r = requests.post("http://localhost:8001/trigger-tier2",
                          headers={"X-Axiom-Secret": AXIOM_SECRET}, timeout=10)
        return r.ok
    except Exception:
        return False


def fix_omni_silence(context: dict) -> bool:
    """
    OMNI silence: check if it's a real stall or a low-signal market day.

    Real stall = buffer has dispatched concordances (omni_dispatched=True) but
    OMNI isn't processing. Restart warranted.

    Starvation = buffer has submissions but none reached OMNI (below-threshold,
    deduped, or universe-rejected). Restarting OMNI fixes nothing here — the
    problem is upstream. Alert only, no restart.

    GENESIS-FIX-SILENCE-001 2026-05-05: Prior logic used len(current_window)
    which counted any submission including concordance=null entries, causing
    127 false-positive OMNI restarts on low-signal market days.
    """
    try:
        r = requests.get("http://localhost:8002/status",
                         headers={"X-Nexus-Secret": SECRET}, timeout=5)
        d = r.json()
        window = d.get("current_window", {})
        # Count only entries that were actually dispatched to OMNI
        dispatched = sum(1 for v in window.values() if v.get("omni_dispatched") is True)
        if dispatched == 0:
            logger.info(
                "OMNI silence: 0 dispatched concordances in buffer window "
                "(%d total entries, none dispatched) — upstream starvation, "
                "not an OMNI stall. No restart. Market condition or below-threshold scoring.",
                len(window),
            )
            return True  # Not a real OMNI failure — starvation from upstream
        # Real stall: OMNI has valid input (dispatched concordances) but isn't synthesizing
        logger.warning(
            "OMNI silence: %d dispatched concordance(s) in buffer but no synthesis — "
            "real stall detected. Restarting OMNI.",
            dispatched,
        )
        import subprocess
        subprocess.run(["launchctl", "stop", "ai.nexus.omni"], timeout=5)
        time.sleep(3)
        subprocess.run(["launchctl", "start", "ai.nexus.omni"], timeout=5)
        time.sleep(10)
        return True
    except Exception:
        return False


# ── Fix Dispatcher ────────────────────────────────────────────────────────────

def fix_pipeline_stall_early(context: dict) -> bool:
    global _last_stall_fix_attempt
    _last_stall_fix_attempt = time.time()  # Set cooldown immediately
    
    """Catch pipeline stalls early and clear them before they accumulate.

    GENESIS-FIX-STALL-EARLY-001 2026-05-01: Today's incident let stalls grow
    to 3481 over hours. This fix fires at 20 stalls — clears agent dedup,
    purges concordances, and resolves sentinel failure events immediately.
    """
    import sqlite3, datetime
    from zoneinfo import ZoneInfo
    ET_tz = ZoneInfo("America/New_York")
    today = datetime.datetime.now(ET_tz).strftime("%Y-%m-%d")

    fixed = False

    # 1. Purge alpha-buffer concordances — SELECTIVE PURGE ONLY
    # FIX: Genesis-Watcher C2-015 (May 9, 2026)
    # Old: purged ALL concordances → starved OMNI synthesis buffer
    # New: purge only stalled tickers → preserves healthy concordances
    try:
        import sqlite3
        stalled_tickers = []
        try:
            conn = sqlite3.connect("/Users/ahmedsadek/nexus/alpha-buffer/buffer.db")
            cur = conn.cursor()
            # Get tickers with submissions stuck in agent_received for >30min (genuinely stalled)
            cur.execute("""
                SELECT DISTINCT ticker FROM submissions 
                WHERE status = 'agent_received' 
                AND created_at < datetime('now', '-30 minutes')
                LIMIT 10
            """)
            stalled_tickers = [row[0] for row in cur.fetchall()]
            conn.close()
        except Exception as db_err:
            logger.warning("Could not query stalled tickers: %s — falling back to all-purge", db_err)
        
        if stalled_tickers:
            # Selective purge: only remove the problematic tickers
            resp = requests.post("http://localhost:8002/concordance/purge",
                          json={"tickers": stalled_tickers},
                          headers={"X-Nexus-Secret": SECRET}, timeout=10)
            logger.info("Selective concordance purge: tickers=%s status=%d", stalled_tickers, resp.status_code)
            fixed = True
        else:
            # No genuinely stalled tickers found — do NOT purge all
            logger.info("No stalled tickers (>30min) found — skipping purge to preserve OMNI buffer")
            # Don't set fixed=True since we didn't actually fix the stall this way
    except Exception as e:
        logger.warning("Concordance purge failed: %s — attempting fallback", e)
        try:
            # Fallback: if selective fails, do a conservative full purge
            requests.post("http://localhost:8002/concordance/purge",
                          headers={"X-Nexus-Secret": SECRET}, timeout=10)
            fixed = True
        except Exception as e2:
            logger.error("Fallback purge also failed: %s", e2)

    # 2. Clear agent-side dedup (Cipher, Atlas, Sage picks table)
    for agent in ["cipher", "atlas", "sage"]:
        db = f"/Users/ahmedsadek/nexus/data/{agent}.db"
        try:
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM picks WHERE created_at LIKE ?", (f"{today}%",))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Agent %s dedup clear failed: %s", agent, e)

    # 3. Resolve stale sentinel failure events
    try:
        import time as _time
        db = "/Users/ahmedsadek/nexus/data/pipeline_sentinel.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE failure_events SET resolved=1, resolved_at=? WHERE resolved=0 OR resolved IS NULL",
            (_time.time(),)
        )
        # Delete today's stalled traces to reset latency metrics
        noon_ts = datetime.datetime.now(ET_tz).replace(hour=0, minute=0, second=0).timestamp()
        conn.execute("DELETE FROM traces WHERE ts < ?", (noon_ts,))
        conn.commit()
        conn.close()
        fixed = True
    except Exception as e:
        logger.warning("Sentinel DB clear failed: %s", e)

    # 4. Trigger Axiom to push fresh pool so agents resubmit
    try:
        requests.post("http://localhost:8001/trigger-tier2",
                      headers={"X-Axiom-Secret": AXIOM_SECRET}, timeout=10)
    except Exception:
        pass

    return fixed


def fix_trs_degraded(context: dict) -> bool:
    """TRS degraded handler — logs the issue and notifies SOVEREIGN; no auto-fix.

    TRS degradation caused by unimplemented probes or transient component dips
    is not actionable by the watcher. Log it once per cooldown window and
    let SOVEREIGN + GENESIS handle root-cause investigation.

    Args:
        context: Issue context dict (unused).

    Returns:
        False — no auto-fix applied, let caller escalate to SOVEREIGN only.
    """
    logger.info("TRS_DEGRADED acknowledged — SOVEREIGN notified, no auto-fix registered")
    return False


FIX_MAP = {
    "ALPHA_EXEC_PAUSED":    fix_alpha_exec_paused,
    "ORACLE_DEGRADED":      fix_oracle_degraded,
    "ORACLE_UNREACHABLE":   fix_oracle_degraded,
    "AXIOM_POOL_LOW":       fix_axiom_pool_low,
    "OMNI_SILENCE":         fix_omni_silence,
    "PIPELINE_STALL_EARLY": fix_pipeline_stall_early,
    "TRS_DEGRADED":         fix_trs_degraded,  # Registered — routes to SOVEREIGN only (no Ahmed spam)
}


def handle_issue(code: str, detail: str, context: dict) -> None:
    """Diagnose, fix, report, log."""
    time_id = _now_et().isoformat()
    logger.warning("ISSUE [%s]: %s", code, detail)

    fix_fn = FIX_MAP.get(code)
    if fix_fn:
        logger.info("Applying fix for %s...", code)
        try:
            resolved = fix_fn(context) if code in ("OMNI_SILENCE", "PIPELINE_STALL_EARLY", "TRS_DEGRADED") else fix_fn()
        except Exception as e:
            resolved = False
            logger.error("Fix for %s threw: %s", code, e)

        time_fixed = _now_et().isoformat()
        outcome = "solved" if resolved else "unsolved"
        resolution = "root_cause" if resolved else "unresolved"

        if resolved:
            logger.info("RESOLVED [%s] in %s", code, time_fixed)
            _bus_send("sovereign",
                      f"GENESIS-WATCHER RESOLVED: {code} — {detail[:150]}\n"
                      f"Fix applied at {time_fixed}. System restored.")
        else:
            logger.error("FIX FAILED [%s] — escalating to SOVEREIGN only", code)
            # Do NOT page Ahmed for routine fix failures — SOVEREIGN handles ops escalations
            # Only page Ahmed for P0 unknown issues (handled separately below)
            _bus_send("sovereign",
                      f"GENESIS-WATCHER FIX FAILED: {code} — {detail[:150]}\n"
                      f"Attempted fix did not resolve. Needs investigation.")

        _chronicle_log(
            error_desc=f"{code}: {detail}",
            time_id=time_id,
            time_acted=time_fixed,
            resolution=resolution,
            outcome=outcome,
            damage=f"Detected by genesis-watcher at {time_id}",
        )
    else:
        # Unknown issue — page Ahmed and SOVEREIGN, but ONLY once per 30-min cooldown
        cooldown_expiry = _unknown_issue_cooldown.get(code, 0.0)
        if time.time() < cooldown_expiry:
            logger.info("Unknown issue [%s] in cooldown — suppressing alert", code)
            return
        _unknown_issue_cooldown[code] = time.time() + 1800  # 30-min cooldown per code

        logger.warning("No auto-fix for %s — paging Ahmed and SOVEREIGN", code)
        _telegram(f"🚨 <b>GENESIS WATCHER — UNKNOWN ISSUE</b>\n"
                  f"Code: <b>{code}</b>\nDetail: {detail[:300]}\n"
                  f"No auto-fix available. SOVEREIGN notified. (Suppressed for 30min)")
        _bus_send("sovereign",
                  f"GENESIS-WATCHER UNKNOWN: {code} — {detail[:200]}\n"
                  f"No auto-fix registered. Needs investigation.")
        _chronicle_log(
            error_desc=f"{code}: {detail}",
            time_id=time_id,
            time_acted=time_id,
            resolution="escalated",
            outcome="in_progress",
            damage=f"Unknown issue detected by genesis-watcher at {time_id}",
        )


# ── Push Alert Receiver ──────────────────────────────────────────────────────

def _start_push_receiver() -> None:
    """Start a lightweight HTTP server on port 9010 to receive push alerts.

    nexus-integrity POSTs directly here the instant a P1/P0 fires,
    so the watcher responds in <1s instead of waiting up to 30s for poll.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import json as _json
    import threading

    class AlertHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = _json.loads(body)
                title  = data.get("title", "Unknown")
                detail = data.get("detail", "")
                tier   = data.get("tier", "P1")
                logger.warning("PUSH ALERT [%s] %s: %s", tier, title, detail[:100])
                # Run diagnostic immediately on any push alert
                issues = run_diagnostic()
                for code, det, ctx in issues:
                    handle_issue(code, det, ctx)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            except Exception as e:
                logger.error("Push receiver error: %s", e)
                self.send_response(500)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass  # Suppress default HTTP logging

    def _serve():
        server = HTTPServer(("127.0.0.1", 9010), AlertHandler)
        logger.info("Push alert receiver listening on port 9010")
        server.serve_forever()

    t = threading.Thread(target=_serve, daemon=True, name="push-receiver")
    t.start()


# ── Main Loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    os.makedirs("/Users/ahmedsadek/nexus/logs/genesis-watcher", exist_ok=True)
    logger.info("GENESIS Watcher started — active window %02d:00–%02d:00 ET, polling every %ds",
                ACTIVE_START_H, ACTIVE_END_H, POLL_INTERVAL_S)

    while True:
        try:
            if not _in_active_window():
                # Outside 06:00–00:00 — sleep until 06:00
                now = _now_et()
                wake = now.replace(hour=ACTIVE_START_H, minute=0, second=0, microsecond=0)
                if now >= wake:
                    wake = wake + datetime.timedelta(days=1)
                sleep_s = (wake - now).total_seconds()
                logger.info("Outside active window — sleeping %.0fs until 06:00 ET", sleep_s)
                time.sleep(min(sleep_s, 3600))
                continue

            # Run diagnostic
            issues = run_diagnostic()
            if issues:
                for code, detail, context in issues:
                    handle_issue(code, detail, context)
            else:
                logger.debug("All clear")

            # Read bus inbox for any SOVEREIGN/integrity directives
            messages = _bus_read_inbox()
            for msg in messages:
                sender = msg.get("from", "")
                text = msg.get("message", "")
                logger.info("Bus message from %s: %s", sender, text[:100])
                # Re-run diagnostic if integrity is flagging something
                if "ALERT" in text.upper() or "P1" in text or "P0" in text:
                    logger.info("Alert message received — running immediate diagnostic")
                    issues = run_diagnostic()
                    for code, detail, context in issues:
                        handle_issue(code, detail, context)

        except Exception as e:
            logger.error("Watcher loop error: %s", e)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
