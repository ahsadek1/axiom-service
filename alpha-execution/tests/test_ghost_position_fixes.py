"""
test_ghost_position_fixes.py — Tests for the two ghost-position prevention fixes.

Fix 1: Test window guard — window_id containing 'test' is rejected at /execute
        before any DB write, so test entries never create production positions.

Fix 2: Startup reconciliation — on service start, DB-open positions that are
        either test-window entries or absent from Alpaca are force-closed with
        a human-readable reason.

These tests cover the root cause of the 2026-04-24 6-day execution blockade.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# Env must be set before importing main
os.environ["NEXUS_AUTO_EXECUTE"]   = "true"
# Use conftest canonical secret so settings.nexus_webhook_secret always matches
os.environ.setdefault("NEXUS_WEBHOOK_SECRET", "x" * 64)
# If conftest already set it to "x"*64, keep it. If not, use "x"*64.
# The hard-coded "test-ghost-fix-secret" was causing cross-module 403 failures.
_tmp_db = tempfile.mktemp(suffix="-ghost-fix.db")
os.environ["ALPHA_EXEC_DB_PATH"]   = _tmp_db
os.environ["ALPACA_API_KEY"]       = "test-key"
os.environ["ALPACA_SECRET_KEY"]    = "test-secret"
os.environ["ALPHA_BUFFER_URL"]     = "http://localhost:8002"
os.environ["TELEGRAM_BOT_TOKEN"]   = "9999:TESTTOKEN"
os.environ["AHMED_CHAT_ID"]        = "8573754783"
os.environ.setdefault("AXIOM_URL",    "http://localhost:8001")
os.environ.setdefault("AXIOM_SECRET", "test-axiom-secret")

SECRET  = os.environ.get("NEXUS_WEBHOOK_SECRET", "x" * 64)
HEADERS = {"X-Nexus-Secret": SECRET}

# ── Base execute payload ─────────────────────────────────────────────────────

VALID_EXECUTE = {
    "ticker":            "AAPL",
    "direction":         "bullish",
    "pathway":           "P1",
    "weighted_score":    82.0,
    "agent_scores":      {"Cipher": 85, "Atlas": 80, "Sage": 78},
    "verdict":           "STRONG_GO",
    "sizing_mult":       1.0,
    "position_size_usd": 2000.0,
    "window_id":         "2026-04-24-0930",  # valid production window
    "echo_chamber":      False,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 1: Test Window Guard in /execute
# ═══════════════════════════════════════════════════════════════════════════════

class TestTestWindowGuard:
    """
    /execute must reject any window_id containing 'test' (case-insensitive)
    with a 422 before any DB write or Alpaca call.
    """

    @pytest.fixture(autouse=True)
    def _patch_market_hours(self):
        """Force market-hours gate to pass so only the test-window gate fires.
        Also mock AlpacaClient.get_latest_price so execution path doesn't blow
        up on MagicMock comparison when a valid window reaches contract resolution."""
        with patch("main._is_market_hours", return_value=True), \
             patch("main.AlpacaClient") as _mock_alpaca:
            _mock_alpaca.return_value.get_latest_price.return_value = 500.0
            _mock_alpaca.return_value.get_positions.return_value = []
            yield

    def _make_client(self):
        # Patch preflight so TestClient lifespan doesn't enter STANDBY.
        # Also reload settings so the correct NEXUS_WEBHOOK_SECRET is used
        # (module-level settings may have been loaded by a different test file).
        with patch("main._preflight_check", return_value=(True, "")), \
             patch("main._run_preflight_retry_loop"):
            from main import app, load_settings
            import main as _m
            _m.settings = load_settings()
            _m._SERVICE_MODE = "active"
            from database import init_db
            init_db(_tmp_db)
            return TestClient(app)

    def test_rejects_exact_test_window(self):
        client = self._make_client()
        payload = {**VALID_EXECUTE, "window_id": "test-2026-04-24-1100"}
        resp = client.post("/execute", json=payload, headers=HEADERS)
        assert resp.status_code == 422
        body = resp.json()
        assert body["executed"] is False
        assert body["gate"] == "test_window_guard"
        assert "test" in body["reason"].lower()

    def test_rejects_test_prefix(self):
        client = self._make_client()
        payload = {**VALID_EXECUTE, "window_id": "TEST-2026-04-24-1100"}
        resp = client.post("/execute", json=payload, headers=HEADERS)
        assert resp.status_code == 422
        assert resp.json()["gate"] == "test_window_guard"

    def test_rejects_test_mid_window(self):
        client = self._make_client()
        payload = {**VALID_EXECUTE, "window_id": "2026-04-24-test-window"}
        resp = client.post("/execute", json=payload, headers=HEADERS)
        assert resp.status_code == 422
        assert resp.json()["gate"] == "test_window_guard"

    def test_rejects_test_uppercase(self):
        client = self._make_client()
        payload = {**VALID_EXECUTE, "window_id": "TEST_ENTRY_0001"}
        resp = client.post("/execute", json=payload, headers=HEADERS)
        assert resp.status_code == 422
        assert resp.json()["gate"] == "test_window_guard"

    def test_no_db_write_on_test_window(self):
        """Verify that a test-window rejection writes nothing to the DB."""
        from database import count_open_positions, get_conn
        client = self._make_client()
        before = count_open_positions(_tmp_db)
        payload = {**VALID_EXECUTE, "window_id": "test-2026-04-24-ghost"}
        client.post("/execute", json=payload, headers=HEADERS)
        after = count_open_positions(_tmp_db)
        assert after == before, "Test window must not write to DB"

    def test_valid_production_window_not_blocked(self):
        """A valid production window_id must not be caught by the test guard."""
        client = self._make_client()
        payload = {**VALID_EXECUTE, "window_id": "2026-04-24-0930"}
        # We only care that it doesn't return 422 with gate=test_window_guard.
        # Any other status (200, 422 with different gate, 503, etc.) is acceptable.
        try:
            resp = client.post("/execute", json=payload, headers=HEADERS)
            if resp.status_code == 422:
                assert resp.json().get("gate") != "test_window_guard", (
                    "Production window must not trigger test_window_guard"
                )
        except Exception as e:
            # RuntimeError from client lifecycle or other infra issue is acceptable
            # as long as it's not a test_window_guard rejection.
            assert "test_window_guard" not in str(e), (
                f"Production window triggered test_window_guard: {e}"
            )

    def test_empty_window_id_not_blocked(self):
        """An empty string window_id must not trigger the test guard."""
        client = self._make_client()
        payload = {**VALID_EXECUTE, "window_id": ""}
        resp = client.post("/execute", json=payload, headers=HEADERS)
        if resp.status_code == 422:
            assert resp.json().get("gate") != "test_window_guard"


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 2: Startup Reconciliation
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartupReconciliation:
    """
    _run_startup_reconciliation() must:
    - Close any DB position whose window_id contains 'test'
    - Close any DB position whose ticker is absent from Alpaca live positions
    - Leave DB positions that exist in Alpaca untouched
    - Survive gracefully if Alpaca is unreachable
    """

    def _seed_position(
        self,
        db_path: str,
        ticker: str = "AAPL",
        window_id: str = "2026-04-24-0930",
        status: str = "open",
    ) -> int:
        """Insert a minimal position row directly. Returns row id."""
        from database import get_conn
        import json as _json
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with get_conn(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO positions (
                    ticker, direction, pathway, option_type,
                    short_strike, long_strike, expiration_date, dte_at_open,
                    contracts, position_size_usd, window_id, agent_scores, verdict,
                    status, opened_at
                ) VALUES (?, 'bullish', 'P1', 'put', 0, 0, '2026-06-20', 45,
                          1, 2000, ?, '{}', 'STRONG_GO', ?, ?)
                """,
                (ticker, window_id, status, now),
            )
        return cursor.lastrowid

    def test_closes_test_window_entry(self):
        """DB position with window_id='test-...' is force-closed on startup."""
        db = tempfile.mktemp(suffix="-recon-test1.db")
        from database import init_db, get_open_positions
        init_db(db)
        self._seed_position(db, ticker="AAPL", window_id="test-2026-04-24-1100")

        assert len(get_open_positions(db)) == 1

        # Patch settings to use our temp db + mock Alpaca
        import main as _main
        orig_settings = _main.settings
        _main.settings = MagicMock()
        _main.settings.alpha_db_path = db
        _main.settings.telegram_bot_token = "9999:TEST"
        _main.settings.ahmed_chat_id = "123"

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.return_value = [{"symbol": "AAPL"}]

        try:
            with patch("main._get_alpaca", return_value=mock_alpaca), \
                 patch("requests.post"):
                _main._run_startup_reconciliation()
        finally:
            _main.settings = orig_settings

        # DB position should now be closed
        assert len(get_open_positions(db)) == 0

    def test_closes_position_absent_from_alpaca(self):
        """DB position whose ticker is not in Alpaca is force-closed."""
        db = tempfile.mktemp(suffix="-recon-test2.db")
        from database import init_db, get_open_positions
        init_db(db)
        self._seed_position(db, ticker="NVDA", window_id="2026-04-24-0930")

        assert len(get_open_positions(db)) == 1

        import main as _main
        orig = _main.settings
        _main.settings = MagicMock()
        _main.settings.alpha_db_path = db
        _main.settings.telegram_bot_token = "9999:TEST"
        _main.settings.ahmed_chat_id = "123"

        mock_alpaca = MagicMock()
        # Alpaca has AAPL, not NVDA
        mock_alpaca.get_positions.return_value = [{"symbol": "AAPL"}]

        try:
            with patch("main._get_alpaca", return_value=mock_alpaca), \
                 patch("requests.post"):
                _main._run_startup_reconciliation()
        finally:
            _main.settings = orig

        assert len(get_open_positions(db)) == 0

    def test_leaves_matching_position_open(self):
        """DB position whose ticker IS in Alpaca is left open."""
        db = tempfile.mktemp(suffix="-recon-test3.db")
        from database import init_db, get_open_positions
        init_db(db)
        self._seed_position(db, ticker="MSFT", window_id="2026-04-24-1000")

        import main as _main
        orig = _main.settings
        _main.settings = MagicMock()
        _main.settings.alpha_db_path = db
        _main.settings.telegram_bot_token = "9999:TEST"
        _main.settings.ahmed_chat_id = "123"

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.return_value = [{"symbol": "MSFT"}]

        try:
            with patch("main._get_alpaca", return_value=mock_alpaca), \
                 patch("requests.post"):
                _main._run_startup_reconciliation()
        finally:
            _main.settings = orig

        # MSFT is in Alpaca — must remain open
        assert len(get_open_positions(db)) == 1

    def test_alpaca_unreachable_still_closes_test_entries(self):
        """Even if Alpaca is down, test-window entries must be closed."""
        db = tempfile.mktemp(suffix="-recon-test4.db")
        from database import init_db, get_open_positions
        init_db(db)
        self._seed_position(db, ticker="TSLA", window_id="test-ghost-entry")

        import main as _main
        orig = _main.settings
        _main.settings = MagicMock()
        _main.settings.alpha_db_path = db
        _main.settings.telegram_bot_token = "9999:TEST"
        _main.settings.ahmed_chat_id = "123"

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.side_effect = Exception("Connection refused")

        try:
            with patch("main._get_alpaca", return_value=mock_alpaca), \
                 patch("requests.post"):
                _main._run_startup_reconciliation()
        finally:
            _main.settings = orig

        # Test entry closed even though Alpaca was down
        assert len(get_open_positions(db)) == 0

    def test_alpaca_unreachable_leaves_production_positions(self):
        """If Alpaca is down, production positions (non-test) are NOT touched."""
        db = tempfile.mktemp(suffix="-recon-test5.db")
        from database import init_db, get_open_positions
        init_db(db)
        self._seed_position(db, ticker="SPY", window_id="2026-04-24-0935")

        import main as _main
        orig = _main.settings
        _main.settings = MagicMock()
        _main.settings.alpha_db_path = db
        _main.settings.telegram_bot_token = "9999:TEST"
        _main.settings.ahmed_chat_id = "123"

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.side_effect = Exception("Connection refused")

        try:
            with patch("main._get_alpaca", return_value=mock_alpaca), \
                 patch("requests.post"):
                _main._run_startup_reconciliation()
        finally:
            _main.settings = orig

        # Production position preserved — Alpaca down, can't confirm mismatch
        assert len(get_open_positions(db)) == 1

    def test_reconciliation_survives_exception(self):
        """Startup reconciliation must never raise — always log and continue."""
        import main as _main
        orig = _main.settings
        _main.settings = MagicMock()
        _main.settings.alpha_db_path = "/nonexistent/path.db"
        _main.settings.telegram_bot_token = "9999:TEST"
        _main.settings.ahmed_chat_id = "123"

        try:
            # Should not raise — must swallow and log
            _main._run_startup_reconciliation()
        except Exception as e:
            pytest.fail(f"_run_startup_reconciliation raised unexpectedly: {e}")
        finally:
            _main.settings = orig

    def test_close_stale_position_sets_reason(self):
        """close_stale_position writes the reason string to close_reason column."""
        db = tempfile.mktemp(suffix="-recon-test6.db")
        from database import init_db, get_conn
        init_db(db)
        pos_id = self._seed_position(db, ticker="GME", window_id="test-reason-check")

        from database import close_stale_position
        close_stale_position(db, pos_id, "reconciled_test_window: window_id='test-reason-check'")

        with get_conn(db) as conn:
            row = conn.execute("SELECT status, close_reason FROM positions WHERE id=?", (pos_id,)).fetchone()

        assert row["status"] == "closed"
        assert "reconciled_test_window" in (row["close_reason"] or "")
