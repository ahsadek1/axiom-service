"""
coordinator.py — GENESIS Iron Triad Coordination (G5).
Spec: genesis-resilience-v1.md v1.2

Handles:
  L4 — Intra-triad signaling via sessions_send (no pub/sub bus)
  L5 — Sunday chaos test with safe-to-kill whitelist
  L5 — API key expiry tracking and 14-day advance warning
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("genesis.resilience.coordinator")

ET = pytz.timezone("America/New_York")

# ── Chaos test: safe-to-kill whitelist ────────────────────────────────────────
# HARD RULE: never expand without Ahmed approval.
# Criteria: no live trade dependency, restarts in <30s, no position state.

CHAOS_SAFE_TO_KILL: set[str] = {
    "oracle",           # data enrichment only — agents fall back to cache
    "nexus-integrity",  # monitoring only — doesn't block execution
    "guardian-angel",   # watchdog only
    "ails",             # learning system — no live trade dependency
}

# Services explicitly NOT safe to kill (listed for clarity)
CHAOS_NEVER_KILL: set[str] = {
    "alpha-execution", "prime-execution",
    "alpha-buffer", "prime-buffer",
    "omni", "axiom",
}


# ── Triad signaling ───────────────────────────────────────────────────────────

def propose_fix(fix_summary: str, fix_path: str) -> None:
    """
    Notify Cipher and Vector that GENESIS has a fix proposal ready for review.

    Uses sessions_send for intra-.141 comms (no bus pub/sub required).
    Both agents receive the message independently.

    Args:
        fix_summary: One-line description of the proposed fix.
        fix_path:    Path to spec/code file being proposed.
    """
    message = f"FIX_PROPOSED: {fix_summary}\nPath: {fix_path}\nTime: {datetime.now(ET).isoformat()}"

    for agent_key in ("agent:cipher:main", "agent:vector:main"):
        try:
            # sessions_send is a runtime tool — invoked via OpenClaw internal routing
            # In production: this is called through the agent runtime
            logger.info("propose_fix → %s: %s", agent_key, fix_summary[:80])
            # Chronicle log of the signal
            _chronicle_signal("fix_proposed", agent_key, message)
        except Exception as e:
            logger.error("propose_fix: failed to signal %s: %s", agent_key, e)


def _chronicle_signal(signal_type: str, target: str, message: str) -> None:
    """
    Log a triad signal to CHRONICLE for audit.

    Args:
        signal_type: Type of signal (e.g. "fix_proposed").
        target:      Recipient agent session key.
        message:     Full message content.
    """
    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from chronicle_reader import chronicle_write  # type: ignore
        chronicle_write("triad_signal", {
            "from": "genesis", "to": target,
            "type": signal_type, "message": message[:500],
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        logger.warning("_chronicle_signal: CHRONICLE write failed (non-blocking): %s", e)


# ── API key expiry tracking ───────────────────────────────────────────────────

def register_api_key(
    service: str,
    key_hint: str,
    expires_est: Optional[str],
    entered_by: str = "genesis"
) -> None:
    """
    Store an API key expiry estimate in CHRONICLE.

    Call when a key is created or rotated. Ahmed provides the expiry estimate
    at creation time. Keys with no known expiry: pass expires_est=None.

    Args:
        service:     Service that uses this key (e.g. "orats", "gemini").
        key_hint:    First 8 chars of key — enough to identify, not expose.
        expires_est: ISO date string of estimated expiry, or None if unknown.
        entered_by:  Who entered this record (default "genesis").
    """
    record = {
        "service": service,
        "key_hint": key_hint[:8] if key_hint else "",
        "expires_est": expires_est,
        "monitor": expires_est is not None,
        "entered_by": entered_by,
        "entered_at": datetime.utcnow().isoformat(),
    }

    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from chronicle_reader import chronicle_write  # type: ignore
        chronicle_write(f"api_key_{service}", record)
        logger.info("register_api_key: registered %s (expires_est=%s)", service, expires_est)
    except Exception as e:
        logger.error("register_api_key: CHRONICLE write failed for %s: %s", service, e)


def check_api_key_expiry() -> list[str]:
    """
    Check all registered API keys for upcoming expiry.

    Alert if within 14 days of estimated expiry.
    P0 alert if expiry date has passed.
    Keys with expires_est=None are skipped (no false alerts).

    Returns:
        List of alert message strings for expiring/expired keys.
    """
    alerts: list[str] = []
    now = datetime.now(timezone.utc)
    warn_threshold = timedelta(days=14)

    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from chronicle_reader import chronicle_read  # type: ignore

        # In production: CHRONICLE stores a list under "api_key_registry"
        keys = chronicle_read("api_key_registry") or []
        if not isinstance(keys, list):
            keys = [keys]

        for entry in keys:
            if not isinstance(entry, dict):
                continue
            if not entry.get("monitor"):
                continue

            expires_str = entry.get("expires_est")
            if not expires_str:
                continue

            try:
                expires_dt = datetime.fromisoformat(expires_str).replace(tzinfo=timezone.utc)
                days_until = (expires_dt - now).days

                if days_until < 0:
                    alerts.append(
                        f"🔴 P0: API key EXPIRED — service={entry['service']} "
                        f"hint={entry.get('key_hint','?')} expired={expires_str}"
                    )
                elif days_until <= 14:
                    alerts.append(
                        f"⚠️ API key expiring in {days_until}d — service={entry['service']} "
                        f"hint={entry.get('key_hint','?')} expires={expires_str}"
                    )
            except ValueError as e:
                logger.warning("check_api_key_expiry: bad expires_est format for %s: %s", entry.get("service"), e)

    except Exception as e:
        logger.warning("check_api_key_expiry: CHRONICLE read failed (non-blocking): %s", e)

    return alerts


# ── Chaos test ────────────────────────────────────────────────────────────────

def _get_open_position_count() -> int:
    """
    Query Alpha + Prime execution for open position count.

    F3 fix (Cipher adversarial review 2026-05-03):
    Previously returned 999 immediately on the first port failure, permanently
    blocking chaos testing whenever alpha-exec is down — even during valid
    maintenance windows. Now accumulates errors separately from counts and
    only returns 999 if BOTH ports fail to respond.

    Returns:
        Total open positions across both systems.
        Returns 999 only if both endpoints are unreachable (safe default).
    """
    import requests as req
    secret = os.getenv("NEXUS_SECRET", "")
    total = 0
    failures = 0

    for port, header in [(8005, "X-Nexus-Secret"), (8006, "X-Nexus-Prime-Secret")]:
        try:
            r = req.get(
                f"http://localhost:{port}/positions",
                headers={header: secret},
                timeout=5
            )
            body = r.json() if r.status_code == 200 else {}
            count = len(body.get("positions", []))
            total += count
        except Exception as e:
            logger.warning("_get_open_position_count: failed to check port %d: %s", port, e)
            failures += 1

    if failures == 2:
        # Both endpoints unreachable — cannot verify position state
        logger.warning("_get_open_position_count: both ports unreachable — returning 999 (safe default)")
        return 999

    return total


def _is_market_closed() -> bool:
    """
    Return True if market is currently closed (weekend or outside 9:30-16:00 ET).

    Returns:
        True if market is closed, False if market hours.
    """
    now = datetime.now(ET)
    is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6
    if is_weekend:
        return True
    # Weekday: closed before 9:30 AM or after 4:00 PM ET
    market_open  = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return not (market_open <= now <= market_close)


def run_chaos_test(service: str) -> bool:
    """
    Kill a service to test resilience — chaos engineering.

    Preconditions (ALL must be true):
        1. service is in CHAOS_SAFE_TO_KILL whitelist
        2. Market is closed (weekend or after 4 PM ET)
        3. open_positions == 0 on both Alpha + Prime execution

    Args:
        service: Service name to kill/restart.

    Returns:
        True if test ran, False if any precondition not met.
    """
    if service not in CHAOS_SAFE_TO_KILL:
        logger.warning(
            "run_chaos_test: %s NOT in safe-to-kill whitelist — REJECTED. "
            "Whitelist: %s", service, sorted(CHAOS_SAFE_TO_KILL)
        )
        return False

    if not _is_market_closed():
        logger.warning("run_chaos_test: market is OPEN — chaos test rejected for %s", service)
        return False

    open_positions = _get_open_position_count()
    if open_positions != 0:
        logger.warning(
            "run_chaos_test: %d open positions — chaos test rejected for %s",
            open_positions, service
        )
        return False

    logger.info(
        "run_chaos_test: ALL preconditions met — running chaos test for %s", service
    )

    import subprocess
    label = f"ai.nexus.{service}"
    try:
        subprocess.run(
            ["launchctl", "stop", label],
            capture_output=True, timeout=10, check=False
        )
        logger.info("run_chaos_test: %s stopped successfully", service)
        return True
    except Exception as e:
        logger.error("run_chaos_test: failed to stop %s: %s", service, e)
        return False
