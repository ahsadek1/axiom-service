"""
router.py — Alert routing and rate limiting for Alert Broker.

Targets:
  ahmed            → Telegram DM to Ahmed (8573754783)
  nexus_health_group → Telegram group (-5241272802)
  sqs_group        → Telegram group (-5130564161)
  sovereign        → Message bus POST to SOVEREIGN inbox
  bus              → Message bus (alias for sovereign)

Rate limit: 10 Telegram sends per 5 minutes per target.
On rate cap: queues remainder, flushes when cap clears.
On Telegram 429: respects Retry-After.
CRITICAL alerts: written to emergency log if ALL delivery paths fail.
"""

import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# alert_manager integration — persist alert state to CHRONICLE
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, "/Users/ahmedsadek/nexus")
    from shared.alert_manager import write_alert as _am_write, resolve_alert as _am_resolve
    _ALERT_MANAGER_AVAILABLE = True
except Exception as _am_err:
    _ALERT_MANAGER_AVAILABLE = False
    logging.getLogger("alert_broker.router").warning(
        "router: alert_manager unavailable — CHRONICLE state disabled: %s", _am_err
    )

logger = logging.getLogger("alert_broker.router")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN: str = os.getenv("GENESIS_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
SOVEREIGN_BUS_URL: str = os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")
EMERGENCY_LOG: str = "/tmp/alert_broker_emergency.log"

RATE_LIMIT_COUNT: int = 10
RATE_LIMIT_WINDOW_S: int = 300   # 5 minutes

HTTP_TIMEOUT: float = 8.0

TARGET_CHAT_IDS: Dict[str, str] = {
    "ahmed":              os.getenv("AHMED_CHAT_ID", "8573754783"),
    "nexus_health_group": os.getenv("NEXUS_HEALTH_GROUP_ID", "-5241272802"),
    "sqs_group":          os.getenv("SQS_GROUP_ID", "-5130564161"),
}

# Agent bus targets — routed via OpenClaw message bus (not Telegram)
AGENT_BUS_TARGETS: Dict[str, str] = {
    "primus": "primus",    # SQS operator agent
    "vector": "vector",    # Root-cause investigation agent
}


# ---------------------------------------------------------------------------
# Rate limiter (per target)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self) -> None:
        self._windows: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, target: str) -> bool:
        """Return True if this target is under the rate limit."""
        now = time.time()
        with self._lock:
            if target not in self._windows:
                self._windows[target] = deque()
            window = self._windows[target]
            # Prune old entries
            while window and now - window[0] > RATE_LIMIT_WINDOW_S:
                window.popleft()
            if len(window) >= RATE_LIMIT_COUNT:
                return False
            window.append(now)
            return True

    def next_available(self, target: str) -> float:
        """Return seconds until next slot is available for this target."""
        now = time.time()
        with self._lock:
            window = self._windows.get(target)
            if not window or len(window) < RATE_LIMIT_COUNT:
                return 0.0
            oldest = window[0]
            return max(0.0, RATE_LIMIT_WINDOW_S - (now - oldest))


_rate_limiter = _RateLimiter()

# Overflow queue for rate-limited messages: target → list of (text, is_critical)
_overflow: Dict[str, List[tuple]] = {}
_overflow_lock = threading.Lock()


def _flush_overflow_worker() -> None:
    """Background thread: flushes overflow queues when rate limit clears."""
    while True:
        time.sleep(15)
        try:
            with _overflow_lock:
                targets = list(_overflow.keys())
            for target in targets:
                wait = _rate_limiter.next_available(target)
                if wait > 0:
                    continue
                with _overflow_lock:
                    pending = _overflow.pop(target, [])
                if pending:
                    logger.info("router: flushing %d overflow alerts for target '%s'", len(pending), target)
                    for text, is_critical in pending:
                        _send_telegram_raw(target, text, is_critical)
        except Exception as exc:
            logger.error("router: overflow flush error: %s", exc)


threading.Thread(target=_flush_overflow_worker, daemon=True, name="alert-overflow-flush").start()


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def _send_telegram_raw(target: str, text: str, is_critical: bool = False) -> bool:
    """
    Send one Telegram message to a named target. Handles 429 Retry-After.
    Returns True on success.
    """
    chat_id = TARGET_CHAT_IDS.get(target)
    if not chat_id:
        logger.warning("router: unknown target '%s' — skipping", target)
        return False

    if not _rate_limiter.allow(target):
        logger.warning("router: rate cap hit for '%s' — queuing overflow", target)
        with _overflow_lock:
            _overflow.setdefault(target, []).append((text, is_critical))
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                logger.warning("router: Telegram 429 for '%s' — sleeping %ds", target, retry_after)
                time.sleep(retry_after)
                continue
            logger.warning("router: Telegram HTTP %d for '%s'", resp.status_code, target)
            return False
        except Exception as exc:
            logger.warning("router: Telegram attempt %d failed for '%s': %s", attempt + 1, target, exc)
            time.sleep(1)

    return False


def _send_bus(text: str) -> bool:
    """POST to SOVEREIGN message bus inbox. Returns True on success."""
    try:
        resp = requests.post(
            f"{SOVEREIGN_BUS_URL}/send",
            json={"from": "alert-broker", "to": "sovereign", "message": text},
            timeout=HTTP_TIMEOUT,
        )
        return resp.status_code < 300
    except Exception as exc:
        logger.warning("router: bus POST failed: %s", exc)
        return False


def _write_emergency_log(text: str) -> None:
    """Last-resort: write to local file if all delivery paths fail."""
    try:
        with open(EMERGENCY_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {text}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

LEVEL_EMOJI = {
    "CRITICAL": "🚨",
    "WARNING":  "⚠️",
    "INFO":     "ℹ️",
}


def _format_message(source: str, level: str, title: str, body: str) -> str:
    emoji = LEVEL_EMOJI.get(level, "📢")
    parts = [f"{emoji} <b>[{level}] {title}</b>", f"<i>Source: {source}</i>"]
    if body:
        parts.append(body)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_alert(
    source: str,
    level: str,
    title: str,
    body: str,
    targets: List[str],
) -> Dict[str, Any]:
    """
    Route an alert to all requested targets.

    Returns dict of target → bool (delivery success).
    Never raises.
    """
    text = _format_message(source, level, title, body)
    is_critical = level == "CRITICAL"
    results: Dict[str, bool] = {}

    for target in targets:
        try:
            if target in ("sovereign", "bus"):
                results[target] = _send_bus(text)
            elif target in AGENT_BUS_TARGETS:
                # Route to agent via OpenClaw message bus
                agent_name = AGENT_BUS_TARGETS[target]
                try:
                    resp = requests.post(
                        f"{SOVEREIGN_BUS_URL}/send",
                        json={"from": "alert-broker", "to": agent_name, "message": text},
                        timeout=HTTP_TIMEOUT,
                    )
                    ok = resp.status_code < 300
                    results[target] = ok
                    if not ok:
                        logger.warning("router: bus dispatch to '%s' returned %d", agent_name, resp.status_code)
                except Exception as bus_exc:
                    logger.warning("router: bus dispatch to '%s' failed: %s", agent_name, bus_exc)
                    results[target] = False
            elif target in TARGET_CHAT_IDS:
                ok = _send_telegram_raw(target, text, is_critical)
                results[target] = ok
                # If CRITICAL and all Telegram paths fail → emergency log
                if is_critical and not ok:
                    _write_emergency_log(f"[FAILED-DELIVERY] {text}")
            else:
                logger.warning("router: unrecognised target '%s'", target)
                results[target] = False
        except Exception as exc:
            logger.error("router: route_alert crashed for target '%s': %s", target, exc)
            results[target] = False
            if is_critical:
                _write_emergency_log(f"[CRASHED-DELIVERY] {text}")

    # -----------------------------------------------------------------------
    # Persist to CHRONICLE agent_alerts (non-blocking, best-effort)
    # -----------------------------------------------------------------------
    if _ALERT_MANAGER_AVAILABLE:
        try:
            any_delivered = any(results.values())
            if any_delivered:
                issue_key = f"{source}:{title[:60]}"
                _am_write(
                    agent=source,
                    severity=level,
                    service=source,
                    issue_key=issue_key,
                    title=title,
                    details=body[:500] if body else "",
                )
        except Exception as _persist_err:
            logger.warning("router: CHRONICLE persist failed (non-fatal): %s", _persist_err)

    return results
