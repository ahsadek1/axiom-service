"""
conftest.py — test environment setup for alpha-execution test suite.

Sets all required env vars before any test module imports main or config,
preventing ValueError from load_settings() at import time.
"""
import os
import sys
import pytest

# Add nexus root and alpha-execution dir to path so 'shared' and service modules resolve.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SVC  = os.path.dirname(_HERE)          # alpha-execution/
_ROOT = os.path.dirname(_SVC)           # nexus/
for _p in (_SVC, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Required env vars — set before any import of main / config
# These are fake values safe for testing; no real credentials.
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


import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture(autouse=True)
def _ensure_active_mode():
    """
    Function-scoped: reset SERVICE_MODE to 'active' before every test.
    Patches preflight + Alpaca count check so tests never require real creds.
    """
    with patch("main._preflight_check", return_value=(True, "")), \
         patch("main._run_preflight_retry_loop"), \
         patch("database.count_open_positions_alpaca", return_value=0):
        import main as _m
        _m._SERVICE_MODE = "active"
        yield
    # Restore after test
    import main as _m
    _m._SERVICE_MODE = "active"


@pytest.fixture(autouse=True)
def reset_service_mode():
    """
    Reset alpha-execution global service mode to 'active' before each test.
    
    main.py uses module-level _SERVICE_MODE global. If a previous test leaves the
    app in STANDBY mode, subsequent tests fail. This fixture ensures clean state.
    Also patches _preflight_check to return (True, '') so tests don't need
    real Alpaca credentials to pass the startup preflight gate.
    """
    from unittest.mock import patch, MagicMock
    
    # Mock account response
    mock_acct = {"status": "ACTIVE", "equity": "100000", "cash": "100000"}
    mock_alpaca = MagicMock()
    mock_alpaca.get_account.return_value = mock_acct
    
    with patch("main._preflight_check", return_value=(True, "")),          patch("main.AlpacaClient", return_value=mock_alpaca):
        try:
            import main as _m
            import threading
            with _m._state_lock:
                _m._SERVICE_MODE = "active"
                _m._standby_reason = ""
        except Exception:
            pass
        yield
