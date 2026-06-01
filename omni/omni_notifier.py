"""
omni_notifier.py — OMNI Fire-and-Forget Notification Queue
===========================================================
Telegram and Chronicle writes moved OFF the synthesis hot path.
The synthesis worker queues notifications and returns immediately.
A background thread drains the queue — never blocking synthesis.

Before: synthesis worker → send Telegram → wait → release
After:  synthesis worker → queue message → release immediately
        background thread → drain queue → send Telegram

Ahmed directive May 2026: Telegram rate limiting was killing OMNI workers.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from typing import Any, Optional

import requests

log = logging.getLogger("omni.notifier")

# ---------------------------------------------------------------------------
# Notification queue
# ---------------------------------------------------------------------------

_notify_queue: queue.Queue = queue.Queue(maxsize=500)
_chronicle_queue: queue.Queue = queue.Queue(maxsize=500)
_started = False
_start_lock = threading.Lock()


def start_background_notifier(
    bot_token: str,
    ahmed_chat_id: str,
    chronicle_db: str,
) -> None:
    """Start background notification threads. Call once at OMNI startup."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    # Telegram drain thread
    t1 = threading.Thread(
        target=_telegram_drain_loop,
        args=(bot_token, ahmed_chat_id),
        daemon=True,
        name="omni-telegram-notifier",
    )
    t1.start()

    # Chronicle drain thread
    t2 = threading.Thread(
        target=_chronicle_drain_loop,
        args=(chronicle_db,),
        daemon=True,
        name="omni-chronicle-writer",
    )
    t2.start()

    log.info("Background notifier started — Telegram + Chronicle queues active")


# ---------------------------------------------------------------------------
# Public API — called from synthesis hot path
# ---------------------------------------------------------------------------

def queue_telegram(text: str, chat_id: Optional[str] = None) -> None:
    """
    Queue a Telegram message. Returns immediately — never blocks.
    If queue is full, drops the message (notification loss > synthesis block).
    """
    try:
        _notify_queue.put_nowait({"text": text, "chat_id": chat_id})
    except queue.Full:
        log.warning("Telegram queue full — dropping notification (synthesis priority)")


def queue_chronicle_write(
    synthesis_id: str,
    ticker: str,
    system: str,
    verdict: str,
    trade_date: str,
) -> None:
    """
    Queue a Chronicle write. Returns immediately — never blocks.
    """
    try:
        _chronicle_queue.put_nowait({
            "synthesis_id": synthesis_id,
            "ticker":       ticker,
            "system":       system,
            "verdict":      verdict,
            "trade_date":   trade_date,
            "ts":           time.time(),
        })
    except queue.Full:
        log.warning("Chronicle queue full — dropping write (synthesis priority)")


# ---------------------------------------------------------------------------
# Background drain loops
# ---------------------------------------------------------------------------

def _telegram_drain_loop(bot_token: str, default_chat_id: str) -> None:
    """Drain Telegram queue. Respects rate limits. Never dies."""
    while True:
        try:
            item = _notify_queue.get(timeout=5)
            text    = item.get("text", "")
            chat_id = item.get("chat_id") or default_chat_id

            if not text or not chat_id or not bot_token:
                continue

            try:
                requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    timeout=10,
                )
            except Exception as exc:
                log.warning("Telegram send failed (queued): %s", exc)

            # Rate limit: max 20 messages/second to Telegram
            time.sleep(0.1)

        except queue.Empty:
            continue
        except Exception as exc:
            log.error("Telegram drain error: %s", exc)
            time.sleep(1)


def _chronicle_drain_loop(chronicle_db: str) -> None:
    """Drain Chronicle write queue. Batches writes. Never dies."""
    while True:
        try:
            item = _chronicle_queue.get(timeout=10)

            try:
                conn = sqlite3.connect(chronicle_db, timeout=5)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS synthesis_completed "
                    "(id INTEGER PRIMARY KEY AUTOINCREMENT, synthesis_id TEXT, "
                    "ticker TEXT, system TEXT, verdict TEXT, trade_date TEXT, ts REAL)"
                )
                conn.execute(
                    "INSERT INTO synthesis_completed "
                    "(synthesis_id, ticker, system, verdict, trade_date, ts) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        item["synthesis_id"], item["ticker"],
                        item["system"], item["verdict"],
                        item["trade_date"], item["ts"],
                    ),
                )
                conn.commit()
                conn.close()
            except Exception as exc:
                log.warning("Chronicle write failed (queued): %s", exc)

        except queue.Empty:
            continue
        except Exception as exc:
            log.error("Chronicle drain error: %s", exc)
            time.sleep(1)


# ---------------------------------------------------------------------------
# Queue stats
# ---------------------------------------------------------------------------

def queue_stats() -> dict:
    return {
        "telegram_queue_depth": _notify_queue.qsize(),
        "chronicle_queue_depth": _chronicle_queue.qsize(),
        "notifier_started": _started,
    }
