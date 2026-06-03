"""
test_chronicle_client.py — PROBE chronicle_client unit tests
"""

import json
import os
import sqlite3
import sys
import tempfile
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chronicle_client


@pytest.fixture
def tmp_chronicle(tmp_path):
    db_path = str(tmp_path / "chronicle.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE critique_bank (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name        TEXT NOT NULL,
            service_version     TEXT NOT NULL,
            review_cycle_id     TEXT NOT NULL,
            reviewer_agent      TEXT NOT NULL,
            reviewer_brain      TEXT NOT NULL,
            review_status       TEXT NOT NULL DEFAULT 'IN_PROGRESS',
            overall_assessment  TEXT,
            p0_count            INTEGER DEFAULT 0,
            p1_count            INTEGER DEFAULT 0,
            p2_count            INTEGER DEFAULT 0,
            p3_count            INTEGER DEFAULT 0,
            full_report         TEXT,
            confidence_level    TEXT,
            review_started_at   TEXT,
            review_completed_at TEXT,
            findings_addressed_at TEXT,
            addressed_by        TEXT,
            fix_commit_hash     TEXT,
            verification_status TEXT DEFAULT 'PENDING',
            verified_by_agent   TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def tmp_local(tmp_path):
    from database import init_db
    db_path = str(tmp_path / "probe.db")
    init_db(db_path)
    return db_path


def test_chronicle_write_on_completion(tmp_chronicle, tmp_local):
    """Test 5: findings written to chronicle_client."""
    row_id = chronicle_client.create_review_entry(
        tmp_chronicle, tmp_local, "test-svc", "1.0.0", "cycle-123"
    )
    assert row_id > 0

    ok = chronicle_client.write_findings(
        tmp_chronicle, tmp_local, row_id,
        "PASS", 0, 1, 2, 3,
        [{"severity": "P1", "category": "x", "description": "d",
          "file": "f", "line_hint": "l", "recommendation": "r"}],
        "MEDIUM", "cycle-123",
    )
    assert ok is True

    conn = sqlite3.connect(tmp_chronicle)
    row = conn.execute("SELECT * FROM critique_bank WHERE id=?", (row_id,)).fetchone()
    conn.close()
    assert row is not None
    assert row[6] == "COMPLETED"   # review_status
    assert row[7] == "PASS"        # overall_assessment
    assert row[8] == 0             # p0_count
    assert row[9] == 1             # p1_count


def test_chronicle_cache_on_failure(tmp_local):
    """Test 11: chronicle down → cached in local SQLite."""
    bad_chronicle_path = "/nonexistent/chronicle.db"
    row_id = chronicle_client.create_review_entry(
        bad_chronicle_path, tmp_local, "test-svc", "1.0.0", "cycle-456"
    )
    # Should return a negative local cache ID
    assert row_id < 0

    # Check it was cached locally
    conn = sqlite3.connect(tmp_local)
    rows = conn.execute("SELECT * FROM chronicle_cache").fetchall()
    conn.close()
    assert len(rows) >= 1


def test_write_findings_caches_on_failure(tmp_local):
    """Write findings fails gracefully and caches locally."""
    ok = chronicle_client.write_findings(
        "/nonexistent/path.db", tmp_local, 999,
        "FAIL", 1, 0, 0, 0, [], "LOW", "cycle-789",
    )
    assert ok is False

    conn = sqlite3.connect(tmp_local)
    rows = conn.execute("SELECT op FROM chronicle_cache WHERE op='UPDATE'").fetchall()
    conn.close()
    assert len(rows) >= 1
