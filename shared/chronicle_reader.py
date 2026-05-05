"""
chronicle_reader.py — CHRONICLE Governance Document Reader

Every agent reads its governance mandates from CHRONICLE at session start.
One source of truth. Version-controlled. Universally applied.

Usage (in each agent's startup or AGENTS.md protocol):
    from shared.chronicle_reader import load_mandate, record_read

    mandate = load_mandate("precision_mandate_v1")
    if mandate:
        record_read("precision_mandate_v1", agent="Cipher")
        # mandate["content"] contains the full text
        # mandate["version"] contains "1.0" (or latest)
        # mandate["checksum"] for integrity verification

CHRONICLE DB path: /Users/ahmedsadek/nexus/data/chronicle.db
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("nexus.chronicle_reader")

CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"


def _connect() -> sqlite3.Connection:
    """Open a read-only WAL connection to CHRONICLE."""
    conn = sqlite3.connect(CHRONICLE_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_agent_mandate_v2(agent: str = "unknown") -> str:
    """
    Load AGENT MANDATE v2 — Immediate Intervention Protocol (2026-04-28).
    ALL agents must call this at session start.
    Core law: identify failure → diagnose → fix → verify → inform SOVEREIGN → register CHRONICLE.
    No reporting without acting. No waiting for permission.
    """
    return load_mandate("agent_mandate_v2", agent=agent)


def load_mandate(
    doc_key: str,
    version: Optional[str] = None,
    agent: Optional[str] = None,
    session_key: Optional[str] = None,
) -> Optional[dict]:
    """
    Load a governance document from CHRONICLE by key.

    Returns the latest version unless `version` is specified.
    Optionally records the read in governance_reads for audit.

    Args:
        doc_key:     Document key (e.g. "precision_mandate_v1").
        version:     Specific version string, or None for latest.
        agent:       Agent name for audit logging (e.g. "Cipher").
        session_key: Optional session identifier for audit trail.

    Returns:
        Dict with keys: doc_key, version, title, content, applies_to,
        authored_by, created_at, checksum. None if not found or DB error.
    """
    try:
        conn = _connect()
        if version:
            row = conn.execute(
                "SELECT * FROM governance_documents WHERE doc_key=? AND version=?",
                (doc_key, version),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM governance_documents
                WHERE doc_key=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (doc_key,),
            ).fetchone()

        if row is None:
            logger.warning("chronicle_reader: doc_key='%s' not found in CHRONICLE", doc_key)
            conn.close()
            return None

        result = dict(row)

        # Integrity check — verify stored checksum matches content
        computed = hashlib.sha256(result["content"].encode()).hexdigest()
        if computed != result["checksum"]:
            logger.error(
                "chronicle_reader: INTEGRITY FAILURE for doc_key='%s' v%s — "
                "stored checksum does not match content. Document may be corrupted.",
                doc_key, result["version"],
            )
            conn.close()
            return None

        # Record read for audit if agent provided
        if agent:
            try:
                rw_conn = sqlite3.connect(CHRONICLE_DB, timeout=5)
                rw_conn.execute(
                    """
                    INSERT INTO governance_reads (doc_key, version, agent, session_key, read_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        result["doc_key"],
                        result["version"],
                        agent,
                        session_key or "",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                rw_conn.commit()
                rw_conn.close()
            except Exception as audit_err:
                # Audit failure must never block mandate loading
                logger.warning("chronicle_reader: audit write failed for %s: %s", agent, audit_err)

        conn.close()
        return result

    except Exception as e:
        logger.error("chronicle_reader: failed to load '%s': %s", doc_key, e)
        return None


def get_compliance_block(agent_name: str) -> str:
    """
    Return the standardized compliance reference paragraph for IDENTITY.md.

    This is the one-paragraph reference that lives in every agent's IDENTITY.md.
    It points to CHRONICLE — never embeds the full mandate text.

    Args:
        agent_name: The agent's canonical name (e.g. "Cipher", "GENESIS").

    Returns:
        Formatted compliance block string.
    """
    return f"""## Governance Compliance

{agent_name} operates under **PRECISION MANDATE v1** stored in CHRONICLE.

- **Source of truth:** `/Users/ahmedsadek/nexus/data/chronicle.db` → `governance_documents` → `precision_mandate_v1`
- **Read at startup** via `shared/chronicle_reader.py` → `load_mandate("precision_mandate_v1")`
- **Version:** 1.0 (2026-04-24). When the mandate updates, CHRONICLE holds the history. This agent automatically uses the latest version at next session start.
- **Full text:** Do not embed inline. Read from CHRONICLE.
"""


def list_mandates() -> list[dict]:
    """
    List all governance documents in CHRONICLE.

    Returns:
        List of dicts with doc_key, version, title, created_at, applies_to.
    """
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT doc_key, version, title, created_at, applies_to FROM governance_documents ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("chronicle_reader: list_mandates failed: %s", e)
        return []


def verify_integrity() -> bool:
    """
    Verify all governance documents in CHRONICLE pass checksum validation.

    Returns:
        True if all documents are intact, False if any checksum fails.
    """
    try:
        conn = _connect()
        rows = conn.execute("SELECT doc_key, version, content, checksum FROM governance_documents").fetchall()
        conn.close()
        all_ok = True
        for row in rows:
            computed = hashlib.sha256(row["content"].encode()).hexdigest()
            if computed != row["checksum"]:
                logger.error(
                    "INTEGRITY FAILURE: doc_key='%s' v%s checksum mismatch",
                    row["doc_key"], row["version"],
                )
                all_ok = False
            else:
                logger.info("Integrity OK: %s v%s", row["doc_key"], row["version"])
        return all_ok
    except Exception as e:
        logger.error("chronicle_reader: verify_integrity failed: %s", e)
        return False
