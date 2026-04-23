"""
sovereign_comms.py — Bidirectional SOVEREIGN Communication Module

Drop-in shared module for every agent on 192.168.1.141.
Provides two methods:
  - report()           : fire-and-forget push to SOVEREIGN via message bus
  - get_instructions() : poll SOVEREIGN's outbox with watermark deduplication

All methods are safe to call at any time — they never raise.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (env vars with hardcoded fallbacks)
# ---------------------------------------------------------------------------

SOVEREIGN_BUS_URL: str = os.getenv(
    "SOVEREIGN_BUS_URL", "http://192.168.1.141:9999"
)
GENESIS_BOT_TOKEN: str = os.getenv(
    "GENESIS_BOT_TOKEN",
    "7973500599:AAHTfCRmjGMoW3pEayGSpDfb84D44M2K3us",
)
SOVEREIGN_TELEGRAM_FALLBACK_CHAT_ID: str = os.getenv(
    "SOVEREIGN_TELEGRAM_FALLBACK_CHAT_ID", "8573754783"
)
NEXUS_GROUP_CHAT_ID: str = os.getenv(
    "NEXUS_GROUP_CHAT_ID", "-1003579956463"
)

_EPOCH_TS: str = "1970-01-01T00:00:00+00:00"
_RETRY_DELAYS: List[float] = [0.5, 1.0, 2.0]
_HTTP_TIMEOUT: float = 3.0

# ---------------------------------------------------------------------------
# Escalation Matrix
# ---------------------------------------------------------------------------
# Keys are message_type values that trigger special routing.
# bus_retries: how many POST attempts before giving up on the bus.
# telegram_ahmed: DM Ahmed directly via bot.
# telegram_group: Post to the NEXUS group chat.
# bus: whether to attempt the message bus at all.
# ---------------------------------------------------------------------------

ESCALATION_MATRIX: Dict[str, Dict] = {
    "CRITICAL": {
        "bus": True,
        "telegram_ahmed": True,
        "telegram_group": True,
        "bus_retries": 5,
    },
    "INFO": {
        "bus": True,
        "telegram_ahmed": False,
        "telegram_group": False,
        "bus_retries": 3,
    },
    "EOD": {
        "bus": True,
        "telegram_ahmed": True,
        "telegram_group": True,
        "bus_retries": 3,
    },
    "AUTONOMOUS": {
        "bus": True,
        "telegram_ahmed": False,
        "telegram_group": False,
        "bus_retries": 3,
    },
    "AHMED_DIRECT": {
        "bus": False,
        "telegram_ahmed": True,
        "telegram_group": False,
        "bus_retries": 0,
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialize_payload(payload: Any) -> str:
    """
    Attempt JSON serialisation of payload.
    Falls back to str() on any TypeError.

    :param payload: Arbitrary value to serialise.
    :return: JSON string or str() representation.
    """
    try:
        return json.dumps(payload)
    except (TypeError, ValueError) as exc:
        logger.error(
            "sovereign_comms: payload not JSON-serialisable (%s) — "
            "falling back to str()",
            exc,
        )
        return str(payload)


def _post_to_bus(agent_name: str, message_type: str, payload: Any, max_retries: int = 3) -> bool:
    """
    Attempt to POST a message to the SOVEREIGN bus with configurable retries.

    :param agent_name: Sending agent name.
    :param message_type: Category string (e.g. "alert", "status").
    :param payload: Message body (dict or str).
    :param max_retries: Maximum number of POST attempts (default 3).
    :return: True if the POST succeeded, False after all retries exhausted.
    """
    body = {
        "from": agent_name,
        "to": "sovereign",
        "message": f"{message_type}: {_serialize_payload(payload)}",
    }
    url = f"{SOVEREIGN_BUS_URL}/send"
    delays = [0.5, 1.0, 2.0, 4.0, 8.0][:max_retries]

    for attempt, delay in enumerate(delays, start=1):
        try:
            resp = requests.post(url, json=body, timeout=_HTTP_TIMEOUT)
            if resp.status_code < 300:
                return True
            logger.warning(
                "sovereign_comms: bus POST attempt %d returned HTTP %d",
                attempt,
                resp.status_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sovereign_comms: bus POST attempt %d failed: %s",
                attempt,
                exc,
            )
        if attempt < len(delays):
            time.sleep(delay)

    return False


def _post_telegram(chat_id: str, text: str) -> bool:
    """
    Send a Telegram message to the given chat_id via the GENESIS bot.

    :param chat_id: Telegram chat ID (user or group).
    :param text: Message text.
    :return: True on success, False on failure.
    """
    url = f"https://api.telegram.org/bot{GENESIS_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=_HTTP_TIMEOUT,
        )
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.error("sovereign_comms: Telegram send to %s failed: %s", chat_id, exc)
        return False


def _post_telegram_fallback(agent_name: str, message_type: str, payload: Any) -> bool:
    """
    Send a Telegram message to Ahmed as a last-resort fallback.

    :param agent_name: Sending agent name.
    :param message_type: Category string.
    :param payload: Message body.
    :return: True on success, False on failure.
    """
    text = (
        f"[SOVEREIGN BUS DOWN] {agent_name.upper()} → {message_type}: "
        f"{_serialize_payload(payload)}"
    )
    return _post_telegram(SOVEREIGN_TELEGRAM_FALLBACK_CHAT_ID, text)


def _report_worker(agent_name: str, message_type: str, payload: Any) -> None:
    """
    Daemon-thread worker for report().
    Routes via escalation matrix when message_type matches a known tier.
    Falls back to standard bus+Telegram path for unknown types.

    :param agent_name: Sending agent name.
    :param message_type: Category string or escalation tier.
    :param payload: Message body.
    """
    try:
        matrix = ESCALATION_MATRIX.get(message_type.upper())
        serialized = _serialize_payload(payload)

        if matrix is not None:
            # ── Escalation-matrix routing ──────────────────────────────────
            bus_ok = False
            if matrix["bus"]:
                bus_ok = _post_to_bus(
                    agent_name, message_type, payload,
                    max_retries=matrix["bus_retries"],
                )
                if not bus_ok:
                    logger.warning(
                        "sovereign_comms: bus failed for %s/%s after %d retries",
                        agent_name, message_type, matrix["bus_retries"],
                    )

            if matrix["telegram_ahmed"]:
                prefix = "" if bus_ok else "[BUS DOWN] "
                ahmed_text = (
                    f"{prefix}[{message_type}] {agent_name.upper()}: {serialized}"
                )
                _post_telegram(SOVEREIGN_TELEGRAM_FALLBACK_CHAT_ID, ahmed_text)

            if matrix["telegram_group"]:
                group_text = (
                    f"🔔 [{message_type}] {agent_name.upper()}: {serialized}"
                )
                _post_telegram(NEXUS_GROUP_CHAT_ID, group_text)

            if not matrix["bus"] and not matrix["telegram_ahmed"] and not matrix["telegram_group"]:
                logger.error(
                    "sovereign_comms: escalation matrix entry for %s has no delivery path — "
                    "message discarded",
                    message_type,
                )
        else:
            # ── Standard routing (bus + Telegram fallback) ─────────────────
            if _post_to_bus(agent_name, message_type, payload):
                return

            logger.warning(
                "sovereign_comms: bus unreachable after 3 retries — "
                "attempting Telegram fallback"
            )
            if _post_telegram_fallback(agent_name, message_type, payload):
                return

            logger.error(
                "sovereign_comms: all delivery paths failed for %s/%s — "
                "message discarded",
                agent_name,
                message_type,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "sovereign_comms: unhandled exception in report worker: %s — "
            "message discarded",
            exc,
        )


# ---------------------------------------------------------------------------
# Watermark helpers
# ---------------------------------------------------------------------------


def _watermark_path(agent_name: str) -> Path:
    """
    Resolve watermark file path for an agent.

    :param agent_name: Agent name (e.g. "cipher").
    :return: Path object for the watermark file.
    """
    env_key = f"NEXUS_{agent_name.upper()}_DIR"
    workdir = os.getenv(env_key, f"/Users/ahmedsadek/nexus/{agent_name}/")
    return Path(workdir) / ".sovereign_watermark"


def _read_watermark(path: Path) -> datetime:
    """
    Read watermark timestamp from file.
    Returns epoch on missing or corrupt file.

    :param path: Path to watermark file.
    :return: datetime with UTC timezone.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if not path.exists():
        return epoch
    try:
        raw = path.read_text().strip()
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sovereign_comms: corrupt watermark at %s (%s) — resetting to epoch",
            path,
            exc,
        )
        return epoch


def _write_watermark(path: Path, ts: datetime) -> None:
    """
    Write watermark timestamp to file.
    Logs a warning and continues if the file is unwritable.

    :param path: Path to watermark file.
    :param ts: Timestamp to persist.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ts.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sovereign_comms: cannot write watermark to %s (%s) — "
            "watermark held in memory only",
            path,
            exc,
        )


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp string into a timezone-aware datetime.

    :param ts_str: Timestamp string.
    :return: datetime or None on parse failure.
    """
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def report(agent_name: str, message_type: str, payload: Dict[str, Any]) -> None:
    """
    Fire-and-forget push to SOVEREIGN via the local message bus.

    Spawns a daemon thread so the caller is never blocked.
    Retries up to 3 times on bus failure, then falls back to Telegram.
    Never raises under any circumstances.

    :param agent_name: Sending agent name (e.g. "cipher", "atlas").
    :param message_type: Message category (e.g. "alert", "status", "escalation").
    :param payload: Arbitrary JSON-serialisable dict. Non-serialisable values
                    are converted to str() before sending.
    """
    t = threading.Thread(
        target=_report_worker,
        args=(agent_name, message_type, payload),
        daemon=True,
    )
    t.start()


def get_instructions(agent_name: str) -> List[Dict[str, str]]:
    """
    Poll SOVEREIGN's outbox for new instructions, deduplicated via watermark.

    Reads GET {bus}/inbox/{agent_name}, filters messages newer than the last
    processed timestamp, updates the watermark, and returns the new messages.

    Never raises under any circumstances.

    :param agent_name: Agent polling its own inbox (e.g. "cipher").
    :return: List of unprocessed instruction dicts, each containing:
             {"id": str, "from": str, "message": str, "timestamp": str}.
             Returns [] on any failure.
    """
    url = f"{SOVEREIGN_BUS_URL}/inbox/{agent_name}"
    wm_path = _watermark_path(agent_name)
    watermark = _read_watermark(wm_path)

    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT)
        if resp.status_code >= 300:
            logger.warning(
                "sovereign_comms: GET inbox returned HTTP %d for agent %s",
                resp.status_code,
                agent_name,
            )
            return []
        data = resp.json()
        messages: List[Dict] = data.get("messages", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sovereign_comms: get_instructions failed for %s: %s",
            agent_name,
            exc,
        )
        return []

    new_messages: List[Dict[str, str]] = []
    latest_ts: datetime = watermark

    for msg in messages:
        ts_raw = msg.get("timestamp")
        if ts_raw is None:
            logger.warning(
                "sovereign_comms: skipping malformed message (no timestamp): %s",
                msg,
            )
            continue
        ts = _parse_ts(ts_raw)
        if ts is None:
            logger.warning(
                "sovereign_comms: skipping message with unparseable timestamp: %s",
                ts_raw,
            )
            continue
        if ts > watermark:
            new_messages.append(
                {
                    "id": str(msg.get("id", "")),
                    "from": str(msg.get("from", "")),
                    "message": str(msg.get("message", "")),
                    "timestamp": ts_raw,
                }
            )
            if ts > latest_ts:
                latest_ts = ts

    if new_messages:
        _write_watermark(wm_path, latest_ts)

    return new_messages
