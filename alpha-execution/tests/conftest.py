"""
conftest.py — test environment setup for alpha-execution test suite.

Sets all required env vars before any test module imports main or config,
preventing ValueError from load_settings() at import time.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Add nexus root and alpha-execution dir to path so 'shared' and service modules resolve.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SVC  = os.path.dirname(_HERE)          # alpha-execution/
_ROOT = os.path.dirname(_SVC)           # nexus/
for _p in (_SVC, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Required env vars — set before any import of main / config
# ---------------------------------------------------------------------------
_TEST_ENV = {
    "NEXUS_WEBHOOK_SECRET":  "x" * 64,
    "ALPHA_EXEC_DB_PATH":    "/tmp/test_alpha_exec.db",
    "ALPACA_API_KEY":        "TEST_ALPACA_KEY",
    "ALPACA_SECRET_KEY":     "TEST_ALPACA_SECRET",
    "ALPHA_BUFFER_URL":      "http://localhost:8002",
    "TELEGRAM_BOT_TOKEN":    "0000000000:TEST_TOKEN_PLACEHOLDER",
    "AHMED_CHAT_ID":         "123456789",
    "AXIOM_URL":             "http://localhost:8001",
    "AXIOM_SECRET":          "test_axiom_secret_" + "x" * 32,
    "NEXUS_AUTO_EXECUTE":    "true",
}
for key, val in _TEST_ENV.items():
    os.environ.setdefault(key, val)


# ---------------------------------------------------------------------------
# Fake Alpaca client — fast, no network, safe for test isolation
# ---------------------------------------------------------------------------
class _FakeAlpaca:
    """Minimal stub — returns empty results, never makes network calls."""
    def get_positions(self): return []
    def get_position(self, symbol): return None
    def get_account(self): return {"status": "ACTIVE", "equity": "100000", "cash": "100000"}
    def get_latest_price(self, ticker): return 500.0
    def place_spread_order(self, **kw): return {"id": "fake-order-001", "status": "accepted"}
    def close_position(self, symbol): return {"id": "fake-close-001"}
    # Returns None so Block 4 (pre-submission existence check) does not skip order placement
    def get_order_by_client_id(self, client_order_id): return None
    _get_order_by_client_id = get_order_by_client_id


@pytest.fixture(autouse=True)
def _test_isolation():
    """
    Function-scoped fixture applied to every test.

    Prevents real Alpaca API calls and startup-induced STANDBY mode from
    contaminating test results. Does NOT mock business logic — only infrastructure.

    Patched:
      - main._preflight_check       → always returns (True, "")
      - main._run_preflight_retry_loop → no-op
      - main._get_alpaca            → returns _FakeAlpaca()
      - main._get_current_vix       → returns 15.0 (CLEAR)
      - main._check_vix_brake       → returns None (no brake)
      - database.count_open_positions_alpaca → returns 0
      - execution_v2.reconcile_on_startup    → no-op
      - shared.sovereign_comms.report       → no-op (prevents live bus writes)
      - execution_healer._notify_sovereign  → no-op (prevents live bus writes)
      - execution_healer._notify_ahmed      → no-op (prevents live Telegram spam)

    NOT patched (intentionally):
      - main._run_startup_reconciliation  — tests that check reconciliation
        behaviour need the real function; they provide their own alpaca mock.
    """
    _fake_alpaca = _FakeAlpaca()

    _ok_result = type("R", (), {"status": "ok", "message": "mocked"})()

    with patch("main._preflight_check", return_value=(True, "")), \
         patch("main._run_preflight_retry_loop"), \
         patch("main._get_alpaca", return_value=_fake_alpaca), \
         patch("main._get_current_vix", return_value=15.0), \
         patch("main._check_vix_brake", return_value=None), \
         patch("database.count_open_positions_alpaca", return_value=0), \
         patch("execution_v2.reconcile_on_startup", return_value=None), \
         patch("main._is_market_hours", return_value=True), \
         patch("shared.api_key_validator.ApiKeyValidator.validate_alpaca",
               return_value=_ok_result), \
         patch("shared.sovereign_comms.report"), \
         patch("execution_healer._notify_sovereign"), \
         patch("execution_healer._notify_ahmed"):
        import main as _m
        # Reset all critical app_state fields before each test
        _m._SERVICE_MODE = "active"
        _m.app_state["first_exec_failed"] = False
        _m.app_state["execution_paused"] = False
        _m.app_state["trades_today"] = 0
        _m.app_state["go_verdicts_today"] = 0
        _m._skipped_tickers.clear()
        _m._ticker_fail_counts.clear()
        # Reset webhook secret to original conftest value (some test modules
        # change os.environ["NEXUS_WEBHOOK_SECRET"] at module load time).
        _CONFTEST_SECRET = _TEST_ENV["NEXUS_WEBHOOK_SECRET"]
        try:
            object.__setattr__(_m.settings, "nexus_webhook_secret", _CONFTEST_SECRET)
        except Exception:
            pass
        # Fresh test DB
        import tempfile, importlib
        _fresh_db = tempfile.mktemp(suffix="-isolation.db")
        os.environ["ALPHA_EXEC_DB_PATH"] = _fresh_db
        try:
            object.__setattr__(_m.settings, "alpha_db_path", _fresh_db)
            from database import init_db
            init_db(_fresh_db)
        except Exception:
            pass
        yield
    # Teardown: restore critical state using the ORIGINAL conftest secret,
    # NOT os.environ (which test modules may have overwritten).
    _CONFTEST_SECRET = _TEST_ENV["NEXUS_WEBHOOK_SECRET"]
    try:
        import main as _m
        _m._SERVICE_MODE = "active"
        _m.app_state["first_exec_failed"] = False
        _m.app_state["execution_paused"] = False
        try:
            object.__setattr__(_m.settings, "nexus_webhook_secret", _CONFTEST_SECRET)
        except Exception:
            pass
    except Exception:
        pass
