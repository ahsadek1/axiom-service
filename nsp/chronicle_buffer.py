"""
chronicle_buffer.py — NSP CHRONICLE audit trail writer with local buffer.

Spec requirements:
  • NSP writes to local SQLite intervention log IMMEDIATELY (synchronous, never fails)
  • Separate async process syncs to CHRONICLE (192.168.1.42:8020) every 60s
  • If .42 unreachable: buffer locally, replay when connectivity restores
  • NEVER block an intervention on a CHRONICLE write

CHRONICLE schema per intervention record:
  failure_class, service, signal_chain, fix_attempt_log,
  duration_to_resolve_s, trading_impact (bool),
  execution_suspended (bool), outcome, actual_root_cause,
  predicted_root_cause, baseline_feedback_signal

Public API:
    write_record(record)          → writes to local buffer immediately (non-blocking)
    start_sync_thread()           → starts background 60s sync to CHRONICLE
    stop_sync_thread()            → graceful stop
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from db import enqueue_write, read_db

logger = logging.getLogger("nsp.chronicle_buffer")

CHRONICLE_URL: str = os.environ.get("CHRONICLE_URL", "http://192.168.1.42:8020")
CHRONICLE_SYNC_INTERVAL_S: int = 60
CHRONICLE_TIMEOUT_S: int = 10

_sync_thread: Optional[threading.Thread] = None
_stop_event: threading.Event = threading.Event()


def write_record(record: Dict[str, Any]) -> None:
    """
    Write an intervention record to the local SQLite buffer immediately.

    The record is serialised to JSON and stored in the payload column.
    The sync thread will push to CHRONICLE asynchronously.
    This call is fire-and-forget and never blocks an intervention.

    Parameters:
        record: Dict conforming to the CHRONICLE intervention schema.
                Required keys: failure_class, service, signal_chain,
                fix_attempt_log, outcome, actual_root_cause.
    """
    # Ensure signal_chain and fix_attempt_log are JSON-serialisable
    payload = dict(record)
    if isinstance(payload.get("signal_chain"), list):
        pass  # already a list
    payload.setdefault("failure_class", "UNKNOWN")
    payload.setdefault("service", "unknown")
    payload.setdefault("outcome", "unknown")
    payload.setdefault("actual_root_cause", "")
    payload.setdefault("predicted_root_cause", "")
    payload.setdefault("baseline_feedback_signal", "")
    payload.setdefault("created_at", time.time())

    sql = "INSERT INTO chronicle_buffer (payload, created_at, synced) VALUES (?, ?, 0)"
    params = (json.dumps(payload), time.time())
    enqueue_write(sql, params)
    logger.debug("Chronicle buffer: record enqueued for service=%s", record.get("service"))


def _get_unsynced_records(limit: int = 100) -> List[Dict]:
    """Fetch up to `limit` unsynced records from the local buffer."""
    rows = read_db(
        "SELECT rowid, payload, created_at FROM chronicle_buffer WHERE synced = 0 ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    results = []
    for row in rows:
        try:
            d = json.loads(row["payload"])
            d["rowid"] = row["rowid"]
            results.append(d)
        except Exception as exc:
            logger.warning("Failed to deserialise chronicle record: %s", exc)
    return results


def _mark_synced(rowids: List[int]) -> None:
    """Mark records as synced in local buffer."""
    if not rowids:
        return
    placeholders = ",".join("?" * len(rowids))
    enqueue_write(
        f"UPDATE chronicle_buffer SET synced = 1 WHERE rowid IN ({placeholders})",
        tuple(rowids),
    )


def _push_to_chronicle(records: List[Dict]) -> List[int]:
    """
    Push records to the remote CHRONICLE service.

    Returns list of rowids successfully pushed.
    """
    synced_rowids: List[int] = []

    for record in records:
        rowid = record.get("rowid")
        try:
            resp = requests.post(
                f"{CHRONICLE_URL}/interventions",
                json=record,
                timeout=CHRONICLE_TIMEOUT_S,
            )
            if resp.status_code in (200, 201):
                if rowid is not None:
                    synced_rowids.append(rowid)
            else:
                logger.warning(
                    "CHRONICLE rejected record (rowid=%s): HTTP %s %s",
                    rowid, resp.status_code, resp.text[:200],
                )
        except requests.exceptions.ConnectionError:
            # CHRONICLE unreachable — stop trying, buffer locally
            logger.debug("CHRONICLE unreachable — will retry next cycle")
            break
        except Exception as exc:
            logger.warning("CHRONICLE push failed (rowid=%s): %s", rowid, exc)

    return synced_rowids


def _sync_loop() -> None:
    """Background sync loop — runs every CHRONICLE_SYNC_INTERVAL_S seconds."""
    logger.info("Chronicle sync thread started — interval=%ds", CHRONICLE_SYNC_INTERVAL_S)

    while not _stop_event.is_set():
        try:
            records = _get_unsynced_records(limit=100)
            if records:
                synced = _push_to_chronicle(records)
                if synced:
                    _mark_synced(synced)
                    logger.info("Chronicle sync: pushed %d/%d records", len(synced), len(records))
        except Exception as exc:
            logger.error("Chronicle sync loop error (non-fatal): %s", exc)

        _stop_event.wait(timeout=CHRONICLE_SYNC_INTERVAL_S)

    logger.info("Chronicle sync thread stopped")


def start_sync_thread() -> None:
    """Start the background CHRONICLE sync thread."""
    global _sync_thread
    _stop_event.clear()
    _sync_thread = threading.Thread(
        target=_sync_loop,
        name="chronicle-sync",
        daemon=True,
    )
    _sync_thread.start()
    logger.info("Chronicle sync thread started")


def stop_sync_thread() -> None:
    """Stop the background CHRONICLE sync thread gracefully."""
    _stop_event.set()
    if _sync_thread and _sync_thread.is_alive():
        _sync_thread.join(timeout=10)
    logger.info("Chronicle sync thread stopped")
