"""
directives.py — VECTOR Bus Directive Validation + Poison Guard (V5+V6).
Spec: vector-resilience-v1.md v1.1

Validates every bus directive before processing:
- Unknown type → quarantine + consume (never re-process)
- Unknown restart target → quarantine + consume
- Processing exception → quarantine + consume

Dedup via local consumed_ids.json (no /consume endpoint needed on bus).
Keeps last 1000 IDs to prevent unbounded growth.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("vector.resilience.directives")

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_DIRECTIVE_TYPES: frozenset[str] = frozenset({
    "restart_service", "check_service", "escalate", "ping"
})

VALID_SERVICE_NAMES: frozenset[str] = frozenset({
    "axiom", "alpha-buffer", "prime-buffer", "omni",
    "alpha-execution", "prime-execution", "oracle", "ails", "guardian-angel",
})

CONSUMED_IDS_PATH = Path("/Users/ahmedsadek/nexus/vector/state/consumed_ids.json")
MAX_CONSUMED_IDS  = 1000


# ── Consumed IDs (dedup) ──────────────────────────────────────────────────────

def load_consumed_ids() -> set:
    """
    Load consumed message IDs from persistent state file.

    Returns empty set on any error (missing file, bad JSON, permission error).
    Never raises.

    Returns:
        Set of consumed message ID strings.
    """
    try:
        if CONSUMED_IDS_PATH.exists():
            return set(json.loads(CONSUMED_IDS_PATH.read_text()))
        return set()
    except Exception as e:
        logger.warning("load_consumed_ids: failed: %s — returning empty set", e)
        return set()


def save_consumed_id(msg_id: str) -> None:
    """
    Append a message ID to the consumed set and persist atomically.

    Keeps at most MAX_CONSUMED_IDS entries to prevent unbounded growth.
    Atomic write: write to .tmp file, then rename.

    Args:
        msg_id: Message ID to mark as consumed.
    """
    try:
        ids = load_consumed_ids()
        ids.add(msg_id)
        # Keep last MAX_CONSUMED_IDS
        trimmed = list(ids)[-MAX_CONSUMED_IDS:]

        CONSUMED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = CONSUMED_IDS_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(trimmed))
        tmp_path.replace(CONSUMED_IDS_PATH)
    except Exception as e:
        logger.error("save_consumed_id: failed to persist %s: %s", msg_id, e)


# ── Chronicle quarantine ──────────────────────────────────────────────────────

def chronicle_quarantine(directive: dict, reason: str) -> None:
    """
    Log a quarantined directive to CHRONICLE for audit.

    Fail-open — CHRONICLE failure is logged but never propagated.
    Also logs locally at WARNING level.

    Args:
        directive: The offending directive dict.
        reason:    Why it was quarantined.
    """
    logger.warning(
        "QUARANTINE: directive id=%s type=%s reason=%s",
        directive.get("id", "?"), directive.get("type", "?"), reason
    )
    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from chronicle_reader import chronicle_write  # type: ignore
        chronicle_write("quarantine_log", {
            "agent": "vector",
            "directive_id": directive.get("id", ""),
            "directive_type": directive.get("type", ""),
            "reason": reason,
            "raw": json.dumps(directive)[:500],
        })
    except Exception as e:
        logger.warning("chronicle_quarantine: CHRONICLE write failed (non-blocking): %s", e)


# ── Main validation + processing ─────────────────────────────────────────────

def validate_and_process_directives(
    directives: list,
    process_fn: Callable[[dict], Any]
) -> dict[str, int]:
    """
    Validate, dedup, and process a list of bus directives.

    For each directive:
        1. Skip if msg_id already in consumed_ids (dedup)
        2. Validate directive type → quarantine if unknown
        3. Validate target for restart_service → quarantine if unknown
        4. Call process_fn(directive) in try/except → quarantine on exception
        5. Always mark as consumed after any disposition

    Args:
        directives: List of directive dicts from the message bus.
        process_fn: Callable that executes a single validated directive.
                    Receives the full directive dict. Return value is ignored.

    Returns:
        Dict with counts: {"processed": N, "quarantined": N, "skipped": N}
    """
    consumed = load_consumed_ids()
    counts = {"processed": 0, "quarantined": 0, "skipped": 0}

    for directive in directives:
        if not isinstance(directive, dict):
            logger.warning("validate_and_process_directives: non-dict item skipped: %r", directive)
            continue

        msg_id = str(directive.get("id", ""))

        # Step 1: Dedup check
        if msg_id and msg_id in consumed:
            logger.debug("Directive %s already consumed — skipping", msg_id)
            counts["skipped"] += 1
            continue

        quarantined = False

        # Step 2: Type validation
        directive_type = directive.get("type")
        if directive_type not in VALID_DIRECTIVE_TYPES:
            chronicle_quarantine(directive, f"unknown directive type: {directive_type!r}")
            quarantined = True

        # Step 3: Target validation for restart_service
        elif directive_type == "restart_service":
            target = directive.get("target")
            if target not in VALID_SERVICE_NAMES:
                chronicle_quarantine(directive, f"unknown restart target: {target!r}")
                quarantined = True

        if quarantined:
            counts["quarantined"] += 1
            if msg_id:
                save_consumed_id(msg_id)
                consumed.add(msg_id)
            continue

        # Step 4: Process
        try:
            process_fn(directive)
            counts["processed"] += 1
            logger.info(
                "Directive processed: id=%s type=%s target=%s",
                msg_id, directive_type, directive.get("target", "-")
            )
        except Exception as e:
            chronicle_quarantine(directive, f"processing exception: {str(e)[:200]}")
            counts["quarantined"] += 1

        # Step 5: Always mark consumed
        finally:
            if msg_id:
                save_consumed_id(msg_id)
                consumed.add(msg_id)

    return counts
