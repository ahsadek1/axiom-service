"""
agent_dm.py — Layer 3: Agent-to-Agent Direct Messaging
Each Nexus/SQS agent can DM SOVEREIGN's bot directly, bypassing group chat blindness.

Telegram rule: bots cannot read other bots' messages in groups.
Solution: each agent has its own bot token and DMs SOVEREIGN's dedicated bot directly.
SOVEREIGN's bot reads the DM → SOVEREIGN session wakes up via bus message.

Agent Bot Registry:
  genesis  → 7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c  (GENESIS15BOT)
  sovereign→ 8611028745:AAGEwi0AcHSsrS1Y8u69wpWB86Jt75pbWyQ  (SOVEREIGN15Bot)
  primus   → uses PRIMUS_BOT_TOKEN env var

SOVEREIGN's inbox chat ID: SOVEREIGN's bot receives DMs from agent bots
via the bus relay → SOVEREIGN session reads bus inbox.

Design: agent DMs go to:
  1. Alert Broker (dedup + rate limit) → Telegram to Ahmed/groups as needed
  2. SOVEREIGN bus → SOVEREIGN's polling loop picks up + wakes session

This module provides dm_sovereign() — the unified inter-agent DM function.
"""

import json
import logging
import os
import threading
import time
from typing import Optional, List

import requests

logger = logging.getLogger("agent_dm")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALERT_BROKER_URL: str = os.getenv("ALERT_BROKER_URL", "http://localhost:9998")
ALERT_BROKER_SECRET: str = os.getenv(
    "ALERT_BROKER_SECRET",
    "ab_secret_f4e2d1c8b7a3e9f5d2c4b6a8e0f3d5c7b9a1e4f6d8c0b2a4e6f8d0c2b4a6e8",
)
SOVEREIGN_BUS_URL: str = os.getenv("SOVEREIGN_BUS_URL", "http://192.168.1.141:9999")

# Agent bot tokens — each agent uses its own token to send
_AGENT_TOKENS: dict = {
    "genesis":  os.getenv("GENESIS_BOT_TOKEN",   "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c"),
    "sovereign":os.getenv("SOVEREIGN_BOT_TOKEN",  "8611028745:AAGEwi0AcHSsrS1Y8u69wpWB86Jt75pbWyQ"),
    "primus":   os.getenv("PRIMUS_BOT_TOKEN",     ""),
    "cipher":   os.getenv("CIPHER_BOT_TOKEN",     ""),
    "atlas":    os.getenv("ATLAS_BOT_TOKEN",      ""),
    "sage":     os.getenv("SAGE_BOT_TOKEN",       ""),
    "omni":     os.getenv("OMNI_BOT_TOKEN",       ""),
}

# SOVEREIGN's Telegram user/bot chat ID for receiving DMs
# Agents DM SOVEREIGN's bot; SOVEREIGN reads these via Telegram getUpdates
# Since bots can't receive DMs from other bots directly, we use the bus as relay
# and optionally post to Ahmed's chat as notification
AHMED_CHAT_ID: str = os.getenv("AHMED_CHAT_ID", "8573754783")
NEXUS_HEALTH_GROUP: str = os.getenv("NEXUS_HEALTH_GROUP_ID", "-5241272802")

_HTTP_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Delivery functions
# ---------------------------------------------------------------------------

def _post_to_bus(from_agent: str, to_agent: str, message: str) -> bool:
    """Post a message to the bus. Returns True on success."""
    try:
        resp = requests.post(
            f"{SOVEREIGN_BUS_URL}/send",
            json={"from": from_agent, "to": to_agent, "message": message},
            timeout=_HTTP_TIMEOUT,
        )
        return resp.status_code < 300
    except Exception as exc:
        logger.warning("agent_dm: bus POST failed: %s", exc)
        return False


def _post_to_broker(
    source: str,
    level: str,
    title: str,
    body: str,
    dedup_key: str,
    targets: List[str],
) -> bool:
    """Route through Alert Broker for dedup + rate limiting."""
    try:
        resp = requests.post(
            f"{ALERT_BROKER_URL}/alert",
            json={
                "source": source,
                "level": level,
                "title": title,
                "body": body,
                "dedup_key": dedup_key,
                "targets": targets,
            },
            headers={"X-Alert-Secret": ALERT_BROKER_SECRET},
            timeout=_HTTP_TIMEOUT,
        )
        return resp.status_code < 300
    except Exception as exc:
        logger.warning("agent_dm: broker POST failed: %s", exc)
        return False


def _direct_telegram(from_token: str, chat_id: str, text: str) -> bool:
    """Send Telegram message using a specific bot token."""
    if not from_token:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{from_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=_HTTP_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception as exc:
        logger.warning("agent_dm: direct Telegram failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dm_sovereign(
    from_agent: str,
    subject: str,
    body: str = "",
    level: str = "INFO",
    dedup_key: Optional[str] = None,
    notify_ahmed: bool = False,
) -> None:
    """
    Send a direct message to SOVEREIGN. Fire-and-forget, daemon thread.

    Routes:
      1. Bus POST → SOVEREIGN inbox (primary — poller picks up within 30s)
      2. Alert Broker → dedup + route to nexus_health_group (if level=CRITICAL)
      3. Direct Telegram to Ahmed (only if notify_ahmed=True or level=CRITICAL)

    Never raises.

    :param from_agent:    Sending agent name (e.g. "genesis", "omni").
    :param subject:       One-line message summary.
    :param body:          Optional detail.
    :param level:         "CRITICAL" | "WARNING" | "INFO"
    :param dedup_key:     Dedup key for Alert Broker. Defaults to "{from_agent}:{subject}".
    :param notify_ahmed:  If True, also DM Ahmed directly.
    """
    def _worker():
        try:
            full_message = f"[{from_agent.upper()}→SOVEREIGN] {subject}"
            if body:
                full_message += f": {body}"

            # 1. Bus (primary path — always attempt)
            bus_ok = _post_to_bus(from_agent, "sovereign", full_message)
            if not bus_ok:
                logger.warning("agent_dm: bus delivery failed for %s → sovereign", from_agent)

            # 2. Alert Broker for CRITICAL/WARNING
            if level in ("CRITICAL", "WARNING"):
                targets = ["nexus_health_group"]
                if notify_ahmed or level == "CRITICAL":
                    targets.append("ahmed")
                _post_to_broker(
                    source=f"agent/{from_agent}",
                    level=level,
                    title=subject,
                    body=body,
                    dedup_key=dedup_key or f"{from_agent}:{subject}",
                    targets=targets,
                )

            # 3. Direct Telegram to Ahmed if requested + bus failed
            elif notify_ahmed and not bus_ok:
                token = _AGENT_TOKENS.get(from_agent, "")
                if token:
                    _direct_telegram(token, AHMED_CHAT_ID, f"[{from_agent}] {subject}\n{body}")

        except Exception as exc:
            logger.error("agent_dm: dm_sovereign worker crashed: %s", exc)

    t = threading.Thread(target=_worker, daemon=True, name=f"agent-dm-{from_agent}")
    t.start()


def dm_agent(
    from_agent: str,
    to_agent: str,
    message: str,
) -> None:
    """
    Send a message from one agent to another via the bus.

    Fire-and-forget, daemon thread. Never raises.

    :param from_agent: Sending agent name.
    :param to_agent:   Receiving agent name.
    :param message:    Message content.
    """
    def _worker():
        try:
            ok = _post_to_bus(from_agent, to_agent, message)
            if not ok:
                logger.warning("agent_dm: bus delivery failed %s → %s", from_agent, to_agent)
        except Exception as exc:
            logger.error("agent_dm: dm_agent crashed: %s", exc)

    t = threading.Thread(target=_worker, daemon=True, name=f"agent-dm-{from_agent}-{to_agent}")
    t.start()
