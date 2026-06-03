"""
shared/notification_router.py — GAP-002: Notification-Status Decoupling

Single entry point for all agent notifications. Routes by tier:

  INFO     → SOVEREIGN bus + Discord channel only. Ahmed never sees these.
  WARN     → SOVEREIGN bus + Discord #escalations. Ahmed not paged.
  ESCALATE → SOVEREIGN bus + Discord #escalations + Ahmed Telegram DM.

Rules:
- Ahmed DM fires ONLY for tier=ESCALATE. Hard enforced here.
- Never raises — all delivery paths are fire-and-forget.
- Falls back gracefully if SOVEREIGN bus or Discord bridge are unreachable.

Usage:
    from shared.notification_router import notify, NotifyTier

    # Informational — SOVEREIGN + Discord only
    notify("alpha-execution", NotifyTier.INFO,
           title="Trade placed",
           body=f"AAPL P1 bull spread — ${premium:.2f} credit",
           ticker="AAPL")

    # Warning — logged prominently but Ahmed not paged
    notify("alpha-execution", NotifyTier.WARN,
           title="VOID trade",
           body=f"AAPL — {void_reason}",
           ticker="AAPL")

    # Escalation — Ahmed gets a DM
    notify("alpha-execution", NotifyTier.ESCALATE,
           title="FATAL AUTH ERROR — manual action required",
           body=f"401 from Alpaca. Execution paused.",
           ticker="AAPL")

Author: Cipher (GAP-002, 2026-04-27)
"""

import logging
import os
from enum import Enum
from typing import Optional

import requests as _requests

logger = logging.getLogger("nexus.notification_router")

# ── Configuration (from env, with sane defaults) ──────────────────────────────

_NEXUS_BUS_URL      = os.environ.get("NEXUS_BUS_URL", "http://192.168.1.141:9999")
_DISCORD_BRIDGE_URL = os.environ.get("DISCORD_BRIDGE_URL", "http://localhost:8010")
_DISCORD_AUTH       = os.environ.get("DISCORD_BRIDGE_SECRET", "")
_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_AHMED_CHAT_ID      = os.environ.get("AHMED_CHAT_ID", "8573754783")

_DISCORD_ESCALATION_CHANNEL = "#escalations"
_DISCORD_AGENT_CHANNELS = {
    "alpha-execution":  "#execution-monitoring",
    "prime-execution":  "#execution-monitoring",
    "omni":             "#go-verdicts",
    "alpha-buffer":     "#go-verdicts",
    "prime-buffer":     "#go-verdicts",
    "sovereign":        "#daily-health-report",
    "primus":           "#primus-sqs",
    "vector":           "#vector-investigations",
    "genesis":          "#findings-stream-a",
    "cipher":           "#ahmed-decisions",
    "axiom":            "#daily-health-report",
    "atlas":            "#go-verdicts",
    "sage":             "#go-verdicts",
}

_TIER_COLORS = {
    "INFO":     "teal",
    "WARN":     "yellow",
    "ESCALATE": "red",
}


# ── Tier Definition ───────────────────────────────────────────────────────────

class NotifyTier(str, Enum):
    INFO     = "INFO"      # operational status — SOVEREIGN + Discord only
    WARN     = "WARN"      # degraded state — SOVEREIGN + Discord #escalations
    ESCALATE = "ESCALATE"  # requires human action — SOVEREIGN + Discord + Ahmed DM


# ── Delivery Functions ────────────────────────────────────────────────────────

def _post_bus(agent: str, title: str, body: str, tier: str) -> None:
    """Send to SOVEREIGN message bus. Fire-and-forget."""
    try:
        text = f"[{tier}] {title}\n{body}"
        _requests.post(
            f"{_NEXUS_BUS_URL}/send",
            json={"from": agent, "to": "sovereign", "message": text},
            timeout=4,
        )
    except Exception as e:
        logger.warning("notification_router: bus delivery failed: %s", e)


def _post_discord(
    channel: str,
    agent: str,
    title: str,
    body: str,
    color: str,
    ticker: Optional[str] = None,
) -> None:
    """Send rich embed to Discord bridge. Fire-and-forget."""
    if not _DISCORD_BRIDGE_URL:
        return
    try:
        fields = []
        if ticker:
            fields.append({"name": "Ticker", "value": ticker, "inline": True})
        fields.append({"name": "Agent", "value": agent.upper(), "inline": True})

        _requests.post(
            f"{_DISCORD_BRIDGE_URL}/post",
            headers={"X-Bridge-Auth": _DISCORD_AUTH},
            json={
                "channel":     channel,
                "agent":       agent,
                "title":       title,
                "description": body,
                "color":       color,
                "fields":      fields,
                "timestamp":   True,
            },
            timeout=4,
        )
    except Exception as e:
        logger.warning("notification_router: Discord delivery failed: %s", e)


def _post_ahmed(title: str, body: str, agent: str, ticker: Optional[str] = None) -> None:
    """
    Page Ahmed via Telegram DM.
    ONLY called for tier=ESCALATE. Never called for INFO or WARN.
    """
    if not _TELEGRAM_BOT_TOKEN:
        logger.warning("notification_router: TELEGRAM_BOT_TOKEN not set — cannot page Ahmed")
        return
    ticker_line = f"Ticker: {ticker}\n" if ticker else ""
    text = (
        f"🚨 {title}\n"
        f"{ticker_line}"
        f"Agent: {agent.upper()}\n"
        f"{body}"
    )
    try:
        _requests.post(
            f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": _AHMED_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logger.warning("notification_router: Ahmed Telegram delivery failed: %s", e)


# ── Public API ────────────────────────────────────────────────────────────────

def notify(
    agent: str,
    tier: NotifyTier,
    title: str,
    body: str,
    ticker: Optional[str] = None,
) -> None:
    """
    Route a notification by tier.

    Args:
        agent:  Source agent name (e.g. "alpha-execution", "omni").
        tier:   NotifyTier.INFO | WARN | ESCALATE.
        title:  Short one-line summary.
        body:   Full message body (markdown OK for Discord).
        ticker: Optional ticker symbol for context fields.

    Delivery matrix:
        INFO     → SOVEREIGN bus + agent Discord channel
        WARN     → SOVEREIGN bus + #escalations Discord channel
        ESCALATE → SOVEREIGN bus + #escalations Discord channel + Ahmed DM
    """
    tier_str = tier.value if isinstance(tier, NotifyTier) else str(tier)
    color    = _TIER_COLORS.get(tier_str, "gray")

    # Always: SOVEREIGN bus
    _post_bus(agent, title, body, tier_str)

    if tier_str == "INFO":
        # INFO → agent's home Discord channel only
        channel = _DISCORD_AGENT_CHANNELS.get(agent, "#daily-health-report")
        _post_discord(channel, agent, title, body, color, ticker)

    elif tier_str == "WARN":
        # WARN → #escalations (more visible, but not Ahmed's phone)
        _post_discord(_DISCORD_ESCALATION_CHANNEL, agent, title, body, color, ticker)

    elif tier_str == "ESCALATE":
        # ESCALATE → #escalations + Ahmed DM
        _post_discord(_DISCORD_ESCALATION_CHANNEL, agent, title, body, color, ticker)
        _post_ahmed(title, body, agent, ticker)

    else:
        logger.warning("notification_router: unknown tier '%s' — defaulting to WARN routing", tier_str)
        _post_discord(_DISCORD_ESCALATION_CHANNEL, agent, title, body, "gray", ticker)


# ── Convenience wrappers ──────────────────────────────────────────────────────

def notify_info(agent: str, title: str, body: str, ticker: Optional[str] = None) -> None:
    notify(agent, NotifyTier.INFO, title, body, ticker)


def notify_warn(agent: str, title: str, body: str, ticker: Optional[str] = None) -> None:
    notify(agent, NotifyTier.WARN, title, body, ticker)


def notify_escalate(agent: str, title: str, body: str, ticker: Optional[str] = None) -> None:
    notify(agent, NotifyTier.ESCALATE, title, body, ticker)
