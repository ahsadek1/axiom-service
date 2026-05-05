"""
alert_queue.py — Nexus Unified Alert Queue Writer
==================================================
Import this in any Nexus service to publish alerts to the unified queue.
The alert_monitor.py cron picks these up every 2 minutes and routes to agents.

Usage:
    from shared.alert_queue import publish_alert, Severity
    
    publish_alert(
        service="alpha-execution",
        severity=Severity.CRITICAL,
        title="NVDA: option mid_price unavailable, INV-14 abort",
        details="Alpaca options/snapshots returned empty for NVDA260620P00900000",
        auto_action="none"
    )
"""

import json
import datetime
import os
from enum import Enum
from pathlib import Path

QUEUE_PATH = os.environ.get(
    "NEXUS_ALERT_QUEUE",
    "/Users/ahmedsadek/nexus/data/alert_queue.jsonl"
)


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"


def publish_alert(
    service: str,
    severity: str,          # use Severity enum or plain string
    title: str,
    details: str = "",
    auto_action: str = "none",
) -> bool:
    """
    Append an alert to the unified queue.
    Thread-safe via append mode (O_APPEND is atomic on POSIX for small writes).
    Never raises — fire and forget.
    """
    entry = {
        "ts":          datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "service":     service,
        "severity":    str(severity).upper(),
        "title":       title,
        "details":     details,
        "auto_action": auto_action,
    }
    try:
        Path(QUEUE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(QUEUE_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return True
    except Exception:
        return False
