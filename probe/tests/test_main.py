"""
test_main.py — PROBE FastAPI endpoint tests (12 required tests)
"""

import os
import sys
import sqlite3
import tempfile
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dbs(tmp_path):
    """Create temp chronicle and local DBs."""
    chronicle_path = str(tmp_path / "chronicle.db")
    local_path = str(tmp_path / "probe.db")

    # Initialize chronicle_bank
    conn = sqlite3.connect(chronicle_path)
    conn.executescript("""
        CREATE TABLE critique_bank (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name TEXT NOT NULL, service_version TEXT NOT NULL,
            review_cycle_id TEXT NOT NULL, reviewer_agent TEXT NOT NULL,
            reviewer_brain TEXT NOT NULL, review_status TEXT DEFAULT 'IN_PROGRESS',
            overall_assessment TEXT, p0_count INTEGER DEFAULT 0,
            p1_count INTEGER DEFAULT 0, p2_count INTEGER DEFAULT 0,
            p3_count INTEGER DEFAULT 0, full_report TEXT,
            confidence_level TEXT, review_started_at TEXT,
            review_completed_at TEXT, findings_addressed_at TEXT,
            addressed_by TEXT, fix_commit_hash TEXT,
            verification_status TEXT DEFAULT 'PENDING', verified_by_agent TEXT
        );
    """)
    conn.commit()
    conn.close()

    from database import init_db
    init_db(local_path)
    return chronicle_path, local_path


@pytest.fixture
def mock_settings(tmp_dbs):
    chronicle_path, local_path = tmp_dbs
    from config import Settings
    return Settings(
        port=9010,
        nexus_secret="test-secret-abc123",
        deepseek_api_key="sk-test",
        deepseek_api_url="https://api.deepseek.com/v1",
        chronicle_db_path=chronicle_path,
        sovereign_bus_url="http://localhost:9999",
        telegram_bot_token="123:ABC",
        telegram_chat_id="12345",
        db_path=local_path,
    )


@pytest.fixture
def client(mock_settings):
    """Create TestClient with mocked settings and background tasks."""
    from fastapi.testclient import TestClient

    with patch("main._settings", mock_settings), \
         patch("main.report"), \
         patch("main.get_instructions", return_value=[]), \
         patch("chronicle_client.start_flush_thread"), \
         patch("chronicle_client.stop_flush_thread"):

        import main as m
        m._settings = mock_settings
        m._sovereign_halted = False
        m._brain_failures_today = 0

        with TestClient(m.app) as tc:
            yield tc


VALID_REVIEW = {
    "service_name": "test-service",
    "service_version": "1.0.0",
    "review_cycle_id": "cycle-test-001",
    "code_files": [{"path": "main.py", "content": "print('hello')"}],
    "spec_content": "Service must be secure.",
    "context": "Test context",
}

AUTH_HEADERS = {"X-Nexus-Secret": "test-secret-abc123"}
BAD_HEADERS = {"X-Nexus-Secret": "wrong-secret"}


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_health_returns_200(client, mock_settings):
    """Test 1: GET /health returns 200 with all checks."""
    with patch("main._settings", mock_settings):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "PROBE"
    assert "checks" in body
    assert "status" in body


def test_auth_required_on_review(client):
    """Test 2: POST /review without secret → 403."""
    resp = client.post("/review", json=VALID_REVIEW)
    assert resp.status_code == 403


def test_auth_wrong_secret(client):
    """POST /review with wrong secret → 403."""
    resp = client.post("/review", json=VALID_REVIEW, headers=BAD_HEADERS)
    assert resp.status_code == 403


def test_review_returns_202(client, mock_settings):
    """Test 3: valid review request → 202 Accepted immediately."""
    with patch("chronicle_client.create_review_entry", return_value=42), \
         patch("database.upsert_review"), \
         patch("main._run_review"):
        resp = client.post("/review", json=VALID_REVIEW, headers=AUTH_HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "review_started"
    assert body["agent"] == "PROBE"
    assert body["review_cycle_id"] == "cycle-test-001"


def test_review_async_runs(client, mock_settings):
    """Test 4: background task executes analyzer."""
    run_called = []

    def fake_run(*args, **kwargs):
        run_called.append(True)

    with patch("chronicle_client.create_review_entry", return_value=1), \
         patch("database.upsert_review"), \
         patch("main._run_review", side_effect=fake_run):
        resp = client.post("/review", json=VALID_REVIEW, headers=AUTH_HEADERS)

    assert resp.status_code == 202
    # TestClient runs background tasks synchronously
    assert len(run_called) == 1


def test_sovereign_directive_halt(client, mock_settings):
    """Test 7: POST /sovereign/directive HALT → halts reviews."""
    import main as m
    m._sovereign_halted = False
    resp = client.post(
        "/sovereign/directive",
        json={"directive": "HALT", "data": {}, "from_agent": "sovereign"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["halted"] is True
    assert m._sovereign_halted is True


def test_sovereign_directive_resume(client, mock_settings):
    """Test 8: POST /sovereign/directive RESUME → resumes."""
    import main as m
    m._sovereign_halted = True
    resp = client.post(
        "/sovereign/directive",
        json={"directive": "RESUME", "data": {}, "from_agent": "sovereign"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["halted"] is False
    assert m._sovereign_halted is False


def test_sovereign_status_endpoint(client, mock_settings):
    """Test 9: GET /sovereign/status returns correct shape."""
    resp = client.get("/sovereign/status", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "PROBE"
    assert "reviews_completed_today" in body
    assert "brain_failures_today" in body
    assert "halted" in body


def test_malformed_request_returns_400(client):
    """Test 10: missing required fields → 422 (FastAPI validation)."""
    resp = client.post(
        "/review",
        json={"service_name": "only-partial"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


def test_review_not_found(client):
    """GET /review/{id} for unknown cycle → 404."""
    resp = client.get("/review/nonexistent-cycle", headers=AUTH_HEADERS)
    assert resp.status_code == 404


def test_sovereign_auth_required(client):
    """SOVEREIGN endpoints require auth."""
    resp = client.get("/sovereign/status")
    assert resp.status_code == 403


def test_sovereign_directive_unrecognized(client):
    """Unknown directive → 400."""
    resp = client.post(
        "/sovereign/directive",
        json={"directive": "UNKNOWN_DIRECTIVE"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
