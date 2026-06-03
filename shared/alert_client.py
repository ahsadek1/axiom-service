"""
alert_client.py — Drop-in Alert Broker client for Nexus services.

Usage:
    from alert_client import send_alert

    send_alert(
        source="nexus-integrity",
        level="CRITICAL",
        title="OMNI silence 25min",
        body="No synthesis since 10:42 AM",
        dedup_key="omni_silence",
        targets=["ahmed", "nexus_health_group"],
    )

Routing:
    - Posts to Alert Broker at localhost:9998
    - If broker unreachable: falls back to direct Telegram (CRITICAL only)
    - Never raises under any circumstances

Install: drop this file into nexus/shared/ — no other changes needed.
"""

import json
import logging
import os
import threading
import time
from typing import List, Optional

import requests

logger = logging.getLogger("alert_client")

BROKER_URL: str = os.getenv("ALERT_BROKER_URL", "http://localhost:9998")
BROKER_SECRET: str = os.getenv(
    "ALERT_BROKER_SECRET",
    "ab_secret_f4e2d1c8b7a3e9f5d2c4b6a8e0f3d5c7b9a1e4f6d8c0b2a4e6f8d0c2b4a6e8"
)

# Fallback Telegram (used only when broker is unreachable and level=CRITICAL)
_FB_TOKEN: str = os.getenv("GENESIS_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
_FB_CHAT_ID: str = os.getenv("AHMED_CHAT_ID", "8573754783")

_HTTP_TIMEOUT: float = 3.0


def _fallback_telegram(source: str, level: str, title: str, body: str) -> None:
    """Direct Telegram to Ahmed — only called when broker is unreachable for CRITICAL alerts."""
    try:
        text = f"🚨 [CRITICAL — BROKER DOWN] [{source}] {title}"
        if body:
            text += f"\n{body}"
        requests.post(
            f"https://api.telegram.org/bot{_FB_TOKEN}/sendMessage",
            json={"chat_id": _FB_CHAT_ID, "text": text},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception as exc:
        logger.error("alert_client: fallback Telegram failed: %s", exc)


def _send_worker(
    source: str,
    level: str,
    title: str,
    body: str,
    dedup_key: str,
    targets: List[str],
) -> None:
    """Background thread worker — posts to broker, falls back on failure."""
    try:
        resp = requests.post(
            f"{BROKER_URL}/alert",
            json={
                "source": source,
                "level": level,
                "title": title,
                "body": body,
                "dedup_key": dedup_key,
                "targets": targets,
            },
            headers={"X-Alert-Secret": BROKER_SECRET},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code < 300:
            return
        logger.warning(
            "alert_client: broker returned HTTP %d for '%s' — %s",
            resp.status_code, dedup_key, level,
        )
    except Exception as exc:
        logger.warning("alert_client: broker unreachable: %s", exc)

    # Fallback: only for CRITICAL
    if level == "CRITICAL":
        _fallback_telegram(source, level, title, body)


def send_alert(
    source: str,
    level: str,
    title: str,
    body: str = "",
    dedup_key: Optional[str] = None,
    targets: Optional[List[str]] = None,
) -> None:
    """
    Send an alert via the Alert Broker (fire-and-forget daemon thread).

    :param source:    Sending service name (e.g. "nexus-integrity", "omni").
    :param level:     "CRITICAL" | "WARNING" | "INFO"
    :param title:     One-line summary.
    :param body:      Optional detail text.
    :param dedup_key: Deduplication key. Defaults to "{source}:{title}".
    :param targets:   Delivery targets. Defaults to ["ahmed"].
                      Options: "ahmed" | "nexus_health_group" | "sqs_group" | "sovereign"

    Never raises under any circumstances.
    """
    try:
        key = dedup_key or f"{source}:{title}"
        tgts = targets or ["ahmed"]
        t = threading.Thread(
            target=_send_worker,
            args=(source, level, title, body, key, tgts),
            daemon=True,
        )
        t.start()
    except Exception as exc:
        logger.error("alert_client: send_alert failed to start thread: %s", exc)
