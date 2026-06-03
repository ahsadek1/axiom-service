"""
alert_router.py — Iron Triad Alert Router
Spec: resilience-phase1-infrastructure.md v1.0
Author: GENESIS | Built: 2026-05-02

Single ingestion point for all resilience alerts from GENESIS, VECTOR, and Cipher.
Handles dedup, severity triage, cooldown, and routing.

No agent calls Telegram or the bus directly for resilience alerts —
everything goes through route_alert().

Severity routing:
  P0 → Ahmed direct (Telegram) + SOVEREIGN  | cooldown 15 min
  P1 → SOVEREIGN only                        | cooldown 30 min
  P2 → morning brief queue                   | cooldown 4 hours

Never raises — alert routing failure must not block the action that triggered it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from shared.resilience.triad_db import check_and_record_alert, log_intervention

logger = logging.getLogger("resilience.alert_router")

# ---------------------------------------------------------------------------
# Cooldown config (seconds)
# ---------------------------------------------------------------------------

_COOLDOWN = {
    "P0": 15 * 60,    # 15 minutes
    "P1": 30 * 60,    # 30 minutes
    "P2": 4 * 60 * 60,  # 4 hours
}

# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------

_AHMED_CHAT_ID  = os.getenv("AHMED_CHAT_ID", "8573754783")
_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
_BUS_URL        = os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")
_MORNING_BRIEF_PATH = "/Users/ahmedsadek/nexus/shared/resilience/morning_brief_queue.jsonl"


# ---------------------------------------------------------------------------
# Internal senders
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> bool:
    """Send a direct Telegram message to Ahmed. Returns True on success."""
    try:
        import requests
        token = _BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            logger.error("TELEGRAM_BOT_TOKEN not set — cannot send P0 alert to Ahmed")
            return False
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": _AHMED_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


def _send_sovereign(agent: str, issue_type: str, target: str, detail: str) -> bool:
    """Post alert to SOVEREIGN via message bus. Returns True on success."""
    try:
        import requests
        payload = {
            "from": agent,
            "to": "sovereign",
            "message": f"RESILIENCE ALERT [{issue_type}] {target}: {detail}",
        }
        r = requests.post(f"{_BUS_URL}/send", json=payload, timeout=5)
        return r.status_code == 200
    except Exception as e:
        logger.error("SOVEREIGN bus send failed: %s", e)
        return False


def _queue_morning_brief(entry: dict) -> None:
    """Append P2 alert to morning brief queue file."""
    try:
        with open(_MORNING_BRIEF_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error("Morning brief queue write failed: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_alert(
    agent: str,
    issue_type: str,
    target: str,
    severity: str,
    detail: str,
    context: Optional[dict] = None,
) -> bool:
    """
    Route a resilience alert. Returns True if alert was sent, False if deduped/suppressed.
    Never raises — alert routing failure must not block the action that triggered it.

    Args:
        agent:      "genesis" | "vector" | "cipher"
        issue_type: "service_down" | "invariant_violation" | "stale_deploy" | "diagnostic_crash" | etc.
        target:     Affected service or component
        severity:   "P0" | "P1" | "P2"
        detail:     Human-readable detail (keep under 200 chars)
        context:    Optional structured context dict
    """
    try:
        if severity not in ("P0", "P1", "P2"):
            logger.warning("Unknown severity %s — defaulting to P1", severity)
            severity = "P1"

        cooldown = _COOLDOWN.get(severity, _COOLDOWN["P1"])
        dedup_key = f"{issue_type}:{target}:{severity}"

        should_send = check_and_record_alert(dedup_key, cooldown)
        if not should_send:
            logger.debug("Alert suppressed (cooldown): %s", dedup_key)
            return False

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        action_taken = (context or {}).get("action_taken", "none")

        if severity == "P0":
            message = (
                f"🚨 *[P0] {issue_type}* — `{target}`\n"
                f"Agent: {agent}\n"
                f"Detail: {detail}\n"
                f"Time: {timestamp}\n"
                f"Action taken: {action_taken}"
            )
            _send_telegram(message)
            _send_sovereign(agent, issue_type, target, detail)
            logger.critical("P0 ALERT routed: %s / %s — %s", agent, target, detail)

        elif severity == "P1":
            _send_sovereign(agent, issue_type, target, detail)
            logger.warning("P1 ALERT routed to SOVEREIGN: %s / %s — %s", agent, target, detail)

        elif severity == "P2":
            _queue_morning_brief({
                "agent": agent,
                "issue_type": issue_type,
                "target": target,
                "detail": detail,
                "context": context,
                "queued_at": timestamp,
            })
            logger.info("P2 ALERT queued for morning brief: %s / %s", target, detail)

        # Log to intervention_log for audit trail
        log_intervention(
            agent=agent,
            action_type="alert_routed",
            target=target,
            trigger=issue_type,
            outcome="escalated",
            pre_state={"severity": severity, "detail": detail},
            post_state={"routed_to": "ahmed+sovereign" if severity == "P0" else
                                     "sovereign" if severity == "P1" else "morning_brief"},
        )
        return True

    except Exception as e:
        # Never raise — log and return False
        logger.error("route_alert FAILED (non-fatal): %s", e)
        return False
