"""
discord_client.py — Shared helper for Nexus agents to push embeds to Discord.

Fire-and-forget: never raises, never blocks. Returns True on success, False on failure.

Usage:
    from shared.discord_client import post_to_discord

    post_to_discord(
        channel="go-verdicts",
        title="GO VERDICT — AAPL",
        description="P1 Concordance. Score 74.",
        color="green",
        fields=[{"name": "Ticker", "value": "AAPL", "inline": True}],
        footer="OMNI Quad Intelligence",
        agent="omni",
    )
"""
import json
import logging
import os
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

# Service config — read from env or use localhost default
BRIDGE_URL = os.getenv("DISCORD_BRIDGE_URL", "http://localhost:8011")
BRIDGE_SECRET = os.getenv("DISCORD_BRIDGE_SECRET", "")
BRIDGE_TIMEOUT_S = int(os.getenv("DISCORD_BRIDGE_TIMEOUT_S", "3"))


def post_to_discord(
    channel: str,
    title: str,
    description: str = "",
    color: str = "blue",
    fields: Optional[List[dict]] = None,
    footer: str = "",
    agent: str = "system",
    timestamp: bool = True,
    urgent: bool = False,
) -> bool:
    """
    Post a rich embed to a Discord channel via the discord-bridge service.

    Args:
        channel:     Target channel name (without #), e.g. "go-verdicts"
        title:       Embed title, max 256 chars
        description: Embed body text, max 4096 chars
        color:       Semantic color: green|red|yellow|blue|purple|orange|gray|gold|teal|indigo
        fields:      Optional list of {"name": str, "value": str, "inline": bool}
        footer:      Optional footer text
        agent:       Name of the calling agent (shown as embed author)
        timestamp:   Whether to include current UTC timestamp in embed
        urgent:      If True, prepends @here to the message

    Returns:
        True if the bridge accepted the payload (202), False on any failure.

    Never raises. All exceptions are caught and logged at WARNING level.
    """
    secret = BRIDGE_SECRET
    if not secret:
        logger.warning(
            "discord_client: DISCORD_BRIDGE_SECRET not set — skipping Discord push"
        )
        return False

    payload = {
        "channel": channel,
        "agent": agent,
        "title": title,
        "color": color,
        "timestamp": timestamp,
        "urgent": urgent,
    }
    if description:
        payload["description"] = description
    if fields:
        payload["fields"] = fields
    if footer:
        payload["footer"] = footer

    try:
        resp = requests.post(
            f"{BRIDGE_URL}/send",
            json=payload,
            headers={"X-Discord-Bridge-Secret": secret},
            timeout=BRIDGE_TIMEOUT_S,
        )
        if resp.status_code == 202:
            return True
        logger.warning(
            f"discord_client: bridge rejected payload — {resp.status_code} {resp.text[:200]}"
        )
        return False
    except requests.exceptions.ConnectionError:
        logger.warning(
            f"discord_client: discord-bridge not reachable at {BRIDGE_URL} — skipping"
        )
        return False
    except requests.exceptions.Timeout:
        logger.warning(
            f"discord_client: discord-bridge timed out after {BRIDGE_TIMEOUT_S}s — skipping"
        )
        return False
    except Exception as exc:
        logger.warning(f"discord_client: unexpected error — {exc}")
        return False


def post_approval_request(
    request_id:  str,
    title:       str,
    description: str = "",
    agent:       str = "genesis",
    reply_to:    str = "genesis",
    color:       str = "gold",
    fields:      Optional[List[dict]] = None,
) -> bool:
    """
    Post an approval request to #ahmed-decisions with Approve/Reject buttons.

    Args:
        request_id:  Unique identifier for this request (used to track decision)
        title:       Approval request title, e.g. "SPEC APPROVAL — discord-phase3"
        description: Context/summary shown in the embed body
        agent:       Name of the agent requesting approval
        reply_to:    Agent name to notify when decision is made (via message bus)
        color:       Embed color (default: gold for decisions)
        fields:      Optional structured fields (risk, est. build time, etc.)

    Returns:
        True if the request was posted successfully, False otherwise.
        Never raises.
    """
    secret = BRIDGE_SECRET
    if not secret:
        logger.warning(
            "discord_client: DISCORD_BRIDGE_SECRET not set — skipping approval request"
        )
        return False

    payload = {
        "request_id":  request_id,
        "title":       title,
        "description": description,
        "agent":       agent,
        "reply_to":    reply_to,
        "color":       color,
        "fields":      fields or [],
    }

    try:
        resp = requests.post(
            f"{BRIDGE_URL}/approve-request",
            json=payload,
            headers={"X-Discord-Bridge-Secret": secret},
            timeout=BRIDGE_TIMEOUT_S,
        )
        if resp.status_code == 202:
            return True
        logger.warning(
            f"discord_client: approve-request rejected — {resp.status_code} {resp.text[:200]}"
        )
        return False
    except requests.exceptions.ConnectionError:
        logger.warning(
            f"discord_client: discord-bridge not reachable at {BRIDGE_URL} — skipping"
        )
        return False
    except requests.exceptions.Timeout:
        logger.warning(
            f"discord_client: discord-bridge timed out after {BRIDGE_TIMEOUT_S}s — skipping"
        )
        return False
    except Exception as exc:
        logger.warning(f"discord_client: unexpected error — {exc}")
        return False


def post_go_verdict(
    ticker: str,
    pathway: str,
    score: int,
    direction: str,
    agent_votes: str = "",
    urgent: bool = False,
) -> bool:
    """
    Convenience wrapper: post a GO verdict to #go-verdicts.

    Args:
        ticker:      Ticker symbol, e.g. "AAPL"
        pathway:     Concordance pathway, e.g. "P1", "P2", "P3"
        score:       Concordance score (0-100)
        direction:   "CALL" or "PUT"
        agent_votes: Human-readable agent votes summary
        urgent:      Whether to @here

    Returns:
        True if delivered, False otherwise.
    """
    color = "green" if score >= 70 else "yellow" if score >= 58 else "red"
    fields = [
        {"name": "Ticker", "value": ticker, "inline": True},
        {"name": "Pathway", "value": pathway, "inline": True},
        {"name": "Score", "value": str(score), "inline": True},
        {"name": "Direction", "value": direction, "inline": True},
    ]
    if agent_votes:
        fields.append({"name": "Agent Votes", "value": agent_votes, "inline": False})

    return post_to_discord(
        channel="go-verdicts",
        title=f"GO VERDICT — {ticker}",
        description=f"{pathway} Concordance. Score {score}/100. Direction: {direction}.",
        color=color,
        fields=fields,
        footer="OMNI Quad Intelligence",
        agent="omni",
        urgent=urgent,
    )


def post_health_report(
    title: str,
    summary: str,
    service_statuses: Optional[List[dict]] = None,
    color: str = "blue",
) -> bool:
    """
    Convenience wrapper: post a health report to #daily-health-report.

    Args:
        title:           Report title
        summary:         One-paragraph summary text
        service_statuses: List of {"name": str, "value": str, "inline": bool} for each service
        color:           Embed color (green=all ok, yellow=degraded, red=critical)

    Returns:
        True if delivered, False otherwise.
    """
    return post_to_discord(
        channel="daily-health-report",
        title=title,
        description=summary,
        color=color,
        fields=service_statuses or [],
        footer="SOVEREIGN",
        agent="sovereign",
    )
