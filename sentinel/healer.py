"""
healer.py — Pipeline Sentinel Auto-Heal

Attempts to fix known failure modes automatically before escalating.
Each heal action returns (healed: bool, action_taken: str).
"""

import logging
import subprocess
import sys
import time

import requests

from config import (
    SERVICE_PLIST, NEXUS_SECRET,
    ALPHA_EXEC_URL, PRIME_EXEC_URL,
    HEAL_RECHECK_SECONDS, OMNI_RESTART_WAIT_SECONDS,
    OBSERVE_ONLY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)
from checks import check_service_health, check_execution_health, check_omni_health

log = logging.getLogger("sentinel.healer")

# Route through alert broker for dedup + rate limiting
try:
    sys.path.insert(0, "/Users/ahmedsadek/nexus")
    from shared.alert_client import send_alert as _broker_send
    _BROKER_OK = True
except Exception as _be:
    _BROKER_OK = False
    log.warning("healer: alert_client unavailable: %s", _be)


def _alert_ahmed(text: str) -> None:
    """Route alert through broker (dedup). Best-effort — never raises."""
    if _BROKER_OK:
        try:
            _broker_send(
                source="sentinel-healer",
                level="WARNING",
                title=text[:200],
                body=text[200:] if len(text) > 200 else "",
                dedup_key=f"sentinel-healer:{text[:80]}",
                targets=["nexus_health_group"],  # healer events → health group only
            )
            log.info("Healer alert routed via broker: %s", text[:80])
            return
        except Exception as _be:
            log.warning("healer: broker send failed, falling back: %s", _be)
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        log.info("Ahmed alert sent (direct fallback): %s", text[:80])
    except Exception as e:
        log.error("Ahmed alert failed: %s", e)


def _launchctl_restart(plist_name: str, wait: int = HEAL_RECHECK_SECONDS) -> bool:
    """
    Stop and start a launchd service. Returns True if the restart command succeeds.

    Args:
        plist_name: LaunchAgent label (e.g. 'ai.nexus.cipher')
        wait:       Seconds to wait after start before returning

    Returns:
        True if launchctl commands exited without error.
    """
    log.info("Restarting %s via launchctl", plist_name)
    try:
        subprocess.run(["launchctl", "stop", plist_name], capture_output=True, timeout=10)
        time.sleep(2)
        result = subprocess.run(
            ["launchctl", "start", plist_name], capture_output=True, timeout=10
        )
        time.sleep(wait)
        return result.returncode == 0
    except Exception as e:
        log.error("launchctl restart failed for %s: %s", plist_name, e)
        return False


def _resume_execution(url: str, secret: str) -> bool:
    """POST to /resume on an execution service."""
    try:
        resp = requests.post(
            f"{url}/resume",
            headers={"X-Nexus-Secret": secret},
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception as e:
        log.error("Resume POST failed to %s: %s", url, e)
        return False


def heal_service(service: str) -> tuple[bool, str]:
    """
    Attempt to heal a service that failed its health check.

    Steps:
        1. Restart via launchctl
        2. Wait HEAL_RECHECK_SECONDS
        3. Re-check health

    Returns:
        (healed, action_taken)
    """
    if OBSERVE_ONLY:
        log.info("OBSERVE_ONLY mode — skipping restart for %s (Phase 0: NSP transition)", service)
        return False, f"OBSERVE_ONLY mode active — restart suppressed for {service}"

    plist = SERVICE_PLIST.get(service)
    if not plist:
        return False, f"No plist registered for {service}"

    wait = OMNI_RESTART_WAIT_SECONDS if service == "omni" else HEAL_RECHECK_SECONDS
    action = f"launchctl restart {plist} (wait {wait}s)"

    restarted = _launchctl_restart(plist, wait=wait)
    if not restarted:
        return False, f"launchctl restart command failed for {plist}"

    # Verify recovery
    passed, detail = check_service_health(service)
    if passed:
        log.info("Heal SUCCESS for %s — %s", service, detail)
        return True, action
    log.warning("Heal FAILED for %s after restart — %s", service, detail)
    return False, action


def _pre_heal_check(system: str) -> dict:
    """
    P0-B: Diagnose the execution service before applying any heal action.
    Returns a dict with the recommended action and reason.

    Actions:
      RESUME           - execution_paused=true, service healthy
      RESTART          - stale_deploy=true or service unreachable
      RESTART_ALERT    - loop_active=false during market hours (restart + alert Ahmed)
      WAIT_AND_RETRY   - alpaca_reachable=false (external issue)
      NO_ACTION        - pre-market hours, loop_active=false expected, or service healthy
    """
    import datetime
    url = ALPHA_EXEC_URL if system == "alpha" else PRIME_EXEC_URL

    try:
        resp = requests.get(f"{url}/health", timeout=8)
        if resp.status_code != 200:
            return {"action": "RESTART", "reason": f"health returned HTTP {resp.status_code}"}
        data = resp.json()
    except Exception as e:
        return {"action": "RESTART", "reason": f"service unreachable: {e}"}

    # Priority 1: stale_deploy — /resume won't fix stale code
    if data.get("stale_deploy"):
        return {"action": "RESTART", "reason": "stale_deploy=true — code on disk newer than running process"}

    # Priority 2: alpaca_reachable=false — external issue, not a service failure
    if data.get("alpaca_reachable") is False:
        return {"action": "WAIT_AND_RETRY", "reason": "alpaca_reachable=false — broker connectivity issue"}

    # Priority 3: execution_paused — /resume is the correct action
    if data.get("execution_paused"):
        return {"action": "RESUME", "reason": "execution_paused=true"}

    # Priority 3b: FLAW 2 fix — execution_valid=false with no clear cause
    # execution_paused=false + alpaca_reachable=true + execution_valid=false
    # = code/data integrity failure. /resume will not fix this. Restart + GENESIS.
    if data.get("execution_valid") is False:
        return {
            "action": "RESTART_GENESIS",
            "reason": (
                "execution_valid=false with no clear cause (not paused, alpaca reachable) — "
                "code or data integrity failure, restart + GENESIS escalation required"
            ),
        }

    # Priority 4: loop_active=false — time-gated
    loop_active = data.get("loop_active")  # None if field absent (execution services may not have it)
    if loop_active is False:
        try:
            import pytz as _tz
            et_now = datetime.datetime.now(_tz.timezone("America/New_York"))
            if et_now.time() < datetime.time(9, 25) and et_now.weekday() < 5:
                return {"action": "NO_ACTION", "reason": "loop_active=false pre-market (before 09:25 ET) — expected state"}
            else:
                return {"action": "RESTART_ALERT", "reason": "loop_active=false during market hours — restart + alert"}
        except Exception:
            return {"action": "RESTART_ALERT", "reason": "loop_active=false — time check failed, treating as market hours"}

    # Service appears healthy
    return {"action": "NO_ACTION", "reason": "service appears healthy"}


def heal_execution(system: str) -> tuple[bool, str]:
    """
    Heal Alpha or Prime Execution using pre-heal diagnosis.
    Routes to the correct action based on actual failure type.

    Returns:
        (healed, action_taken)
    """
    import time as _time
    url = ALPHA_EXEC_URL if system == "alpha" else PRIME_EXEC_URL
    service = f"{system}-execution"

    # Diagnose first — never apply a blind fix
    diagnosis = _pre_heal_check(system)
    action = diagnosis["action"]
    reason = diagnosis["reason"]
    log.info("Pre-heal diagnosis for %s: action=%s reason=%s", system, action, reason)

    if action == "NO_ACTION":
        return True, f"no heal needed: {reason}"

    if action == "WAIT_AND_RETRY":
        log.warning("%s: Alpaca unreachable — waiting 60s before retry", system)
        _time.sleep(60)
        # Re-check after wait
        diag2 = _pre_heal_check(system)
        if diag2["action"] == "WAIT_AND_RETRY":
            msg = (
                f"🚨 SENTINEL — {service.upper()}\n"
                f"alpaca_reachable=false persisting after 60s wait.\n"
                f"This is a broker connectivity issue — do NOT restart the service.\n"
                f"Manual investigation required."
            )
            _alert_ahmed(msg)
            return False, "alpaca_reachable=false persists after 60s — Ahmed alerted, no restart taken"
        # Alpaca recovered — recurse with fresh diagnosis
        return heal_execution(system)

    if action == "RESTART_ALERT":
        _alert_ahmed(
            f"🚨 SENTINEL — {service.upper()}\n"
            f"loop_active=false during market hours.\n"
            f"Auto-restarting now via launchctl."
        )
        healed, act = heal_service(service)
        return healed, f"{reason} → plist restart: {act}"

    if action == "RESUME":
        log.info("Attempting /resume on %s execution", system)
        resumed = _resume_execution(url, NEXUS_SECRET)
        if resumed:
            passed, detail = check_execution_health(system)
            if passed:
                return True, f"POST /resume to {url} (was paused)"
        # Resume failed — fall through to restart
        log.warning("%s: /resume failed, escalating to restart", system)

    if action == "RESTART_GENESIS":
        # FLAW 2 fix: execution_valid=false with no clear cause—restart first,
        # then escalate to GENESIS regardless of restart outcome.
        # A restart may clear the state, but root cause needs GENESIS eyes.
        _alert_ahmed(
            f"🚨 SENTINEL — {service.upper()}\n"
            f"execution_valid=false with no diagnosable cause.\n"
            f"Restarting now. GENESIS escalation follows regardless."
        )
        healed, act = heal_service(service)
        # Import here to avoid circular dependency
        try:
            from escalation import _inject_genesis_event
            _inject_genesis_event(service, reason, "")
        except Exception as _e:
            log.error("RESTART_GENESIS: could not notify GENESIS: %s", _e)
        return healed, f"{reason} → restart + GENESIS notified: {act}"

    if action in ("RESTART", "RESUME"):  # RESUME falls here if /resume failed
        healed, act = heal_service(service)
        return healed, f"{reason} → plist restart: {act}"


def heal_agent_picks(agent: str) -> tuple[bool, str]:
    """
    Heal an agent that has submitted 0 picks. Restarts the agent service.

    Returns:
        (healed, action_taken)
    """
    return heal_service(agent)


def heal(component: str) -> tuple[bool, str]:
    """
    Route component name to the appropriate heal function.

    Args:
        component: Component name from check results (e.g. 'cipher', 'alpha-execution')

    Returns:
        (healed, action_taken)
    """
    if component.endswith("-picks"):
        agent = component.replace("-picks", "")
        return heal_agent_picks(agent)

    if component.endswith("-execution"):
        system = component.replace("-execution", "")
        return heal_execution(system)

    if component.endswith("-logs"):
        # CRITICAL logs — restart the agent
        agent = component.replace("-logs", "")
        return heal_service(agent)

    # Default: restart the service
    return heal_service(component)


def heal_circuit_breaker_paper_reset(alpha_buffer_url: str, nexus_secret: str) -> tuple[bool, str]:
    """
    Auto-reset circuit breaker overnight for paper trading mode.
    ONLY fires if:
      1. ALPACA_PAPER=true confirmed
      2. Current time is outside market hours (before 9:00 AM or after 4:30 PM ET)
      3. CB status is STOP or RED
    Never auto-resets in live trading mode — requires Ahmed manual intervention.
    Ahmed directive May 2026.
    """
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Safety: only auto-reset in paper mode
    is_paper = os.getenv("ALPACA_PAPER", "false").lower() == "true"
    if not is_paper:
        return False, "LIVE MODE — CB reset requires Ahmed manual approval"

    # Safety: only outside market hours
    now_et = datetime.now(ZoneInfo("America/New_York"))
    in_market = now_et.weekday() < 5 and (
        now_et.replace(hour=9, minute=0) <= now_et <= now_et.replace(hour=16, minute=30)
    )
    if in_market:
        return False, "Market hours — CB reset requires Ahmed manual approval"

    try:
        resp = requests.post(
            f"{alpha_buffer_url}/circuit-breaker/reset",
            headers={"X-Nexus-Secret": nexus_secret},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("CB auto-reset successful (paper mode, off-hours)")
            _alert_ahmed("✅ Circuit breaker auto-reset (paper mode, off-hours) — V2 ready for tomorrow")
            return True, "CB reset to NORMAL"
        return False, f"CB reset failed: HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"CB reset error: {exc}"


def check_and_heal_backtest_db(backtest_db_path: str) -> tuple[bool, str]:
    """
    Verify backtest DB is accessible and populated.
    Critical: OMNI's deterministic verdicts depend on this data.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(backtest_db_path, timeout=5)
        count = conn.execute("SELECT COUNT(*) FROM historical_win_rates").fetchone()[0]
        conn.close()
        if count < 1000:
            _alert_ahmed(f"⚠️ Backtest DB has only {count} rows — expected 10,000+. OMNI Kelly sizing degraded.")
            return False, f"Backtest DB underpopulated: {count} rows"
        return True, f"Backtest DB healthy: {count} rows"
    except Exception as exc:
        _alert_ahmed(f"🚨 Backtest DB unreachable: {exc} — OMNI Kelly sizing disabled")
        return False, f"Backtest DB error: {exc}"
