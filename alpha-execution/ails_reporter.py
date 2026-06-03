"""
ails_reporter.py — Post completed trade outcomes to AILS.

OMNI H-NEW-1 fix: outcomes that fail to post are queued in a local SQLite table
and retried by a background thread every 5 minutes (up to 3 attempts). After
3 failures Ahmed is alerted via Telegram. This prevents Bayesian win rate
inflation from survivor bias when AILS is temporarily unreachable.

Original fire-and-forget design is preserved for the happy path — no blocking.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict

import requests

log = logging.getLogger(__name__)

AILS_URL:     str = os.environ.get("AILS_URL",     "http://localhost:8008")
AILS_SECRET:  str = os.environ.get("AILS_SECRET",  "")
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AHMED_CHAT_ID: str = os.environ.get("AHMED_CHAT_ID", "")

_TIMEOUT_S:       int = 5
_MAX_ATTEMPTS:    int = 3
_RETRY_INTERVAL:  int = 300  # 5 minutes

# Local outcome queue DB — one per execution service
_QUEUE_DB_PATH: str = os.environ.get(
    "AILS_QUEUE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "ails_outcome_queue.db"),
)
_queue_lock  = threading.Lock()
_retry_started = False


def _init_queue_db() -> None:
    """Create the local outcome queue table if it doesn't exist."""
    os.makedirs(os.path.dirname(_QUEUE_DB_PATH), exist_ok=True)
    with sqlite3.connect(_QUEUE_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outcome_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                payload     TEXT    NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                queued_at   TEXT    NOT NULL,
                last_tried  TEXT
            )
        """)
        conn.commit()


def _enqueue_outcome(payload: dict) -> None:
    """Write a failed outcome to the local retry queue."""
    try:
        _init_queue_db()
        with sqlite3.connect(_QUEUE_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO outcome_queue (payload, queued_at) VALUES (?, ?)",
                (json.dumps(payload), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        log.info("AILS outcome queued for retry: %s", payload.get("ticker"))
    except Exception as e:
        log.error("Failed to enqueue AILS outcome for %s: %s", payload.get("ticker"), e)


def _post_to_ails(payload: dict) -> bool:
    """Attempt a single AILS post. Returns True on success."""
    try:
        resp = requests.post(
            f"{AILS_URL}/outcome",
            json=payload,
            headers={"X-Ails-Secret": AILS_SECRET},
            timeout=_TIMEOUT_S,
        )
        if resp.ok:
            log.info("AILS outcome posted: %s win=%s pnl=%.2f",
                     payload.get("ticker"), payload.get("win"), payload.get("pnl"))
            return True
        log.warning("AILS outcome post failed %d for %s", resp.status_code, payload.get("ticker"))
        return False
    except requests.RequestException as exc:
        log.warning("AILS outcome post error for %s: %s", payload.get("ticker"), exc)
        return False


def _send_lost_outcome_alert(ticker: str, attempts: int) -> None:
    """Alert Ahmed when an outcome exhausts all retry attempts."""
    if not TELEGRAM_BOT_TOKEN or not AHMED_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": AHMED_CHAT_ID,
                "text": (
                    f"⚠️ <b>AILS OUTCOME PERMANENTLY LOST</b>\n"
                    f"Ticker: {ticker}\n"
                    f"Failed after {attempts} attempts — Bayesian learning update dropped.\n"
                    f"Check AILS service health."
                ),
                "parse_mode": "HTML",
            },
            timeout=5,
        )
    except Exception:
        pass


def _retry_worker() -> None:
    """Background thread: retry queued outcomes every 5 minutes."""
    while True:
        time.sleep(_RETRY_INTERVAL)
        try:
            _init_queue_db()
            with _queue_lock:
                with sqlite3.connect(_QUEUE_DB_PATH) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT * FROM outcome_queue WHERE attempts < ? ORDER BY queued_at ASC",
                        (_MAX_ATTEMPTS,),
                    ).fetchall()

                for row in rows:
                    payload = json.loads(row["payload"])
                    now = datetime.now(timezone.utc).isoformat()
                    ok = _post_to_ails(payload)
                    with sqlite3.connect(_QUEUE_DB_PATH) as conn:
                        if ok:
                            conn.execute("DELETE FROM outcome_queue WHERE id=?", (row["id"],))
                            log.info("AILS retry succeeded for %s (attempt %d)",
                                     payload.get("ticker"), row["attempts"] + 1)
                        else:
                            new_attempts = row["attempts"] + 1
                            conn.execute(
                                "UPDATE outcome_queue SET attempts=?, last_tried=? WHERE id=?",
                                (new_attempts, now, row["id"]),
                            )
                            if new_attempts >= _MAX_ATTEMPTS:
                                log.error(
                                    "AILS outcome permanently lost for %s after %d attempts",
                                    payload.get("ticker"), new_attempts,
                                )
                                _send_lost_outcome_alert(payload.get("ticker", "?"), new_attempts)
                        conn.commit()
        except Exception as e:
            log.error("AILS retry worker error: %s", e)


def start_retry_worker() -> None:
    """Start the background retry thread (idempotent — safe to call multiple times)."""
    global _retry_started
    if _retry_started:
        return
    _retry_started = True
    _init_queue_db()
    t = threading.Thread(target=_retry_worker, daemon=True, name="ails-retry-worker")
    t.start()
    log.info("AILS outcome retry worker started (interval=%ds, max_attempts=%d)",
             _RETRY_INTERVAL, _MAX_ATTEMPTS)


def post_outcome(
    ticker: str,
    strategy: str,
    regime: str,
    direction: str,
    pnl_usd: float,
    win: bool,
    system: str = "alpha",
    concordance_path: Optional[str] = None,
    agent_votes: Optional[Dict[str, bool]] = None,
) -> None:
    """
    POST a completed trade outcome to AILS for Bayesian learning.

    Non-blocking — never raises, never holds up execution flow.
    On failure, queues the outcome for background retry (OMNI H-NEW-1 fix).

    Args:
        ticker:           Ticker symbol
        strategy:         Strategy type (e.g. 'bull_put_spread')
        regime:           Market regime at entry (e.g. 'NORMAL')
        direction:        Trade direction ('bullish', 'bearish', 'neutral')
        pnl_usd:          Realized P&L in dollars
        win:              True if trade was profitable
        system:           'alpha' or 'prime'
        concordance_path: Concordance pathway (P1, P2, P3, P4)
        agent_votes:      Agent vote map {agent_name: voted_go}
    """
    if not AILS_SECRET:
        log.debug("AILS_SECRET not set — skipping outcome post for %s", ticker)
        return

    payload = {
        "ticker":           ticker,
        "strategy":         strategy,
        "regime":           regime,
        "direction":        direction,
        "pnl":              round(pnl_usd, 2),
        "win":              win,
        "system":           system,
        "concordance_path": concordance_path,
        "agent_votes":      agent_votes,
    }

    ok = _post_to_ails(payload)
    if not ok:
        _enqueue_outcome(payload)
