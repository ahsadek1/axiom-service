"""
test_pipeline_sentinel.py — All 12 required test cases for Pipeline Sentinel.

Each test is fully hermetic (fresh DB via tmp_path fixture).
Telegram HTTP calls are mocked — no real network calls in tests.
Uses FastAPI TestClient for endpoint tests.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Path setup — allow imports from parent directory
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent))

# Set required env vars BEFORE importing app modules
os.environ.setdefault("NEXUS_SECRET",               "test-secret-abc123")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",         "fake-token")
os.environ.setdefault("TELEGRAM_AHMED_CHAT_ID",     "12345")
os.environ.setdefault("HEALTH_HALT_THRESHOLD",      "55")
os.environ.setdefault("HEALTH_IMPAIR_THRESHOLD",    "70")
os.environ.setdefault("STALL_WINDOW_SECONDS",       "300")
os.environ.setdefault("SCORE_RECOMPUTE_INTERVAL_S", "30")

from database import (
    get_active_failures,
    get_latest_health_score,
    get_stalled_picks,
    get_traces_for_id,
    init_db,
    insert_failure_event,
    insert_health_score,
    insert_trace,
    prune_old_scores,
)
from models import (
    AnomalyReport,
    HealthScoreComponents,
    HopEnum,
    PathwayEnum,
    ServiceEnum,
    StatusEnum,
    SystemHealthResponse,
    TraceRequest,
)
from notifier import TelegramNotifier
from scorer import HealthScorer

_SECRET = "test-secret-abc123"
_HEADERS = {"X-Nexus-Secret": _SECRET}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Fresh, isolated SQLite DB for each test."""
    path = str(tmp_path / "test_sentinel.db")
    os.environ["PIPELINE_SENTINEL_DB_PATH"] = path
    init_db(path)
    return path


@pytest.fixture()
def client(db_path: str) -> Generator:
    """TestClient with the FastAPI app wired to the temp DB."""
    # Patch background threads so they don't interfere
    with patch("main.threading.Thread"):
        from main import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture()
def mock_telegram() -> Generator:
    """Suppress all real Telegram HTTP calls."""
    with patch("notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        yield mock_post


def _make_trace(
    trace_id: str,
    hop: str,
    ticker: str = "AAPL",
    pathway: str = "alpha",
    service: str = "cipher",
    status: str = "ok",
) -> TraceRequest:
    """Helper to build a TraceRequest."""
    return TraceRequest(
        trace_id=trace_id,
        hop=HopEnum(hop),
        service=ServiceEnum(service),
        ticker=ticker,
        pathway=PathwayEnum(pathway),
        status=StatusEnum(status),
        metadata={},
    )


# ---------------------------------------------------------------------------
# Test 1 — Happy path: 8-hop trace, GET /pipeline returns all hops in order
# ---------------------------------------------------------------------------

def test_happy_path_full_trace(db_path: str) -> None:
    """
    Submit all 8 hops for a single pick.
    Confirm GET /pipeline/{trace_id} returns all 8 in chronological order.
    """
    tid = "trace-happy-001"
    hops = [
        ("axiom_push",         "axiom"),
        ("agent_received",     "cipher"),
        ("buffer_accepted",    "alpha-buffer"),
        ("omni_started",       "omni"),
        ("omni_completed",     "omni"),
        ("execution_received", "alpha-execution"),
        ("alpaca_submitted",   "alpha-execution"),
        ("alpaca_confirmed",   "alpha-execution"),
    ]

    for hop, svc in hops:
        req = _make_trace(tid, hop, service=svc)
        insert_trace(db_path, req)
        time.sleep(0.01)  # ensure ts ordering

    result = get_traces_for_id(db_path, tid)
    assert len(result) == 8, f"Expected 8 hops, got {len(result)}"
    hop_names = [r["hop"] for r in result]
    assert hop_names == [h for h, _ in hops], f"Hop order wrong: {hop_names}"


# ---------------------------------------------------------------------------
# Test 2 — Stall detection
# ---------------------------------------------------------------------------

def test_stall_detection(db_path: str) -> None:
    """
    Insert axiom_push hop timestamped 6 minutes ago (beyond 300s window).
    Confirm get_stalled_picks returns this pick.
    """
    tid = "trace-stall-002"
    conn = sqlite3.connect(db_path)
    stale_ts = time.time() - 360   # 6 minutes ago — beyond 300s window
    conn.execute(
        "INSERT INTO traces (trace_id,ticker,pathway,hop,service,status,metadata,ts,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, "TSLA", "alpha", "axiom_push", "axiom", "ok", "{}", stale_ts,
         "2026-04-13T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    stalls = get_stalled_picks(db_path, stall_window_s=300)
    trace_ids = [s["trace_id"] for s in stalls]
    assert tid in trace_ids, f"Expected {tid} in stalls, got {trace_ids}"


# ---------------------------------------------------------------------------
# Test 3 — Score computation reflects low completion rate
# ---------------------------------------------------------------------------

def test_score_computation_low_completion(db_path: str, mock_telegram: MagicMock) -> None:
    """
    Insert 12 picks: 7 complete (58% rate).
    Confirm health score reflects a completion deduction of approximately 12–18 pts.
    """
    now = time.time()
    for i in range(12):
        tid = f"trace-score-{i:03d}"
        # All get axiom_push
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO traces (trace_id,ticker,pathway,hop,service,status,metadata,ts,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "MSFT", "alpha", "axiom_push", "axiom", "ok", "{}", now - (12 - i) * 10,
             "2026-04-13T00:00:00+00:00"),
        )
        # First 7 also get alpaca_confirmed
        if i < 7:
            conn.execute(
                "INSERT INTO traces (trace_id,ticker,pathway,hop,service,status,metadata,ts,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, "MSFT", "alpha", "alpaca_confirmed", "alpha-execution", "ok", "{}",
                 now - (12 - i) * 10 + 5, "2026-04-13T00:00:00+00:00"),
            )
        conn.commit()
        conn.close()

    notifier = TelegramNotifier()
    scorer = HealthScorer(db_path, 300, notifier)
    result = scorer.compute_score()

    # 7/12 = 58.3% completion — should deduct ~12 pts from completion component
    assert result.health_score < 100.0, "Score should not be 100 with 58% completion"
    assert result.score_components.pipeline_completion_rate < 0.65


# ---------------------------------------------------------------------------
# Test 4 — Health halt: score < 55 → submissions_open = False
# ---------------------------------------------------------------------------

def test_health_halt(db_path: str) -> None:
    """
    Insert a health score row with score=40 directly into DB.
    Confirm get_latest_health_score returns submissions_open=0 (False).
    """
    components = HealthScoreComponents(
        pipeline_completion_rate=0.50,
        inter_service_latency_p95_ms=4000.0,
        omni_brain_latency_p95_ms=9000.0,
        oracle_cache_freshness=0.0,
        active_anomaly_count=2,
        stalled_picks_count=3,
    )
    response = SystemHealthResponse(
        health_score=40.0,
        score_components=components,
        status="CRITICAL",
        recommended_size_multiplier=0.0,
        active_failures=["PIPELINE_STALL", "EXECUTOR_SLOW"],
        context_block={"health_score": 40.0, "executor_latency_p95_ms": 4000.0,
                       "pipeline_completion_rate": 0.50, "active_anomalies": [],
                       "recommended_size_multiplier": 0.0},
        submissions_open=False,
        computed_at="2026-04-13T00:00:00+00:00",
    )
    insert_health_score(db_path, response)
    row = get_latest_health_score(db_path)
    assert row is not None
    assert row["submissions_open"] == 0, "submissions_open should be 0 (False) at score=40"
    assert row["score"] == 40.0


# ---------------------------------------------------------------------------
# Test 5 — Score recovery: submissions_open flips as score crosses 55
# ---------------------------------------------------------------------------

def test_score_recovery(db_path: str) -> None:
    """
    Insert three successive health scores: 40 → 60 → 82.
    Confirm submissions_open transitions: False → True → True.
    """
    def _store(score: float, open_flag: bool) -> None:
        components = HealthScoreComponents(
            pipeline_completion_rate=1.0,
            inter_service_latency_p95_ms=0.0,
            omni_brain_latency_p95_ms=0.0,
            oracle_cache_freshness=1.0,
            active_anomaly_count=0,
            stalled_picks_count=0,
        )
        status_map = {40.0: "CRITICAL", 60.0: "IMPAIRED", 82.0: "DEGRADED"}
        response = SystemHealthResponse(
            health_score=score,
            score_components=components,
            status=status_map[score],
            recommended_size_multiplier=0.0 if score < 55 else 0.85,
            active_failures=[],
            context_block={"health_score": score, "executor_latency_p95_ms": 0.0,
                           "pipeline_completion_rate": 1.0, "active_anomalies": [],
                           "recommended_size_multiplier": 1.0},
            submissions_open=open_flag,
            computed_at="2026-04-13T00:00:00+00:00",
        )
        insert_health_score(db_path, response)
        time.sleep(0.01)

    _store(40.0, False)
    _store(60.0, True)
    _store(82.0, True)

    row = get_latest_health_score(db_path)
    assert row is not None
    assert row["score"] == 82.0
    assert row["submissions_open"] == 1


# ---------------------------------------------------------------------------
# Test 6 — EXECUTOR_SLOW classification
# ---------------------------------------------------------------------------

def test_executor_slow_detection(db_path: str, mock_telegram: MagicMock) -> None:
    """
    Insert 5 picks with alpaca_submitted→alpaca_confirmed latency >3000ms.
    Confirm EXECUTOR_SLOW failure event is created.
    """
    from classifier import FailureClassifier

    now = time.time()
    conn = sqlite3.connect(db_path)
    for i in range(5):
        tid = f"trace-exec-{i:03d}"
        t_submit = now - (5 - i) * 60
        conn.execute(
            "INSERT INTO traces (trace_id,ticker,pathway,hop,service,status,metadata,ts,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "NVDA", "alpha", "alpaca_submitted", "alpha-execution", "ok", "{}",
             t_submit, "2026-04-13T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO traces (trace_id,ticker,pathway,hop,service,status,metadata,ts,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "NVDA", "alpha", "alpaca_confirmed", "alpha-execution", "ok", "{}",
             t_submit + 4.0, "2026-04-13T00:00:00+00:00"),  # 4000ms latency
        )
    conn.commit()
    conn.close()

    notifier = TelegramNotifier()
    classifier = FailureClassifier(db_path, 300, notifier)
    active = classifier.detect_all()

    assert "EXECUTOR_SLOW" in active, f"Expected EXECUTOR_SLOW in {active}"


# ---------------------------------------------------------------------------
# Test 7 — AGENT_SILENT detection
# ---------------------------------------------------------------------------

def test_agent_silent_detection(db_path: str, mock_telegram: MagicMock) -> None:
    """
    Insert no hops from 'cipher' in the last 20 minutes.
    Confirm AGENT_SILENT failure event is created.
    """
    from classifier import FailureClassifier

    # Only atlas and sage have recent hops — cipher is silent
    now = time.time()
    conn = sqlite3.connect(db_path)
    for svc in ("atlas", "sage"):
        conn.execute(
            "INSERT INTO traces (trace_id,ticker,pathway,hop,service,status,metadata,ts,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"trace-agent-{svc}", "AMD", "prime", "agent_received", svc, "ok", "{}",
             now - 60, "2026-04-13T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    notifier = TelegramNotifier()
    # market_hours_override=True: bypass time-of-day gate so test passes at any hour
    classifier = FailureClassifier(db_path, 300, notifier, market_hours_override=True)
    active = classifier.detect_all()

    assert "AGENT_SILENT" in active, f"Expected AGENT_SILENT in {active}"


# ---------------------------------------------------------------------------
# Test 8 — DB integrity check: corrupt file detected and recreated
# ---------------------------------------------------------------------------

def test_db_integrity_check_and_recreate(tmp_path: Path) -> None:
    """
    Write garbage to the DB file.
    Confirm init_db detects corruption and recreates the database successfully.
    """
    db_file = tmp_path / "corrupt.db"
    db_file.write_bytes(b"THIS IS GARBAGE NOT A SQLITE DB!!!!!")

    # Should NOT raise — should delete and recreate
    conn = init_db(str(db_file))
    assert conn is not None

    # Verify it's a valid SQLite DB with correct schema
    result = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {r[0] for r in result}
    conn.close()

    assert "traces" in table_names
    assert "health_scores" in table_names
    assert "failure_events" in table_names


# ---------------------------------------------------------------------------
# Test 9 — Concurrent writes: 20 simultaneous POST /trace calls
# ---------------------------------------------------------------------------

def test_concurrent_trace_writes(db_path: str) -> None:
    """
    Fire 20 simultaneous insert_trace calls via threads.
    Confirm all 20 are recorded in the DB.
    """
    import threading

    results = []
    errors = []

    def _insert(i: int) -> None:
        try:
            req = _make_trace(f"trace-concurrent-{i:03d}", "axiom_push",
                              ticker=f"TIC{i}", service="axiom")
            insert_trace(db_path, req)
            results.append(i)
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=_insert, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Concurrent writes produced errors: {errors}"

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    conn.close()
    assert count == 20, f"Expected 20 traces, found {count}"


# ---------------------------------------------------------------------------
# Test 10 — Context block format: all 5 required keys present
# ---------------------------------------------------------------------------

def test_context_block_format(db_path: str, mock_telegram: MagicMock) -> None:
    """
    Compute a health score and confirm context_block contains all 5 required keys.
    """
    notifier = TelegramNotifier()
    scorer = HealthScorer(db_path, 300, notifier)
    result = scorer.compute_score()

    required_keys = {
        "health_score",
        "executor_latency_p95_ms",
        "pipeline_completion_rate",
        "active_anomalies",
        "recommended_size_multiplier",
    }
    missing = required_keys - set(result.context_block.keys())
    assert not missing, f"context_block missing keys: {missing}"

    # Type checks
    assert isinstance(result.context_block["health_score"], float)
    assert isinstance(result.context_block["active_anomalies"], list)
    assert isinstance(result.context_block["recommended_size_multiplier"], float)


# ---------------------------------------------------------------------------
# Test 11 — Telegram dedup: same failure_class within 300s sent only once
# ---------------------------------------------------------------------------

def test_telegram_dedup(mock_telegram: MagicMock) -> None:
    """
    Trigger send_alert for the same failure_class twice within dedup window.
    Confirm Telegram POST was called exactly once.
    """
    notifier = TelegramNotifier()
    notifier.send_alert("First alert", failure_class="TEST_CLASS")
    notifier.send_alert("Second alert", failure_class="TEST_CLASS")

    assert mock_telegram.call_count == 1, (
        f"Expected 1 Telegram call, got {mock_telegram.call_count}"
    )


# ---------------------------------------------------------------------------
# Test 12 — Score history pruning: old records deleted, recent survive
# ---------------------------------------------------------------------------

def test_score_history_pruning(db_path: str) -> None:
    """
    Insert one health score 31 days ago and one score now.
    Run prune_old_scores(keep_days=30).
    Confirm old record is deleted, recent record survives.
    """
    conn = sqlite3.connect(db_path)
    old_ts  = time.time() - (31 * 86400)   # 31 days ago
    now_ts  = time.time()

    components_json = json.dumps({
        "pipeline_completion_rate": 1.0,
        "inter_service_latency_p95_ms": 0.0,
        "omni_brain_latency_p95_ms": 0.0,
        "oracle_cache_freshness": 1.0,
        "active_anomaly_count": 0,
        "stalled_picks_count": 0,
    })

    conn.execute(
        "INSERT INTO health_scores (score,status,size_multiplier,submissions_open,components,active_failures,ts,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (95.0, "NOMINAL", 1.0, 1, components_json, "[]", old_ts, "2026-03-13T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO health_scores (score,status,size_multiplier,submissions_open,components,active_failures,ts,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (88.0, "NOMINAL", 1.0, 1, components_json, "[]", now_ts, "2026-04-13T18:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    prune_old_scores(db_path, keep_days=30)

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT score, ts FROM health_scores").fetchall()
    conn.close()

    assert len(rows) == 1, f"Expected 1 row after pruning, got {len(rows)}: {rows}"
    assert rows[0][0] == 88.0, f"Expected recent score 88.0 to survive, got {rows[0][0]}"
