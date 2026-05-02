"""
test_execution_failure_modes.py — Tests for the three April 23 execution failure mode fixes.

Fix A: Post-confirmation position verification
  - After confirm_pending_position(), check Alpaca option leg positions.
  - If both legs are zero qty, close DB position as phantom and return 503.
  - If Alpaca unreachable, leave position open (fail-safe).

Fix B: Idempotency key (client_order_id) on place_spread_order()
  - Same window_id+ticker: 422 → look up existing order, return it.
  - Different window_id+ticker: two separate orders (different client_order_ids).
  - client_order_id truncated cleanly at 48 chars.

Fix C: cancel_pending_position() DELETE verification
  - DELETE succeeds: row gone, no error logged.
  - DELETE misses (row not in pending status): fallback UPDATE to canceled, ERROR logged.
  - Nonexistent ID: no crash.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# All required env vars must be set BEFORE any import of main/config.
# Use setdefault so we don't override values already set by other test modules
# that loaded first (e.g. test_ghost_position_fixes sets NEXUS_WEBHOOK_SECRET).
os.environ.setdefault("ALPHA_EXEC_DB_PATH",   tempfile.mktemp(suffix="-fm-default.db"))
os.environ.setdefault("NEXUS_AUTO_EXECUTE",   "true")
os.environ.setdefault("ALPACA_API_KEY",        "test-alpaca-key")
os.environ.setdefault("ALPACA_SECRET_KEY",     "test-alpaca-secret")
os.environ.setdefault("ALPHA_BUFFER_URL",      "http://localhost:8002")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",    "9999:TESTTOKEN")
os.environ.setdefault("AHMED_CHAT_ID",         "8573754783")
os.environ.setdefault("AXIOM_URL",             "http://localhost:8001")
os.environ.setdefault("AXIOM_SECRET",          "test-axiom-secret")
os.environ.setdefault("NEXUS_WEBHOOK_SECRET",  "test-execution-fm-secret")
# NEXUS_WEBHOOK_SECRET intentionally NOT set here — the integration tests in
# this file override it per-test via os.environ[] assignment + settings reload.

import pytest
from unittest.mock import patch, MagicMock, call


# ═══════════════════════════════════════════════════════════════════════════════
# Fix C: cancel_pending_position() DELETE verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelPendingPositionDeleteVerification:
    """cancel_pending_position() must never silently leave a pending row."""

    def _make_db(self) -> str:
        db = tempfile.mktemp(suffix="-cancel-test.db")
        from database import init_db
        init_db(db)
        return db

    def _seed_pending(self, db: str, ticker: str = "AAPL", status: str = "pending") -> int:
        """Insert a position row with the given status. Returns row id."""
        from database import get_conn
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with get_conn(db) as conn:
            cursor = conn.execute(
                """
                INSERT INTO positions (
                    ticker, direction, pathway, option_type,
                    short_strike, long_strike, expiration_date, dte_at_open,
                    contracts, position_size_usd, window_id, agent_scores,
                    verdict, status, opened_at
                ) VALUES ('AAPL','bullish','P1','put',0,0,'2026-06-20',45,
                          1,2000,'win-001','{}','GO',?,'2026-01-01T00:00:00+00:00')
                """,
                (status,),
            )
        return cursor.lastrowid

    def test_delete_pending_row_leaves_no_trace(self):
        """Deleting a truly pending row leaves no DB trace."""
        from database import cancel_pending_position, get_conn
        db = self._make_db()
        pos_id = self._seed_pending(db, status="pending")

        cancel_pending_position(db, pos_id)

        with get_conn(db) as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        assert row is None, "Pending row must be fully deleted"

    def test_delete_miss_forces_canceled_status(self):
        """If row is not in pending status, fallback UPDATE sets status=canceled."""
        from database import cancel_pending_position, get_conn
        db = self._make_db()
        # Seed as 'open' — not pending, so DELETE will miss
        pos_id = self._seed_pending(db, status="open")

        cancel_pending_position(db, pos_id)

        with get_conn(db) as conn:
            row = conn.execute("SELECT status, close_reason FROM positions WHERE id=?", (pos_id,)).fetchone()

        # Row must exist but be in canceled state
        assert row is not None
        assert row["status"] == "canceled"
        assert row["close_reason"] == "force_canceled_pending_miss"

    def test_delete_miss_logs_error(self, caplog):
        """DELETE miss must log at ERROR level."""
        import logging
        from database import cancel_pending_position
        db = self._make_db()
        pos_id = self._seed_pending(db, status="open")

        with caplog.at_level(logging.ERROR, logger="alpha_exec.database"):
            cancel_pending_position(db, pos_id)

        assert any("pending_miss" in r.message or "cancel_pending_position" in r.message
                   for r in caplog.records if r.levelno >= logging.ERROR)

    def test_nonexistent_id_no_crash(self):
        """Nonexistent position ID must not raise."""
        from database import cancel_pending_position
        db = self._make_db()
        # Should silently succeed (no row to delete, fallback UPDATE also no-ops)
        cancel_pending_position(db, 99999)

    def test_already_closed_row_left_as_closed(self):
        """A 'closed' row must not be touched by the fallback UPDATE."""
        from database import cancel_pending_position, get_conn
        db = self._make_db()
        pos_id = self._seed_pending(db, status="closed")

        cancel_pending_position(db, pos_id)

        with get_conn(db) as conn:
            row = conn.execute("SELECT status FROM positions WHERE id=?", (pos_id,)).fetchone()
        # closed rows must NOT be overwritten to canceled
        assert row["status"] == "closed"


# ═══════════════════════════════════════════════════════════════════════════════
# Fix B: Idempotency key on place_spread_order()
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlaceSpreadOrderIdempotency:
    """place_spread_order() must use client_order_id and handle 422 gracefully."""

    def _make_client(self) -> object:
        from alpaca_client import AlpacaClient
        return AlpacaClient("test-key", "test-secret")

    def test_client_order_id_sent_in_body(self):
        """client_order_id must appear in the POST body sent to Alpaca."""
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "order-abc", "status": "accepted"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = client.place_spread_order(
                legs=[{"symbol": "X", "side": "sell", "ratio_qty": 1}],
                qty=1,
                limit_debit=1.50,
                client_order_id="nexus-2026-04-24-0930-NVDA",
            )

        call_kwargs = mock_post.call_args[1] if mock_post.call_args[1] else {}
        call_json   = mock_post.call_args[0][0] if mock_post.call_args[0] else call_kwargs.get("json", {})
        # Inspect positional+keyword args
        _, kwargs = mock_post.call_args
        sent_body = kwargs.get("json", {})
        assert "client_order_id" in sent_body
        assert sent_body["client_order_id"] == "nexus-2026-04-24-0930-NVDA"
        assert result["id"] == "order-abc"

    def test_client_order_id_truncated_to_48(self):
        """client_order_id > 48 chars must be truncated before sending."""
        client = self._make_client()
        long_id = "nexus-" + "X" * 60  # 66 chars
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "order-xyz", "status": "accepted"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            client.place_spread_order(
                legs=[{"symbol": "X", "side": "sell", "ratio_qty": 1}],
                qty=1,
                client_order_id=long_id,
            )

        _, kwargs = mock_post.call_args
        sent_coid = kwargs["json"]["client_order_id"]
        assert len(sent_coid) <= 48

    def test_no_client_order_id_when_none(self):
        """When client_order_id=None, it must not appear in the POST body."""
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "order-abc", "status": "accepted"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            client.place_spread_order(
                legs=[{"symbol": "X", "side": "sell", "ratio_qty": 1}],
                qty=1,
                client_order_id=None,
            )

        _, kwargs = mock_post.call_args
        assert "client_order_id" not in kwargs["json"]

    def test_422_with_client_order_id_returns_existing_order(self):
        """Alpaca 422 + client_order_id → look up and return the existing order."""
        client = self._make_client()
        existing_order = {"id": "existing-order-123", "status": "accepted", "client_order_id": "nexus-test-NVDA"}

        mock_422 = MagicMock()
        mock_422.status_code = 422
        mock_422.text = "duplicate client_order_id"

        mock_lookup = MagicMock()
        mock_lookup.status_code = 200
        mock_lookup.json.return_value = [existing_order]

        with patch("requests.post", return_value=mock_422), \
             patch("requests.get", return_value=mock_lookup):
            result = client.place_spread_order(
                legs=[{"symbol": "X", "side": "sell", "ratio_qty": 1}],
                qty=1,
                client_order_id="nexus-test-NVDA",
            )

        assert result["id"] == "existing-order-123"

    def test_422_without_client_order_id_raises(self):
        """Alpaca 422 without client_order_id must raise AlpacaError (no lookup)."""
        from alpaca_client import AlpacaError
        client = self._make_client()

        mock_422 = MagicMock()
        mock_422.status_code = 422
        mock_422.text = "unprocessable entity"

        with patch("requests.post", return_value=mock_422):
            with pytest.raises(AlpacaError):
                client.place_spread_order(
                    legs=[{"symbol": "X", "side": "sell", "ratio_qty": 1}],
                    qty=1,
                    client_order_id=None,  # No idempotency key
                )

    def test_422_lookup_fallback_raises_if_existing_not_found(self):
        """If 422 lookup returns no matching order, AlpacaError is raised."""
        from alpaca_client import AlpacaError
        client = self._make_client()

        mock_422 = MagicMock()
        mock_422.status_code = 422
        mock_422.text = "duplicate"

        mock_lookup = MagicMock()
        mock_lookup.status_code = 200
        mock_lookup.json.return_value = []  # No orders returned

        with patch("requests.post", return_value=mock_422), \
             patch("requests.get", return_value=mock_lookup):
            with pytest.raises(AlpacaError):
                client.place_spread_order(
                    legs=[{"symbol": "X", "side": "sell", "ratio_qty": 1}],
                    qty=1,
                    client_order_id="nexus-missing-order",
                )

    def test_main_passes_client_order_id_to_alpaca(self):
        """In the execute handler, place_spread_order must receive a client_order_id."""
        db = tempfile.mktemp(suffix="-idp-test.db")
        os.environ["ALPHA_EXEC_DB_PATH"]   = db
        os.environ["NEXUS_WEBHOOK_SECRET"] = "test-idp-secret"

        import main as _main
        _main.settings = _main.load_settings()
        from database import init_db
        init_db(db)

        captured_kwargs = {}

        def mock_place_spread(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return {"id": "order-idp-001", "status": "accepted"}

        mock_alpaca = MagicMock()
        mock_alpaca.get_latest_price.return_value = 200.0
        mock_alpaca.place_spread_order.side_effect = mock_place_spread
        mock_alpaca.get_option_contracts.return_value = [
            {"strike_price": "170"}, {"strike_price": "180"},
            {"strike_price": "190"}, {"strike_price": "200"},
        ]
        mock_alpaca.get_position.return_value = {"qty": "1"}  # Fix A: non-zero
        # Block 4 pre-submission check: return None so the order proceeds to place_spread_order
        mock_alpaca.get_order_by_client_id.return_value = None
        mock_alpaca._get_order_by_client_id.return_value = None

        mock_fill = MagicMock()
        mock_fill.status.__eq__ = lambda s, o: False  # Not VOID
        mock_fill.fill_price = 200.0

        from shared.order_reconciler import FillStatus
        mock_fill.status = FillStatus.CONFIRMED
        mock_fill.fill_price = 200.0

        from contract_resolver import SpreadParams as _SP
        _mock_spread = _SP(
            underlying="NVDA", direction="bullish", option_type="put",
            short_strike=185.0, long_strike=180.0, expiration_date="2026-05-16",
            target_dte=14, current_price=200.0, is_etf=False,
        )
        with patch("main._get_alpaca", return_value=mock_alpaca), \
             patch("main._is_market_hours", return_value=True), \
             patch("main.AlpacaClient", return_value=mock_alpaca), \
             patch("main.resolve_available_contract", return_value=_mock_spread), \
             patch("shared.order_reconciler.OrderReconciler.confirm_fill", return_value=mock_fill), \
             patch("main._get_current_vix", return_value=15.0), \
             patch("main._check_vix_brake", return_value=None):  # Note: NOT patching requests.post/get - _FakeAlpaca handles Alpaca calls
            from fastapi.testclient import TestClient
            with patch("main._preflight_check", return_value=(True, "")), \
                 patch("main._run_preflight_retry_loop"), \
                 patch("database.count_open_positions_alpaca", return_value=0):
                _main._SERVICE_MODE = "active"
                with TestClient(_main.app) as c:
                    resp = c.post(
                        "/execute",
                        json={
                            "ticker": "NVDA", "direction": "bullish", "pathway": "P1",
                            "weighted_score": 82.0,
                            "agent_scores": {"Cipher": 85, "Atlas": 80, "Sage": 78},
                            "verdict": "STRONG_GO", "sizing_mult": 1.0,
                            "position_size_usd": 2000.0,
                            "window_id": "2026-04-25-0930",
                            "echo_chamber": False,
                        },
                        headers={"X-Nexus-Secret": "test-idp-secret"},
                    )

        # Verify client_order_id was passed
        assert mock_alpaca.place_spread_order.called
        call_kw = mock_alpaca.place_spread_order.call_args[1]
        assert "client_order_id" in call_kw
        coid = call_kw["client_order_id"]
        assert "NVDA" in coid
        assert len(coid) <= 48


# ═══════════════════════════════════════════════════════════════════════════════
# Fix A: Post-confirmation position verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestPostConfirmPositionCheck:
    """After confirm_pending_position(), if Alpaca shows zero leg positions, close the DB position."""

    def test_alpaca_client_get_position_returns_none_for_missing(self):
        """AlpacaClient.get_position() returns None on 404 — contract for Fix A."""
        from alpaca_client import AlpacaClient
        client = AlpacaClient("key", "secret")
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("requests.get", return_value=mock_resp):
            result = client.get_position("NVDA260717P00145000")
        assert result is None

    def test_alpaca_client_get_position_returns_dict_when_found(self):
        """AlpacaClient.get_position() returns the position dict on 200."""
        from alpaca_client import AlpacaClient
        client = AlpacaClient("key", "secret")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"symbol": "NVDA260717P00145000", "qty": "1"}

        with patch("requests.get", return_value=mock_resp):
            result = client.get_position("NVDA260717P00145000")
        assert result is not None
        assert result["qty"] == "1"

    def test_post_confirm_phantom_closes_position(self):
        """
        When both leg positions are zero after fill confirm, DB position is force-closed.
        """
        db = tempfile.mktemp(suffix="-fixA-phantom.db")
        os.environ["ALPHA_EXEC_DB_PATH"]   = db
        os.environ["NEXUS_WEBHOOK_SECRET"] = "test-fixA-secret"

        import main as _main
        _main.settings = _main.load_settings()
        from database import init_db, get_open_positions
        init_db(db)

        mock_alpaca = MagicMock()
        mock_alpaca.get_latest_price.return_value = 200.0
        mock_alpaca.place_spread_order.return_value = {"id": "order-phantom-001", "status": "accepted"}
        mock_alpaca.get_order_by_client_id.return_value = None  # block4: no pre-existing order
        mock_alpaca.get_option_contracts.return_value = [
            {"strike_price": "170"}, {"strike_price": "180"},
            {"strike_price": "190"}, {"strike_price": "200"},
        ]
        # Fix A: both positions are None (not in Alpaca → qty = 0)
        mock_alpaca.get_position.return_value = None

        from shared.order_reconciler import FillStatus
        mock_fill = MagicMock()
        mock_fill.status = FillStatus.CONFIRMED
        mock_fill.fill_price = 200.0

        from contract_resolver import SpreadParams as _SP
        _mock_spread = _SP(
            underlying="NVDA", direction="bullish", option_type="put",
            short_strike=185.0, long_strike=180.0, expiration_date="2026-05-16",
            target_dte=14, current_price=200.0, is_etf=False,
        )
        with patch("main._get_alpaca", return_value=mock_alpaca), \
             patch("main._is_market_hours", return_value=True), \
             patch("main.AlpacaClient", return_value=mock_alpaca), \
             patch("main.resolve_available_contract", return_value=_mock_spread), \
             patch("shared.order_reconciler.OrderReconciler.confirm_fill", return_value=mock_fill), \
             patch("main._get_current_vix", return_value=15.0), \
             patch("main._check_vix_brake", return_value=None):  # Note: NOT patching requests.post/get - _FakeAlpaca handles Alpaca calls
            from fastapi.testclient import TestClient
            with patch("main._preflight_check", return_value=(True, "")), \
                 patch("main._run_preflight_retry_loop"), \
                 patch("database.count_open_positions_alpaca", return_value=0):
                _main._SERVICE_MODE = "active"
                with TestClient(_main.app) as c:
                    resp = c.post(
                        "/execute",
                        json={
                            "ticker": "NVDA", "direction": "bullish", "pathway": "P1",
                            "weighted_score": 82.0,
                            "agent_scores": {"Cipher": 85, "Atlas": 80, "Sage": 78},
                            "verdict": "STRONG_GO", "sizing_mult": 1.0,
                            "position_size_usd": 2000.0,
                            "window_id": "2026-04-25-0935",
                            "echo_chamber": False,
                        },
                        headers={"X-Nexus-Secret": "test-fixA-secret"},
                    )

        # Should return 503 (phantom detected)
        assert resp.status_code == 503
        body = resp.json()
        assert body["executed"] is False
        assert body["gate"] == "post_confirm_position_check"

        # DB must NOT have an open position for NVDA
        assert len(get_open_positions(db)) == 0

    def test_post_confirm_does_not_close_valid_position(self):
        """When Alpaca reports non-zero leg qty, position stays open."""
        db = tempfile.mktemp(suffix="-fixA-valid.db")
        os.environ["ALPHA_EXEC_DB_PATH"]   = db
        os.environ["NEXUS_WEBHOOK_SECRET"] = "test-fixA-valid-secret"

        import main as _main
        _main.settings = _main.load_settings()
        from database import init_db, get_open_positions
        init_db(db)

        mock_alpaca = MagicMock()
        mock_alpaca.get_latest_price.return_value = 200.0
        mock_alpaca.place_spread_order.return_value = {"id": "order-valid-001", "status": "accepted"}
        mock_alpaca.get_order_by_client_id.return_value = None  # block4: no pre-existing order
        mock_alpaca.get_option_contracts.return_value = [
            {"strike_price": "170"}, {"strike_price": "180"},
            {"strike_price": "190"}, {"strike_price": "200"},
        ]
        # Fix A: positions are non-zero → real fill
        mock_alpaca.get_position.return_value = {"qty": "1", "symbol": "NVDA260717P00145000"}

        from shared.order_reconciler import FillStatus
        mock_fill = MagicMock()
        mock_fill.status = FillStatus.CONFIRMED
        mock_fill.fill_price = 200.0

        from contract_resolver import SpreadParams as _SP
        _mock_spread = _SP(
            underlying="NVDA", direction="bullish", option_type="put",
            short_strike=185.0, long_strike=180.0, expiration_date="2026-05-16",
            target_dte=14, current_price=200.0, is_etf=False,
        )
        with patch("main._get_alpaca", return_value=mock_alpaca), \
             patch("main._is_market_hours", return_value=True), \
             patch("main.AlpacaClient", return_value=mock_alpaca), \
             patch("main.resolve_available_contract", return_value=_mock_spread), \
             patch("shared.order_reconciler.OrderReconciler.confirm_fill", return_value=mock_fill), \
             patch("main._get_current_vix", return_value=15.0), \
             patch("main._check_vix_brake", return_value=None):  # Note: NOT patching requests.post/get - _FakeAlpaca handles Alpaca calls
            from fastapi.testclient import TestClient
            with patch("main._preflight_check", return_value=(True, "")), \
                 patch("main._run_preflight_retry_loop"), \
                 patch("database.count_open_positions_alpaca", return_value=0):
                _main._SERVICE_MODE = "active"
                with TestClient(_main.app) as c:
                    resp = c.post(
                        "/execute",
                        json={
                            "ticker": "MSFT", "direction": "bearish", "pathway": "P1",
                            "weighted_score": 80.0,
                            "agent_scores": {"Cipher": 82, "Atlas": 79, "Sage": 77},
                            "verdict": "GO", "sizing_mult": 1.0,
                            "position_size_usd": 2000.0,
                            "window_id": "2026-04-25-0940",
                            "echo_chamber": False,
                        },
                        headers={"X-Nexus-Secret": "test-fixA-valid-secret"},
                    )

        # Should return 200 — real position confirmed
        assert resp.status_code == 200
        assert resp.json()["executed"] is True
        assert len(get_open_positions(db)) == 1

    def test_post_confirm_alpaca_unreachable_leaves_position_open(self):
        """If Alpaca is unreachable during position check, position stays open (fail-safe)."""
        db = tempfile.mktemp(suffix="-fixA-down.db")
        os.environ["ALPHA_EXEC_DB_PATH"]   = db
        os.environ["NEXUS_WEBHOOK_SECRET"] = "test-fixA-down-secret"

        import main as _main
        _main.settings = _main.load_settings()
        from database import init_db, get_open_positions
        init_db(db)

        mock_alpaca = MagicMock()
        mock_alpaca.get_latest_price.return_value = 200.0
        mock_alpaca.place_spread_order.return_value = {"id": "order-down-001", "status": "accepted"}
        mock_alpaca.get_order_by_client_id.return_value = None  # block4: no pre-existing order
        mock_alpaca.get_option_contracts.return_value = [
            {"strike_price": "170"}, {"strike_price": "180"},
            {"strike_price": "190"}, {"strike_price": "200"},
        ]
        # Fix A: get_position raises (Alpaca down)
        mock_alpaca.get_position.side_effect = Exception("Connection refused")

        from shared.order_reconciler import FillStatus
        mock_fill = MagicMock()
        mock_fill.status = FillStatus.CONFIRMED
        mock_fill.fill_price = 200.0

        from contract_resolver import SpreadParams as _SP
        _mock_spread = _SP(
            underlying="NVDA", direction="bullish", option_type="put",
            short_strike=185.0, long_strike=180.0, expiration_date="2026-05-16",
            target_dte=14, current_price=200.0, is_etf=False,
        )
        with patch("main._get_alpaca", return_value=mock_alpaca), \
             patch("main._is_market_hours", return_value=True), \
             patch("main.AlpacaClient", return_value=mock_alpaca), \
             patch("main.resolve_available_contract", return_value=_mock_spread), \
             patch("shared.order_reconciler.OrderReconciler.confirm_fill", return_value=mock_fill), \
             patch("main._get_current_vix", return_value=15.0), \
             patch("main._check_vix_brake", return_value=None):  # Note: NOT patching requests.post/get - _FakeAlpaca handles Alpaca calls
            from fastapi.testclient import TestClient
            with patch("main._preflight_check", return_value=(True, "")), \
                 patch("main._run_preflight_retry_loop"), \
                 patch("database.count_open_positions_alpaca", return_value=0):
                _main._SERVICE_MODE = "active"
                with TestClient(_main.app) as c:
                    resp = c.post(
                        "/execute",
                        json={
                            "ticker": "AAPL", "direction": "bullish", "pathway": "P2",
                            "weighted_score": 78.0,
                            "agent_scores": {"Cipher": 80, "Atlas": 78, "Sage": 76},
                            "verdict": "GO", "sizing_mult": 0.75,
                            "position_size_usd": 1500.0,
                            "window_id": "2026-04-25-0945",
                            "echo_chamber": False,
                        },
                        headers={"X-Nexus-Secret": "test-fixA-down-secret"},
                    )

        # Alpaca down → fail-safe: position stays open, execute succeeds
        assert resp.status_code == 200
        assert resp.json()["executed"] is True
        assert len(get_open_positions(db)) == 1
