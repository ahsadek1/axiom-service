"""
sentinel.py — Pipeline Sentinel Main Process

Runs every 5 minutes. Checks the full V2 trading chain.
Writes heartbeat at end of each cycle. Detects its own downtime.
Escalates failures through 3 tiers: auto-heal → GENESIS → OMNI+Cipher.
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from config import (
    ET, CHECK_INTERVAL_SECONDS, HEARTBEAT_PATH, HEARTBEAT_STALE_THRESHOLD,
    MARKET_OPEN_TIME, MARKET_CLOSE_TIME, PRE_MARKET_CHECK_TIME,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SENTINEL_LOG,
)
from checks import check_all_services, check_all_agent_picks, check_execution_health, check_omni_health
from escalation import run_escalation, _telegram

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.sovereign_comms import get_instructions, report as sovereign_report

MARKET_OPEN_CHECK_SCRIPT = "/Users/ahmedsadek/.openclaw/workspace-axiom/check_market_open.py"

# ── Logging Setup ──────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(SENTINEL_LOG), exist_ok=True)
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(SENTINEL_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sentinel.main")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Track which components are currently in escalation to avoid duplicate Tier 2/3 threads
_active_escalations: set[str] = set()

# Track consecutive failures per component to distinguish new failures from lingering ones
_failure_counts: dict[str, int] = {}

# SOVEREIGN directive: pause all escalation actions (checks still run, alerts suppressed)
_sovereign_paused: bool = False


def _dispatch_sentinel_instruction(instr: dict) -> None:
    """Execute a SOVEREIGN instruction for the sentinel. Supported: PAUSE, RESUME, STATUS."""
    global _sovereign_paused, _active_escalations, _failure_counts

    raw = instr.get("message", "").strip()
    directive = raw.split(":", 1)[0].strip().upper() if ":" in raw else raw.upper()

    log.info("SOVEREIGN directive received — raw: %s", raw[:200])

    if directive in ("HALT", "PAUSE"):
        _sovereign_paused = True
        log.warning("SOVEREIGN DIRECTIVE: PAUSE — sentinel escalations suspended")
        sovereign_report("sentinel", "ack", {"directive": directive, "status": "applied", "paused": True})
    elif directive == "RESUME":
        _sovereign_paused = False
        log.info("SOVEREIGN DIRECTIVE: RESUME — sentinel escalations resumed")
        sovereign_report("sentinel", "ack", {"directive": "RESUME", "status": "applied", "paused": False})
    elif directive == "STATUS":
        sovereign_report("sentinel", "status", {
            "directive": "STATUS",
            "paused": _sovereign_paused,
            "active_escalations": list(_active_escalations),
            "failure_counts": dict(_failure_counts),
        })
        log.info("SOVEREIGN DIRECTIVE: STATUS — reported back")
    elif directive == "FLUSH":
        _active_escalations.clear()
        _failure_counts.clear()
        log.info("SOVEREIGN DIRECTIVE: FLUSH — escalation state cleared")
        sovereign_report("sentinel", "ack", {"directive": "FLUSH", "status": "applied"})
    else:
        log.warning("SOVEREIGN DIRECTIVE: unrecognized '%s'", directive[:100])
        sovereign_report("sentinel", "ack", {"directive": directive[:100], "status": "unrecognized"})


def _write_heartbeat() -> None:
    """Write current UTC timestamp to heartbeat file."""
    try:
        Path(HEARTBEAT_PATH).write_text(str(datetime.utcnow().timestamp()))
    except Exception as e:
        log.error("Failed to write heartbeat: %s", e)


def _read_heartbeat_age() -> float:
    """Return seconds since last heartbeat, or infinity if missing."""
    try:
        ts = float(Path(HEARTBEAT_PATH).read_text().strip())
        return (datetime.utcnow() - datetime.utcfromtimestamp(ts)).total_seconds()
    except Exception:
        return float("inf")


def _is_market_holiday() -> bool:
    """
    Return True if today is a NYSE market holiday (market is closed all day).
    Uses check_market_open.py as the authoritative source.
    Falls back to False on any script error (fail-open: assume not a holiday).
    """
    try:
        result = subprocess.run(
            [sys.executable, MARKET_OPEN_CHECK_SCRIPT],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        # Exit code 1 = market closed. Only treat as holiday if it's outside
        # the normal pre/post-market window (i.e., during what would be core hours).
        if result.returncode != 0:
            now = datetime.now(ET)
            t = (now.hour, now.minute)
            # If we're in what should be market hours (9:30–16:00) but market
            # is reported closed → genuine holiday
            if MARKET_OPEN_TIME <= t <= MARKET_CLOSE_TIME:
                log.info("Market holiday detected by check_market_open.py: %s", output.strip().split('\n')[0])
                return True
        return False
    except Exception as e:
        log.warning("Holiday check failed (fail-open): %s", e)
        return False


def _in_market_hours() -> bool:
    """Return True if current ET time is within market hours AND it is not a holiday."""
    now = datetime.now(ET)
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    t = (now.hour, now.minute)
    if not (MARKET_OPEN_TIME <= t <= MARKET_CLOSE_TIME):
        return False
    if _is_market_holiday():
        return False
    return True


def _in_pre_market_window() -> bool:
    """Return True if current ET time is the pre-market check window (09:05–09:15) AND not a holiday."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    if not (PRE_MARKET_CHECK_TIME <= t < MARKET_OPEN_TIME):
        return False
    if _is_market_holiday():
        return False
    return True


def _check_and_escalate(failures: list[tuple[str, str]]) -> None:
    """
    For each failure, trigger escalation in the foreground.
    Skips components already being escalated.
    Respects SOVEREIGN PAUSE directive.
    """
    if _sovereign_paused:
        log.warning("SOVEREIGN PAUSE active — %d failure(s) detected but escalation suppressed", len(failures))
        sovereign_report("sentinel", "status", {
            "event": "escalation_suppressed",
            "reason": "sovereign_pause",
            "failures": [f[0] for f in failures],
        })
        return

    for component, detail in failures:
        if component in _active_escalations:
            log.info("Skipping %s — escalation already in progress", component)
            continue
        _active_escalations.add(component)
        try:
            log.warning("Escalating: %s — %s", component, detail)
            run_escalation(component, detail)
        except Exception as e:
            log.error("Escalation error for %s: %s", component, e)
        finally:
            _active_escalations.discard(component)


def run_service_only_check() -> list[tuple[str, str]]:
    """
    Run service health checks only (no pick/execution checks).
    Used outside market hours.
    """
    failures = []
    for service, passed, detail in check_all_services():
        if not passed:
            failures.append((service, detail))
    return failures


def run_market_check() -> list[tuple[str, str]]:
    """
    Run full check suite during market hours.
    """
    failures = []

    # Service health
    for service, passed, detail in check_all_services():
        if not passed:
            failures.append((service, detail))

    # Agent picks — gated behind a direct market-open check.
    # This is a second independent guard (the first is _in_market_hours() above).
    # Prevents 0-picks false positives on NYSE holidays even if the outer
    # gate is bypassed by a holiday-check crash or timing edge case.
    _picks_market_open = True
    try:
        _result = subprocess.run(
            [sys.executable, MARKET_OPEN_CHECK_SCRIPT],
            capture_output=True, text=True, timeout=10,
        )
        if _result.returncode != 0:
            _picks_market_open = False
            log.info(
                "Agent picks check skipped — market closed per check_market_open.py: %s",
                (_result.stdout + _result.stderr).strip().split('\n')[0],
            )
    except Exception as _e:
        log.warning("Picks market-open gate failed (%s) — allowing picks check (fail-open)", _e)

    if _picks_market_open:
        for agent, passed, detail in check_all_agent_picks():
            if not passed:
                failures.append((f"{agent}-picks", detail))

    # Execution health
    for system in ("alpha", "prime"):
        passed, detail = check_execution_health(system)
        if not passed:
            failures.append((f"{system}-execution", detail))

    # OMNI
    passed, detail = check_omni_health()
    if not passed:
        failures.append(("omni", detail))

    return failures


def check_own_heartbeat() -> None:
    """
    On startup, check if the last heartbeat is stale (sentinel was down).
    If so, alert Ahmed immediately.
    """
    age = _read_heartbeat_age()
    if age == float("inf"):
        log.info("No prior heartbeat found — first run")
        return
    if age > HEARTBEAT_STALE_THRESHOLD:
        minutes_down = int(age // 60)
        log.warning("Stale heartbeat detected: sentinel was down for ~%d minutes", minutes_down)
        _telegram(
            f"⚠️ <b>SENTINEL — Self-Recovery</b>\n"
            f"Sentinel was down for ~{minutes_down} minutes and has now restarted.\n"
            f"Missed check windows during downtime.\n"
            f"Running immediate full check now.\n"
            f"Time: {datetime.now(ET).strftime('%H:%M ET')}"
        )
    else:
        log.info("Heartbeat fresh (%.0fs ago) — sentinel running normally", age)


def main_loop() -> None:
    """
    Main sentinel loop.

    Runs indefinitely. Each iteration:
    1. Determine if market hours
    2. Run appropriate check suite
    3. Escalate failures
    4. Write heartbeat
    5. Sleep until next cycle
    """
    log.info("=" * 60)
    log.info("PIPELINE SENTINEL STARTING — check interval=%ds", CHECK_INTERVAL_SECONDS)
    log.info("=" * 60)

    # Check own heartbeat on startup
    check_own_heartbeat()

    # Send startup notification
    _telegram(
        f"🟢 <b>SENTINEL ONLINE</b>\n"
        f"Check interval: 5 minutes\n"
        f"Escalation: Tier1→Tier2(5min)→Tier3(+3min)\n"
        f"Market hours: 09:15–16:15 ET\n"
        f"Time: {datetime.now(ET).strftime('%H:%M ET')}"
    )

    # Report sentinel startup to SOVEREIGN
    sovereign_report("sentinel", "status", {"event": "started"})

    cycle = 0
    while True:
        cycle += 1
        cycle_start = time.monotonic()
        now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
        log.info("=== CYCLE %d | %s ===", cycle, now_str)

        # ── Poll SOVEREIGN for directives ────────────────────────────────────
        try:
            _instr = get_instructions("sentinel")
            for _i in _instr:
                _dispatch_sentinel_instruction(_i)
        except Exception as exc:
            log.warning("SOVEREIGN poll error: %s", exc)

        try:
            if _in_market_hours():
                log.info("Market hours — running full check")
                failures = run_market_check()
            elif _in_pre_market_window():
                log.info("Pre-market window — running service check only")
                failures = run_service_only_check()
            else:
                log.info("Outside market hours — running service check only")
                failures = run_service_only_check()

            if not failures:
                log.info("ALL CHECKS PASSED")
            else:
                log.warning("%d FAILURE(S): %s", len(failures), [f[0] for f in failures])
                _check_and_escalate(failures)

        except Exception as e:
            log.error("Cycle %d error: %s", cycle, e, exc_info=True)

        # Write heartbeat at end of successful cycle
        _write_heartbeat()
        log.info("Heartbeat written")

        # Sleep until next cycle (account for time spent in checks/escalation)
        elapsed  = time.monotonic() - cycle_start
        sleep_for = max(0, CHECK_INTERVAL_SECONDS - elapsed)
        log.info("Cycle %d complete in %.1fs — sleeping %.0fs", cycle, elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Sentinel stopped by user")
        sys.exit(0)
    except Exception as e:
        log.critical("Sentinel crashed: %s", e, exc_info=True)
        # Try to alert Ahmed before dying
        try:
            _telegram(
                f"💀 <b>SENTINEL CRASHED</b>\n"
                f"Error: {str(e)[:200]}\n"
                f"launchd will restart automatically.\n"
                f"Time: {datetime.now(ET).strftime('%H:%M ET')}"
            )
        except Exception:
            pass
        sys.exit(1)
