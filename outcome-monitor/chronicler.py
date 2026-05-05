"""
chronicler.py — Writes outcome monitor cycle results to CHRONICLE.

Every cycle produces exactly one CHRONICLE write. On failure, retries once
after 10s, then falls back to a local JSONL file. Never raises — the cycle
continues regardless of CHRONICLE availability.
"""

import json
import logging
import time
from typing import Any, Dict

import requests

from config import CHRONICLE_AUTH, CHRONICLE_URL, FALLBACK_LOG
from models import AlpacaSnapshot, CycleResult, ServiceSnapshot

logger = logging.getLogger(__name__)


def _snap_to_dict(snap: ServiceSnapshot) -> Dict[str, Any]:
    """
    Serialize a ServiceSnapshot to a plain dict for CHRONICLE payload.

    Args:
        snap: ServiceSnapshot to serialize.

    Returns:
        Dict merging status, error, and all service data fields.
    """
    return {"status": snap.status, "error": snap.error, **snap.data}


def _alpaca_to_dict(snap: AlpacaSnapshot) -> Dict[str, Any]:
    """
    Serialize an AlpacaSnapshot to a plain dict for CHRONICLE payload.

    Args:
        snap: AlpacaSnapshot to serialize.

    Returns:
        Dict with status, buying_power, portfolio_value, open_positions, error.
    """
    return {
        "status": snap.status,
        "buying_power": snap.buying_power,
        "portfolio_value": snap.portfolio_value,
        "open_positions": snap.open_positions,
        "error": snap.error,
    }


def _build_payload(result: CycleResult) -> Dict[str, Any]:
    """
    Serialize a CycleResult to the CHRONICLE /chronicle/adaptive POST payload.

    Args:
        result: Completed cycle result.

    Returns:
        JSON-serializable dict matching CHRONICLE adaptive event schema:
        { source, event_type, system, timestamp, payload, trigger, outcome_correlation_id }
    """
    return {
        "source": "outcome-monitor",
        "event_type": f"outcome_cycle_{result.diagnosis.diagnosis}",
        "system": "nexus",
        "timestamp": result.cycle_ts,
        "payload": {
            "market_open": result.market_open,
            "trades_since_last_cycle": result.trades_since_last_cycle,
            "consecutive_dry_cycles": result.diagnosis.consecutive_dry_cycles,
            "diagnosis": result.diagnosis.diagnosis,
            "severity": result.diagnosis.severity,
            "escalated": result.escalated,
            "details": result.diagnosis.details,
            "services": {
                name: _snap_to_dict(snap) for name, snap in result.services.items()
            },
            "alpaca": {
                account: _alpaca_to_dict(snap)
                for account, snap in result.alpaca.items()
            },
        },
        "trigger": "scheduled_30min",
        "outcome_correlation_id": None,
    }


def _write_fallback(payload: Dict[str, Any]) -> bool:
    """
    Write payload to local fallback JSONL file when CHRONICLE is unreachable.

    Args:
        payload: Serialized cycle result.

    Returns:
        Always False (indicates CHRONICLE write did not succeed).
    """
    try:
        with open(FALLBACK_LOG, "a") as f:
            f.write(json.dumps(payload) + "\n")
        logger.warning("Wrote fallback cycle log to %s", FALLBACK_LOG)
    except Exception as e:
        logger.critical("Fallback write also failed: %s", e)
    return False


def write_to_chronicle(result: CycleResult) -> bool:
    """
    POST cycle result to CHRONICLE. Retries once after 10s on failure,
    then writes to local fallback file.

    Args:
        result: Completed CycleResult to persist.

    Returns:
        True if CHRONICLE accepted the write, False if fell back to local file.
    """
    payload = _build_payload(result)
    headers = {
        "X-Chronicle-Auth": CHRONICLE_AUTH,
        "Content-Type": "application/json",
    }

    for attempt in range(2):
        try:
            resp = requests.post(
                CHRONICLE_URL, json=payload, headers=headers, timeout=10
            )
            if resp.status_code == 200:
                logger.info("CHRONICLE write successful (attempt %d)", attempt + 1)
                return True
            logger.error(
                "CHRONICLE returned HTTP %d on attempt %d",
                resp.status_code,
                attempt + 1,
            )
        except Exception as e:
            logger.error(
                "CHRONICLE unreachable on attempt %d: %s", attempt + 1, e
            )
        if attempt == 0:
            logger.info("Retrying CHRONICLE write in 10s...")
            time.sleep(10)

    return _write_fallback(payload)
