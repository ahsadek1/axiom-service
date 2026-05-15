#!/usr/bin/env python3
"""
genesis_inbox_poller.py — GENESIS Message Bus Inbox Poller
===========================================================
Runs as a persistent background daemon.
Polls /inbox/genesis on the Nexus message bus every 60 seconds.
When messages arrive, processes them immediately via genesis_diagnostic logic.

This closes the cross-agent alert visibility gap:
  ANY agent can POST to the bus with to="genesis" and GENESIS will receive and act.

Run via LaunchAgent: ai.nexus.genesis-inbox-poller.plist
Or directly: python3 /Users/ahmedsadek/nexus/genesis/genesis_inbox_poller.py

Author: GENESIS | 2026-05-05
"""

from __future__ import annotations

import logging
import os
import sys
import time

import requests

sys.path.insert(0, "/Users/ahmedsadek/nexus")
from shared.alert_manager import write_alert, is_known, ack_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] genesis.inbox: %(message)s",
)
logger = logging.getLogger("genesis.inbox")

BUS_URL = os.getenv("NEXUS_BUS_URL", "http://192.168.1.141:9999")
POLL_INTERVAL_S = 60
HTTP_TIMEOUT = 5


def poll_and_process() -> int:
    """Poll inbox, process messages. Returns count of messages processed."""
    try:
        r = requests.get(f"{BUS_URL}/inbox/genesis", timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return 0
        data = r.json()
        messages = data if isinstance(data, list) else data.get("messages", [])
        if not messages:
            return 0

        logger.info("Received %d message(s) from bus", len(messages))
        for msg in messages:
            _handle(msg)
        return len(messages)

    except Exception as exc:
        logger.debug("Bus poll error (non-fatal): %s", exc)
        return 0


def _handle(msg: dict) -> None:
    """
    Process a single bus message.
    Writes to agent_alerts if it looks like an alert.
    Logs the message regardless.
    """
    from_agent = msg.get("from_agent", msg.get("from", "unknown"))
    message_text = msg.get("message", "")
    msg_id = msg.get("id", "?")

    logger.info("[MSG %s] from=%s: %s", msg_id, from_agent, message_text[:200])

    msg_upper = message_text.upper()
    is_alert = any(kw in msg_upper for kw in (
        "CRITICAL", "ERROR", "ALERT", "FAILURE", "PAUSED", "UNREACHABLE",
        "SILENCE", "STOPPED", "CRASH", "DOWN", "BREACH",
    ))

    if is_alert:
        severity = "CRITICAL" if "CRITICAL" in msg_upper else "WARNING"
        # Use a stable issue_key from sender + first 40 chars of message
        key_text = message_text[:40].lower().replace(" ", "_").replace(":", "_")
        issue_key = f"bus:{from_agent}:{key_text}"

        if not is_known(issue_key):
            alert_id = write_alert(
                agent=from_agent,
                severity=severity,
                service=from_agent,
                issue_key=issue_key,
                title=f"[Bus/{from_agent}] {message_text[:80]}",
                details=message_text[:500],
            )
            if alert_id > 0:
                ack_alert(issue_key, "genesis-inbox-poller")
                logger.warning(
                    "New alert from bus: id=%d from=%s key=%s",
                    alert_id, from_agent, issue_key,
                )
        else:
            logger.info("Bus alert already known: %s", issue_key)


def run_forever() -> None:
    """Main daemon loop."""
    logger.info("GENESIS inbox poller started — polling %s every %ds", BUS_URL, POLL_INTERVAL_S)
    while True:
        try:
            count = poll_and_process()
            if count:
                logger.info("Processed %d bus message(s)", count)
        except Exception as exc:
            logger.error("Unexpected error in poll loop: %s", exc)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    run_forever()
