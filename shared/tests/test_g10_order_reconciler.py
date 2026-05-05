"""
test_g10_order_reconciler.py — G10: OSI-Aware Order Fill Reconciler Tests

All Alpaca HTTP calls are mocked via unittest.mock.
"""
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.order_reconciler import (
    FillResult,
    FillStatus,
    OrderReconciler,
    POLL_INTERVAL_SEC,
)

BASE_URL = "https://paper-api.alpaca.markets"
KEY = "test_key"
SECRET = "test_secret"


def _make_reconciler(dry_run: bool = False, poll_interval: float = 0.01, max_wait: float = 0.1) -> OrderReconciler:
    """Helper: create reconciler with fast poll/wait for tests."""
    return OrderReconciler(
        alpaca_base_url=BASE_URL,
        alpaca_key=KEY,
        alpaca_secret=SECRET,
        poll_interval_sec=poll_interval,
        max_wait_sec=max_wait,
        dry_run=dry_run,
    )


# ─── T1: dry_run mode returns CONFIRMED immediately ──────────────────────────

def test_t1_dry_run_returns_confirmed_immediately():
    """T1: In dry_run mode, confirm_fill returns CONFIRMED without any Alpaca calls."""
    reconciler = _make_reconciler(dry_run=True)
    with patch("requests.get") as mock_get:
        result = reconciler.confirm_fill("order-123", "AAPL", 10.0)
    assert result.status == FillStatus.CONFIRMED
    assert result.void_reason is None
    assert result.filled_qty == 10.0
    assert result.elapsed_sec == 0.0
    mock_get.assert_not_called()


# ─── T2: canceled order → VOID:order_canceled ───────────────────────────────

def test_t2_canceled_order_returns_void_order_canceled():
    """T2: Alpaca order status 'canceled' → VOID:order_canceled."""
    reconciler = _make_reconciler()

    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"status": "canceled"}

    with patch("requests.get", return_value=order_resp):
        result = reconciler.confirm_fill("order-456", "TSLA", 5.0)

    assert result.status == FillStatus.VOID
    assert result.void_reason == "VOID:order_canceled"


# ─── T3: rejected order → VOID:order_rejected ───────────────────────────────

def test_t3_rejected_order_returns_void_order_rejected():
    """T3: Alpaca order status 'rejected' → VOID:order_rejected."""
    reconciler = _make_reconciler()

    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"status": "rejected"}

    with patch("requests.get", return_value=order_resp):
        result = reconciler.confirm_fill("order-789", "NVDA", 3.0)

    assert result.status == FillStatus.VOID
    assert result.void_reason == "VOID:order_rejected"


# ─── T4: position confirmed with qty == expected → CONFIRMED ─────────────────

def test_t4_position_confirmed_returns_confirmed():
    """T4: Position found with expected qty → CONFIRMED with fill_price."""
    reconciler = _make_reconciler(poll_interval=0.01, max_wait=5.0)

    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"status": "filled"}

    position_resp = MagicMock()
    position_resp.status_code = 200
    position_resp.json.return_value = {"qty": "10.0", "avg_entry_price": "150.25"}

    with patch("requests.get", side_effect=[order_resp, position_resp]):
        result = reconciler.confirm_fill("order-001", "AAPL", 10.0)

    assert result.status == FillStatus.CONFIRMED
    assert result.fill_price == pytest.approx(150.25)
    assert result.filled_qty == pytest.approx(10.0)


# ─── T5: timeout → VOID:confirmation_timeout ────────────────────────────────

def test_t5_timeout_returns_void_timeout():
    """T5: Position never appears within max_wait → VOID:confirmation_timeout."""
    reconciler = _make_reconciler(poll_interval=0.01, max_wait=0.05)

    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"status": "new"}

    # Position always 404 (not yet filled)
    position_resp = MagicMock()
    position_resp.status_code = 404

    with patch("requests.get", side_effect=lambda *args, **kwargs: (
        order_resp if "/orders/" in args[0] else position_resp
    )):
        result = reconciler.confirm_fill("order-002", "SPY", 20.0)

    assert result.status == FillStatus.VOID
    assert result.void_reason == "VOID:confirmation_timeout"


# ─── T6: API error on every poll → VOID:alpaca_api_error ─────────────────────

def test_t6_api_errors_exceed_retries_returns_void_api_error():
    """T6: Alpaca API raises exception on every poll → VOID:alpaca_api_error."""
    reconciler = _make_reconciler(poll_interval=0.01, max_wait=5.0)

    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"status": "new"}

    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return order_resp  # first call for order status
        raise ConnectionError("Alpaca unreachable")

    with patch("requests.get", side_effect=side_effect):
        result = reconciler.confirm_fill("order-003", "QQQ", 15.0)

    assert result.status == FillStatus.VOID
    assert result.void_reason == "VOID:alpaca_api_error"


# ─── T7: partial fill → VOID:partial_fill ────────────────────────────────────

def test_t7_partial_fill_returns_void_partial_fill():
    """T7: Position qty deviates more than 5% from expected → VOID:partial_fill."""
    reconciler = _make_reconciler(poll_interval=0.01, max_wait=5.0)

    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"status": "partially_filled"}

    # Only 5 shares filled when 10 expected (50% deviation, well over 5%)
    position_resp = MagicMock()
    position_resp.status_code = 200
    position_resp.json.return_value = {"qty": "5.0", "avg_entry_price": "100.00"}

    with patch("requests.get", side_effect=[order_resp, position_resp]):
        result = reconciler.confirm_fill("order-004", "MSFT", 10.0)

    assert result.status == FillStatus.VOID
    assert result.void_reason == "VOID:partial_fill"
    assert result.filled_qty == pytest.approx(5.0)


# ─── T8: FillResult dataclass fields are correct ─────────────────────────────

def test_t8_fill_result_fields_correct():
    """T8: FillResult dataclass has all expected fields with correct types."""
    result = FillResult(
        status=FillStatus.CONFIRMED,
        void_reason=None,
        fill_price=123.45,
        filled_qty=10.0,
        elapsed_sec=1.5,
        order_id="abc-123",
        ticker="AAPL",
    )
    assert result.status == FillStatus.CONFIRMED
    assert result.void_reason is None
    assert result.fill_price == 123.45
    assert result.filled_qty == 10.0
    assert result.elapsed_sec == 1.5
    assert result.order_id == "abc-123"
    assert result.ticker == "AAPL"


# ─── T9: cancelled (British spelling) also triggers VOID:order_canceled ──────

def test_t9_cancelled_british_spelling_also_void():
    """T9: Alpaca order status 'cancelled' (British) → VOID:order_canceled."""
    reconciler = _make_reconciler()

    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"status": "cancelled"}

    with patch("requests.get", return_value=order_resp):
        result = reconciler.confirm_fill("order-005", "AMZN", 2.0)

    assert result.status == FillStatus.VOID
    assert result.void_reason == "VOID:order_canceled"
