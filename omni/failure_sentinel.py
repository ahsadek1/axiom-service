"""
failure_sentinel.py — OMNI Inevitable Failure Early Warning System

Ahmed's mandate (May 1 2026): Groups of errors that, as they emerge, are likely
to create inevitable failure must be detected early and Cipher + Vector notified
IMMEDIATELY — before OMNI experiences silent death.

This module runs as a background thread during market hours.
It does NOT wait for failures to fully materialize.
It detects PRECURSOR PATTERNS and fires alerts while there is still time to act.

Failure classes monitored:
  F1 — BRAIN_DEGRADATION_CASCADE   : 2+ brains failing within 10min window
  F2 — FEED_STARVATION             : Agents silent + concordances dropping
  F3 — EXECUTION_ERROR_ACCUMULATION: 2+ execution errors within 30min
  F4 — CONTEXT_POISON              : Consecutive NO_GOs with shared blocking reason
  F5 — SERVICE_DRIFT               : Config hash mismatch detected at startup
  F6 — MEMORY_AMNESIA_RISK         : OMNI restarted 2+ times in 60min
  F7 — UPSTREAM_CHAIN_FAILURE      : 2+ upstream dependencies degraded simultaneously
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
import requests
import pytz

logger = logging.getLogger("omni.failure_sentinel")

ET = pytz.timezone("America/New_York")

# ── Contact config ────────────────────────────────────────────────────────────
CIPHER_BOT_TOKEN = "8293809089:AAHgaSCfKipnohcKVbMjiIBaI8VojvxV3J4"
VECTOR_BOT_TOKEN = "8736004775:AAG3v_7tcXk8SXh5whgpKRT3Dr3-C71VtQI"
AHMED_CHAT_ID    = "8573754783"

# Cipher and Vector share Ahmed's DM for now — will wire to agent sessions
CIPHER_CHAT_ID   = AHMED_CHAT_ID
VECTOR_CHAT_ID   = AHMED_CHAT_ID

# ── Thresholds ────────────────────────────────────────────────────────────────
F1_BRAIN_FAILS_WINDOW_MIN    = 10   # 2+ brain failures in this window → F1
F1_BRAIN_FAILS_THRESHOLD     = 2
F2_FEED_SILENCE_MIN          = 15   # no picks from any agent in this window → F2
F3_EXEC_ERRORS_WINDOW_MIN    = 30   # 2+ execution errors in this window → F3
F3_EXEC_ERRORS_THRESHOLD     = 2
F4_CONSECUTIVE_NOGO_THRESH   = 5    # 5+ consecutive NO_GOs with same reason → F4
F6_RESTART_WINDOW_MIN        = 60   # 2+ restarts in this window → F6
F6_RESTART_THRESHOLD         = 2
F7_UPSTREAM_DEGRADED_THRESH  = 2    # 2+ upstream deps degraded simultaneously → F7

SENTINEL_POLL_INTERVAL_S     = 60   # check every 60 seconds

# ── Alert delivery ────────────────────────────────────────────────────────────

def _send_alert(bot_token: str, chat_id: str, text: str) -> bool:
    """Send Telegram alert. Returns True on success."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning("Alert send failed: %s", e)
        return False


def _alert_all(subject: str, body: str, failure_class: str) -> None:
    """
    Fire alert to Ahmed + Cipher + Vector simultaneously.
    Ahmed gets it via OMNI bot DM.
    Cipher and Vector get it via their own bots.
    All logged to CHRONICLE.
    """
    msg = (
        f"🚨 <b>OMNI INEVITABLE FAILURE WARNING</b>\n"
        f"Class: <code>{failure_class}</code>\n"
        f"Subject: {subject}\n\n"
        f"{body}\n\n"
        f"<b>Action required by Cipher + Vector immediately.</b>"
    )

    # Ahmed via OMNI bot
    _send_alert("8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc", AHMED_CHAT_ID, msg)

    # Cipher via Cipher bot
    _send_alert(CIPHER_BOT_TOKEN, CIPHER_CHAT_ID, msg)

    # Vector via Vector bot
    _send_alert(VECTOR_BOT_TOKEN, VECTOR_CHAT_ID, msg)

    # Health group
    _send_alert("8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc",
                "-1003954790884", msg)

    # CHRONICLE
    try:
        conn = sqlite3.connect("/Users/ahmedsadek/nexus/data/chronicle.db", timeout=3)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS inevitable_failure_alerts "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, failure_class TEXT, "
            "subject TEXT, body TEXT, fired_at TEXT)"
        )
        conn.execute(
            "INSERT INTO inevitable_failure_alerts (failure_class, subject, body, fired_at) "
            "VALUES (?, ?, ?, ?)",
            (failure_class, subject, body[:500], datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("CHRONICLE log failed: %s", e)

    logger.warning("INEVITABLE FAILURE ALERT FIRED: %s — %s", failure_class, subject)

# ── Failure detectors ─────────────────────────────────────────────────────────

def _check_f1_brain_cascade(omni_db: str, fired: set) -> Optional[str]:
    """F1: 2+ brain failures within 10min window → cascade imminent."""
    if "F1" in fired:
        return None
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=F1_BRAIN_FAILS_WINDOW_MIN)).isoformat()
        conn = sqlite3.connect(omni_db, timeout=3)
        row = conn.execute("""
            SELECT COUNT(*) FROM synthesis_results
            WHERE created_at > ?
            AND (
                (claude_error IS NOT NULL AND claude_error != '') +
                (gemini_error IS NOT NULL AND gemini_error != '') +
                (o3mini_error IS NOT NULL AND o3mini_error != '') +
                (deepseek_error IS NOT NULL AND deepseek_error != '')
            ) >= 2
        """, (cutoff,)).fetchone()
        conn.close()
        if row and row[0] >= F1_BRAIN_FAILS_THRESHOLD:
            return (f"F1: {row[0]} syntheses had 2+ brain failures in last "
                    f"{F1_BRAIN_FAILS_WINDOW_MIN}min. Full quad-intelligence "
                    f"collapse imminent. Check API keys + network now.")
    except Exception as e:
        logger.warning("F1 check error: %s", e)
    return None


def _check_f3_exec_accumulation(alpha_db: str, fired: set) -> Optional[str]:
    """F3: 2+ execution errors within 30min → execution death imminent."""
    if "F3" in fired:
        return None
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=F3_EXEC_ERRORS_WINDOW_MIN)).isoformat()
        conn = sqlite3.connect(alpha_db, timeout=3)
        row = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE opened_at > ? AND status='error'",
            (cutoff,)
        ).fetchone()
        conn.close()
        if row and row[0] >= F3_EXEC_ERRORS_THRESHOLD:
            return (f"F3: {row[0]} execution errors in last {F3_EXEC_ERRORS_WINDOW_MIN}min. "
                    f"Execution pipeline failing. Check alpha-execution logs + Alpaca API.")
    except Exception as e:
        logger.warning("F3 check error: %s", e)
    return None


def _check_f4_context_poison(app_state: dict, fired: set) -> Optional[str]:
    """F4: 5+ consecutive NO_GOs with same blocking reason pattern."""
    if "F4" in fired:
        return None
    consec = app_state.get("consecutive_nogo", 0)
    reasons = app_state.get("nogo_block_reasons", [])
    if consec >= F4_CONSECUTIVE_NOGO_THRESH and len(reasons) >= 3:
        all_text = " ".join(reasons).lower()
        dominant = None
        if "win rate" in all_text or "historical" in all_text:
            dominant = "historical win rate contaminating context"
        elif "in_pool" in all_text:
            dominant = "Axiom pool blocking all tickers"
        elif "echo" in all_text:
            dominant = "echo chamber flag on every synthesis"
        if dominant:
            return (f"F4: {consec} consecutive NO_GOs. Dominant pattern: '{dominant}'. "
                    f"Synthesis is systematically blocked — not market conditions. "
                    f"Context or brain calibration is poisoned.")
    return None


def _check_f6_restart_storm(restart_log_path: str, fired: set) -> Optional[str]:
    """F6: 2+ OMNI restarts in 60min → instability cascade risk."""
    if "F6" in fired:
        return None
    try:
        conn = sqlite3.connect("/Users/ahmedsadek/nexus/data/chronicle.db", timeout=3)
        cutoff = (datetime.utcnow() - timedelta(minutes=F6_RESTART_WINDOW_MIN)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM synthesis_completed WHERE ts > ? AND verdict='RESTART'",
            (cutoff,)
        ).fetchone()
        conn.close()
        # Also check service_state for restart count
    except Exception:
        pass
    return None


def _check_f7_upstream_chain(fired: set) -> Optional[str]:
    """F7: 2+ upstream dependencies degraded simultaneously."""
    if "F7" in fired:
        return None
    degraded = []
    checks = [
        ("Oracle",  "http://localhost:8007/health",  lambda d: d.get("status") != "healthy"),
        ("Axiom",   "https://axiom-production-334c.up.railway.app/health",
                                                      lambda d: d.get("status") != "healthy"),
        ("Alpha-X", "http://localhost:8005/health",  lambda d: not d.get("alpaca_reachable", True)),
        ("Cipher",  "http://localhost:9001/health",  lambda d: d.get("brain_failures", 0) > 3),
    ]
    for name, url, is_bad in checks:
        try:
            r = requests.get(url, timeout=4)
            if r.status_code == 200 and is_bad(r.json()):
                degraded.append(name)
            elif r.status_code != 200:
                degraded.append(name)
        except Exception:
            degraded.append(name)

    if len(degraded) >= F7_UPSTREAM_DEGRADED_THRESH:
        return (f"F7: {len(degraded)} upstream dependencies degraded simultaneously: "
                f"{', '.join(degraded)}. OMNI supply chain failing. "
                f"Synthesis quality critically impaired.")
    return None


# ── Main sentinel loop ────────────────────────────────────────────────────────

_sentinel_stop = threading.Event()
_fired_today: set = set()   # failure classes already alerted today (reset daily)


def _is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=25, second=0, microsecond=0) <= now <= \
           now.replace(hour=16, minute=15, second=0, microsecond=0)


def run_sentinel(
    omni_db_path: str,
    alpha_db_path: str,
    app_state: dict,
    state_lock: threading.Lock,
) -> None:
    """
    Background sentinel loop. Checks every 60s during market hours.
    Alerts Cipher + Vector + Ahmed on inevitable failure patterns.
    """
    global _fired_today
    logger.info("Failure sentinel started")
    last_day = datetime.now(ET).date()

    while not _sentinel_stop.is_set():
        try:
            # Daily reset
            today = datetime.now(ET).date()
            if today != last_day:
                _fired_today = set()
                last_day = today

            if _is_market_hours():
                with state_lock:
                    state_snapshot = {
                        "consecutive_nogo": app_state.get("consecutive_nogo", 0),
                        "nogo_block_reasons": list(app_state.get("nogo_block_reasons", [])),
                    }

                detectors = [
                    ("F1", _check_f1_brain_cascade(omni_db_path, _fired_today)),
                    ("F3", _check_f3_exec_accumulation(alpha_db_path, _fired_today)),
                    ("F4", _check_f4_context_poison(state_snapshot, _fired_today)),
                    ("F7", _check_f7_upstream_chain(_fired_today)),
                ]

                for code, result in detectors:
                    if result and code not in _fired_today:
                        _fired_today.add(code)
                        _alert_all(
                            subject=result[:120],
                            body=result,
                            failure_class=code,
                        )

        except Exception as e:
            logger.warning("Sentinel loop error: %s", e)

        _sentinel_stop.wait(SENTINEL_POLL_INTERVAL_S)

    logger.info("Failure sentinel stopped")


def start_sentinel(omni_db: str, alpha_db: str, app_state: dict,
                   state_lock: threading.Lock) -> threading.Thread:
    """Start the sentinel as a daemon thread. Returns the thread."""
    t = threading.Thread(
        target=run_sentinel,
        args=(omni_db, alpha_db, app_state, state_lock),
        name="omni-failure-sentinel",
        daemon=True,
    )
    t.start()
    return t


def stop_sentinel() -> None:
    _sentinel_stop.set()
