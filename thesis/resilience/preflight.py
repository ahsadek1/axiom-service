"""
resilience/preflight.py — THESIS Startup Preflight Gate (B1).
Spec: THESIS_RESILIENCE_SPEC v1.0 — Block 1

Runs synchronously during lifespan startup, before APScheduler starts.
Critical failures (CHRONICLE, Anthropic) → STANDBY mode.
Warning failures (ORACLE, SOVEREIGN bus) → alert and continue degraded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("thesis.resilience.preflight")

HTTP_TIMEOUT = 6


@dataclass
class PreflightResult:
    """
    Result of startup preflight checks.

    Attributes:
        checks:         Dict of check_name → "ok" | "FAILED: reason" | "warning: reason"
        critical_failed: List of critical checks that failed.
        ok:             True if no critical failures.
    """
    checks:          Dict[str, str] = field(default_factory=dict)
    critical_failed: List[str]      = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.critical_failed) == 0


async def run_preflight(
    chronicle_db_path: str,
    anthropic_api_key: str,
    oracle_url: Optional[str] = None,
    sovereign_bus_url: Optional[str] = None,
    telegram_bot_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
) -> PreflightResult:
    """
    Execute all startup preflight checks.

    Critical (failure → STANDBY):
        - CHRONICLE: reachable and writable
        - Anthropic: API key valid (model list call)

    Warning (failure → degraded, alert sent):
        - ORACLE: reachable
        - SOVEREIGN bus: reachable

    Args:
        chronicle_db_path:  Absolute path to chronicle.db.
        anthropic_api_key:  Anthropic API key.
        oracle_url:         ORACLE service URL.
        sovereign_bus_url:  SOVEREIGN message bus URL.
        telegram_bot_token: Bot token for direct Telegram alerts.
        telegram_chat_id:   Chat ID for direct Telegram alerts.

    Returns:
        PreflightResult with check outcomes.
    """
    result = PreflightResult()

    # ── Critical: CHRONICLE ───────────────────────────────────────────────────
    try:
        conn = sqlite3.connect(chronicle_db_path, timeout=5)
        conn.execute("SELECT 1")
        # Test write (rolled back)
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE IF NOT EXISTS _preflight_test (id INTEGER)")
        conn.execute("INSERT INTO _preflight_test VALUES (1)")
        conn.execute("ROLLBACK")
        conn.close()
        result.checks["chronicle"] = "ok"
        logger.info("preflight: CHRONICLE — ok")
    except Exception as e:
        result.checks["chronicle"] = f"FAILED: {e}"
        result.critical_failed.append("chronicle")
        logger.error("preflight: CHRONICLE FAILED: %s", e)

    # ── Critical: Anthropic API key ───────────────────────────────────────────
    if not anthropic_api_key:
        result.checks["anthropic"] = "FAILED: ANTHROPIC_API_KEY not set"
        result.critical_failed.append("anthropic")
    else:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_api_key)
            # Lightweight probe: list models
            client.models.list()
            result.checks["anthropic"] = "ok"
            logger.info("preflight: Anthropic — ok")
        except Exception as e:
            result.checks["anthropic"] = f"FAILED: {e}"
            result.critical_failed.append("anthropic")
            logger.error("preflight: Anthropic FAILED: %s", e)

    # ── Warning: ORACLE ───────────────────────────────────────────────────────
    if oracle_url:
        try:
            import requests
            r = requests.get(f"{oracle_url}/health", timeout=HTTP_TIMEOUT)
            result.checks["oracle"] = "ok" if r.status_code == 200 else f"warning: HTTP {r.status_code}"
        except Exception as e:
            result.checks["oracle"] = f"warning: {e}"
        if result.checks["oracle"] != "ok":
            logger.warning("preflight: ORACLE — %s", result.checks["oracle"])

    # ── Warning: SOVEREIGN bus ────────────────────────────────────────────────
    if sovereign_bus_url:
        try:
            import requests
            r = requests.post(
                f"{sovereign_bus_url}/send",
                json={"from": "thesis", "to": "sovereign",
                      "message": "THESIS preflight ping", "event_type": "heartbeat"},
                timeout=HTTP_TIMEOUT
            )
            result.checks["sovereign_bus"] = "ok" if r.status_code in (200, 201) else f"warning: HTTP {r.status_code}"
        except Exception as e:
            result.checks["sovereign_bus"] = f"warning: {e}"
        if result.checks.get("sovereign_bus") != "ok":
            logger.warning("preflight: SOVEREIGN bus — %s", result.checks.get("sovereign_bus"))

    # ── Alert on critical failures ────────────────────────────────────────────
    if result.critical_failed and telegram_bot_token and telegram_chat_id:
        _send_telegram_alert(
            telegram_bot_token, telegram_chat_id,
            f"🔴 THESIS STANDBY: critical preflight failed: {result.critical_failed}\n"
            f"Checks: {result.checks}\nRetrying every 60s."
        )

    return result


def _send_telegram_alert(token: str, chat_id: str, message: str) -> None:
    """Send direct Telegram alert. Fail-open."""
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message[:4000]},
            timeout=8
        )
    except Exception as e:
        logger.warning("_send_telegram_alert: failed: %s", e)
