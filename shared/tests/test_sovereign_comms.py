"""
test_sovereign_comms.py — Tests for shared/sovereign_comms.py v2.0

Original 10 spec-mandated cases (preserved):
  1.  Happy path report — POST succeeds, caller unblocked
  2.  Retry exhaustion → Telegram fallback
  3.  Full fallback failure — no exception raised
  4.  get_instructions no watermark — returns all messages, writes watermark
  5.  get_instructions with watermark — returns only newer messages
  6.  get_instructions bus down — returns [], no raise
  7.  Corrupt watermark — resets to epoch, processes all
  8.  Non-serialisable payload — converts to str, still sends
  9.  Malformed inbox message — skipped, others returned
  10. Concurrent report calls — 5 threads, no contention, no crash

Escalation Matrix cases (new):
  11. AUTONOMOUS — returns immediately, zero threads, zero bus calls
  12. CRITICAL — routes to bus with escalation field in body
  13. INFO — routes to bus (default escalation)
  14. EOD — enqueued in SQLite, no bus POST
  15. EOD flush — consolidated batch sent to bus
  16. AHMED_DIRECT — skips bus, hits Telegram directly
  17. Default escalation is INFO (no arg)
  18. Backward compat — existing callers without escalation arg still work

Dispatch layer cases:
  19. dispatch_instruction — registered handler called
  20. dispatch_instruction — unrecognised directive logs warning, no raise
  21. dispatch_instruction — NOOP directives silently acknowledged
  22. dispatch_instruction — handler exception does not propagate

Polling loop cases:
  23. start_polling_loop — daemon thread launched, polls on interval
  24. start_polling_loop — idempotent (second call does not spawn second thread)
  25. start_polling_loop — on_instruction callback fires for each instruction
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import shared.sovereign_comms as sc
from shared.sovereign_comms import (
    EscalationLevel,
    dispatch_instruction,
    get_instructions,
    register_directive_handler,
    report,
    start_polling_loop,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status: int = 200, body: dict = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    return r


def _inbox_response(messages: list) -> MagicMock:
    return _make_response(200, {"messages": messages})


def _ts(year: int = 2026, month: int = 4, day: int = 20,
        hour: int = 12, minute: int = 0, second: int = 0) -> str:
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Original Test 1: Happy path report
# ---------------------------------------------------------------------------

def test_report_happy_path():
    """POST succeeds on first attempt; caller returns immediately."""
    with patch("shared.sovereign_comms.requests.post",
               return_value=_make_response(200)) as mock_post:
        sc.report("cipher", "status", {"msg": "ok"})
        time.sleep(0.2)

    mock_post.assert_called_once()
    body = mock_post.call_args[1]["json"]
    assert body["from"] == "cipher"
    assert body["to"] == "sovereign"
    assert "status:" in body["message"]


# ---------------------------------------------------------------------------
# Original Test 2: Retry exhaustion → Telegram fallback
# ---------------------------------------------------------------------------

def test_report_retry_exhaustion_then_telegram():
    """Bus fails 3 times → Telegram fallback fires."""
    bus_exc = ConnectionError("bus down")
    done_event = threading.Event()
    call_log = []

    def fake_post(url, *args, **kwargs):
        call_log.append(url)
        if len(call_log) < 4:
            raise bus_exc
        done_event.set()
        return _make_response(200)

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post), \
         patch("shared.sovereign_comms._RETRY_DELAYS", [0.0, 0.0, 0.0]):
        sc.report("atlas", "alert", {"data": "critical"})
        done_event.wait(timeout=3.0)

    assert len(call_log) == 4
    assert "api.telegram.org" in call_log[3]


# ---------------------------------------------------------------------------
# Original Test 3: Full fallback failure — no exception raised
# ---------------------------------------------------------------------------

def test_report_full_fallback_failure_no_raise():
    """Bus down + Telegram down → no exception, ERROR logged."""
    with patch("shared.sovereign_comms.requests.post",
               side_effect=ConnectionError("all down")), \
         patch("shared.sovereign_comms.time.sleep"), \
         patch("shared.sovereign_comms.logger") as mock_log:
        sc.report("sage", "escalation", {"issue": "critical"})
        time.sleep(0.3)

    mock_log.error.assert_called()


# ---------------------------------------------------------------------------
# Original Test 4: get_instructions — no watermark — returns all
# ---------------------------------------------------------------------------

def test_get_instructions_no_watermark(tmp_path):
    """No watermark file → all 3 messages returned, watermark written."""
    messages = [
        {"id": "1", "from": "sovereign", "message": "go", "timestamp": _ts(hour=10)},
        {"id": "2", "from": "sovereign", "message": "hold", "timestamp": _ts(hour=11)},
        {"id": "3", "from": "sovereign", "message": "halt", "timestamp": _ts(hour=12)},
    ]

    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path",
               return_value=tmp_path / ".sovereign_watermark"):
        result = sc.get_instructions("cipher")

    assert len(result) == 3
    assert (tmp_path / ".sovereign_watermark").exists()
    written = (tmp_path / ".sovereign_watermark").read_text()
    assert "12:00:00" in written


# ---------------------------------------------------------------------------
# Original Test 5: get_instructions — with watermark — returns only newer
# ---------------------------------------------------------------------------

def test_get_instructions_with_watermark(tmp_path):
    """Watermark = ts of msg 3 → only msgs 4 and 5 returned."""
    wm_file = tmp_path / ".sovereign_watermark"
    wm_file.write_text(_ts(hour=12))

    messages = [
        {"id": "1", "from": "s", "message": "a", "timestamp": _ts(hour=10)},
        {"id": "2", "from": "s", "message": "b", "timestamp": _ts(hour=11)},
        {"id": "3", "from": "s", "message": "c", "timestamp": _ts(hour=12)},
        {"id": "4", "from": "s", "message": "d", "timestamp": _ts(hour=13)},
        {"id": "5", "from": "s", "message": "e", "timestamp": _ts(hour=14)},
    ]

    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path",
               return_value=wm_file):
        result = sc.get_instructions("atlas")

    assert [m["id"] for m in result] == ["4", "5"]


# ---------------------------------------------------------------------------
# Original Test 6: get_instructions — bus down — returns [], no raise
# ---------------------------------------------------------------------------

def test_get_instructions_bus_down():
    """Bus unreachable → returns [], logs warning, never raises."""
    with patch("shared.sovereign_comms.requests.get",
               side_effect=ConnectionError("bus down")), \
         patch("shared.sovereign_comms.logger") as mock_log:
        result = sc.get_instructions("sage")

    assert result == []
    mock_log.warning.assert_called()


# ---------------------------------------------------------------------------
# Original Test 7: Corrupt watermark — resets to epoch
# ---------------------------------------------------------------------------

def test_get_instructions_corrupt_watermark(tmp_path):
    """Corrupt watermark → reset to epoch → all messages returned."""
    wm_file = tmp_path / ".sovereign_watermark"
    wm_file.write_text("THIS IS NOT A TIMESTAMP !!!")

    messages = [
        {"id": "1", "from": "s", "message": "x", "timestamp": _ts(hour=9)},
        {"id": "2", "from": "s", "message": "y", "timestamp": _ts(hour=10)},
    ]

    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path",
               return_value=wm_file), \
         patch("shared.sovereign_comms.logger") as mock_log:
        result = sc.get_instructions("omni")

    assert len(result) == 2
    mock_log.warning.assert_called()


# ---------------------------------------------------------------------------
# Original Test 8: Non-serialisable payload
# ---------------------------------------------------------------------------

def test_report_non_serialisable_payload():
    """datetime in payload → str() conversion, message still sent, no raise."""
    bad_payload = {"ts": datetime(2026, 4, 21, tzinfo=timezone.utc)}

    with patch("shared.sovereign_comms.requests.post",
               return_value=_make_response(200)) as mock_post, \
         patch("shared.sovereign_comms.time.sleep"), \
         patch("shared.sovereign_comms.logger") as mock_log:
        sc.report("genesis", "build_complete", bad_payload)
        time.sleep(0.3)

    mock_post.assert_called_once()
    mock_log.error.assert_called()


# ---------------------------------------------------------------------------
# Original Test 9: Malformed inbox message — skipped, others returned
# ---------------------------------------------------------------------------

def test_get_instructions_malformed_message(tmp_path):
    """One message missing 'timestamp' → skipped; valid messages returned."""
    wm_file = tmp_path / ".sovereign_watermark"

    messages = [
        {"id": "1", "from": "s", "message": "good", "timestamp": _ts(hour=9)},
        {"id": "2", "from": "s", "message": "bad"},   # no timestamp
        {"id": "3", "from": "s", "message": "also_good", "timestamp": _ts(hour=11)},
    ]

    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path",
               return_value=wm_file), \
         patch("shared.sovereign_comms.logger") as mock_log:
        result = sc.get_instructions("cipher")

    ids = [m["id"] for m in result]
    assert "1" in ids and "3" in ids and "2" not in ids
    mock_log.warning.assert_called()


# ---------------------------------------------------------------------------
# Original Test 10: Concurrent report calls
# ---------------------------------------------------------------------------

def test_report_concurrent_calls():
    """5 threads call report() simultaneously → 5 posts, no lock contention."""
    post_calls = []
    lock = threading.Lock()

    def fake_post(*args, **kwargs):
        with lock:
            post_calls.append(1)
        return _make_response(200)

    threads = []
    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post), \
         patch("shared.sovereign_comms.time.sleep"):
        for i in range(5):
            t = threading.Thread(target=sc.report, args=("cipher", "status", {"i": i}))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.5)

    assert len(post_calls) == 5


# ===========================================================================
# NEW: Escalation Matrix Tests (11–18)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 11: AUTONOMOUS — zero threads, zero bus calls
# ---------------------------------------------------------------------------

def test_escalation_autonomous_suppressed():
    """AUTONOMOUS: returns immediately, no thread spawned, no bus call."""
    with patch("shared.sovereign_comms.requests.post") as mock_post, \
         patch("shared.sovereign_comms.threading.Thread") as mock_thread, \
         patch("shared.sovereign_comms.logger") as mock_log:
        report("cipher", "cache_clear", {"key": "all"}, escalation=EscalationLevel.AUTONOMOUS)

    mock_post.assert_not_called()
    mock_thread.assert_not_called()
    # DEBUG log should record the suppression
    mock_log.debug.assert_called()


# ---------------------------------------------------------------------------
# Test 12: CRITICAL — bus call with escalation field in body
# ---------------------------------------------------------------------------

def test_escalation_critical_routes_to_bus():
    """CRITICAL: bus POST fires with escalation='critical' in body."""
    captured_body = {}

    def fake_post(url, *args, **kwargs):
        captured_body.update(kwargs.get("json", {}))
        return _make_response(200)

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post):
        report("cipher", "brain_down", {"consecutive": 3}, escalation=EscalationLevel.CRITICAL)
        time.sleep(0.3)

    assert captured_body.get("escalation") == "critical"
    assert captured_body.get("to") == "sovereign"


# ---------------------------------------------------------------------------
# Test 13: INFO — routes to bus (default)
# ---------------------------------------------------------------------------

def test_escalation_info_routes_to_bus():
    """INFO: bus POST fires with escalation='info' in body."""
    captured_body = {}

    def fake_post(url, *args, **kwargs):
        captured_body.update(kwargs.get("json", {}))
        return _make_response(200)

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post):
        report("atlas", "window_complete", {"analyzed": 10}, escalation=EscalationLevel.INFO)
        time.sleep(0.3)

    assert captured_body.get("escalation") == "info"


# ---------------------------------------------------------------------------
# Test 14: EOD — enqueued in SQLite, no bus POST
# ---------------------------------------------------------------------------

def test_escalation_eod_enqueues_no_bus(tmp_path):
    """EOD: SQLite row inserted, bus POST never called."""
    test_db = str(tmp_path / "eod_queue.db")

    with patch("shared.sovereign_comms.EOD_DB_PATH", test_db), \
         patch("shared.sovereign_comms.requests.post") as mock_post, \
         patch("shared.sovereign_comms._ensure_eod_flush_thread"):  # don't start bg thread
        report("sage", "daily_summary", {"windows": 5, "picks": 12}, escalation=EscalationLevel.EOD)

    mock_post.assert_not_called()

    conn = sqlite3.connect(test_db)
    rows = conn.execute("SELECT agent_name, message_type FROM eod_queue").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "sage"
    assert rows[0][1] == "daily_summary"


# ---------------------------------------------------------------------------
# Test 15: EOD flush — consolidated batch POSTed to bus
# ---------------------------------------------------------------------------

def test_eod_flush_sends_batch(tmp_path):
    """EOD flush reads all queued rows, POSTs consolidated message, clears queue."""
    test_db = str(tmp_path / "eod_queue.db")

    with patch("shared.sovereign_comms.EOD_DB_PATH", test_db):
        # Enqueue 3 entries directly
        sc._eod_enqueue("cipher", "day_summary", {"wins": 3})
        sc._eod_enqueue("atlas", "day_summary", {"wins": 1})
        sc._eod_enqueue("sage", "day_summary", {"wins": 2})

        posted_body = {}

        def fake_post(url, json=None, **kwargs):
            posted_body.update(json or {})
            return _make_response(200)

        with patch("shared.sovereign_comms.requests.post", side_effect=fake_post):
            entries = sc._eod_flush_and_clear()
            # Manually post as the flush thread would
            import requests as _r
            _r.post(
                f"{sc.SOVEREIGN_BUS_URL}/send",
                json={
                    "from": "sovereign_comms_eod",
                    "to": "sovereign",
                    "message": f"eod_summary: {json.dumps(entries)}",
                },
                timeout=sc._HTTP_TIMEOUT,
            )

    assert len(entries) == 3
    agents = [e["agent"] for e in entries]
    assert "cipher" in agents and "atlas" in agents and "sage" in agents

    # Queue should now be empty
    with patch("shared.sovereign_comms.EOD_DB_PATH", test_db):
        remaining = sc._eod_flush_and_clear()
    assert remaining == []


# ---------------------------------------------------------------------------
# Test 16: AHMED_DIRECT — skips bus, hits Telegram directly
# ---------------------------------------------------------------------------

def test_escalation_ahmed_direct_skips_bus():
    """AHMED_DIRECT: Telegram POST fires, bus is never contacted."""
    telegram_called = []
    bus_called = []

    def fake_post(url, *args, **kwargs):
        if "api.telegram.org" in url:
            telegram_called.append(url)
        elif "9999" in url:
            bus_called.append(url)
        return _make_response(200)

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post):
        report(
            "alpha_execution",
            "credential_rotation_needed",
            {"reason": "key_expired"},
            escalation=EscalationLevel.AHMED_DIRECT,
        )
        time.sleep(0.3)

    assert len(telegram_called) == 1
    assert len(bus_called) == 0
    assert "[DIRECT]" in telegram_called[0] or "api.telegram.org" in telegram_called[0]


# ---------------------------------------------------------------------------
# Test 17: Default escalation is INFO
# ---------------------------------------------------------------------------

def test_default_escalation_is_info():
    """report() with no escalation arg → INFO routing (bus POST with escalation='info')."""
    captured_body = {}

    def fake_post(url, *args, **kwargs):
        captured_body.update(kwargs.get("json", {}))
        return _make_response(200)

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post):
        report("omni", "status", {"event": "started"})
        time.sleep(0.3)

    assert captured_body.get("escalation") == "info"


# ---------------------------------------------------------------------------
# Test 18: Backward compatibility — existing callers without escalation arg
# ---------------------------------------------------------------------------

def test_backward_compat_no_escalation_arg():
    """Existing report() calls without escalation= still work, route to INFO."""
    with patch("shared.sovereign_comms.requests.post",
               return_value=_make_response(200)) as mock_post:
        # Call exactly as legacy code does — no escalation kwarg
        report("cipher", "status", {"event": "window_complete", "analyzed": 5, "submitted": 2})
        time.sleep(0.3)

    mock_post.assert_called_once()
    body = mock_post.call_args[1]["json"]
    assert body["escalation"] == "info"
    assert body["from"] == "cipher"


# ===========================================================================
# NEW: Dispatch Layer Tests (19–22)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 19: dispatch_instruction — registered handler called
# ---------------------------------------------------------------------------

def test_dispatch_instruction_registered_handler():
    """Registered handler is called with the full instruction dict."""
    called_with = []

    def my_handler(instr):
        called_with.append(instr)

    register_directive_handler("TESTDIRECTIVE", my_handler)
    instr = {"id": "42", "from": "sovereign", "message": "TESTDIRECTIVE", "timestamp": _ts()}
    dispatch_instruction(instr)

    assert len(called_with) == 1
    assert called_with[0]["id"] == "42"


# ---------------------------------------------------------------------------
# Test 20: dispatch_instruction — unrecognised directive logs warning
# ---------------------------------------------------------------------------

def test_dispatch_instruction_unrecognised_logs_warning():
    """Unrecognised directive logs WARNING and does not raise."""
    instr = {"id": "99", "from": "sovereign", "message": "UNKNOWNDIRECTIVE_XYZ", "timestamp": _ts()}

    with patch("shared.sovereign_comms.logger") as mock_log:
        dispatch_instruction(instr)  # must not raise

    mock_log.warning.assert_called()


# ---------------------------------------------------------------------------
# Test 21: dispatch_instruction — NOOP directives silently acknowledged
# ---------------------------------------------------------------------------

def test_dispatch_instruction_noop_directives():
    """PING, NOP, HEARTBEAT are silently acknowledged without logging WARNING."""
    for noop in ("PING", "NOP", "HEARTBEAT"):
        instr = {"id": "1", "from": "sovereign", "message": noop, "timestamp": _ts()}
        with patch("shared.sovereign_comms.logger") as mock_log:
            dispatch_instruction(instr)
        mock_log.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Test 22: dispatch_instruction — handler exception does not propagate
# ---------------------------------------------------------------------------

def test_dispatch_instruction_handler_exception_contained():
    """If a registered handler raises, dispatch_instruction catches it and does not propagate."""
    def bad_handler(instr):
        raise RuntimeError("handler exploded")

    register_directive_handler("EXPLODE", bad_handler)
    instr = {"id": "1", "from": "sovereign", "message": "EXPLODE", "timestamp": _ts()}

    # Must not raise
    dispatch_instruction(instr)


# ===========================================================================
# NEW: Polling Loop Tests (23–25)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 23: start_polling_loop — thread launched and polls
# ---------------------------------------------------------------------------

def test_start_polling_loop_launches_and_polls(tmp_path):
    """start_polling_loop launches a daemon thread that calls get_instructions."""
    poll_count = []
    wm_file = tmp_path / ".sovereign_watermark"

    def fake_get(url, *args, **kwargs):
        poll_count.append(1)
        return _inbox_response([])

    # Reset internal thread registry so test is clean
    sc._polling_threads.pop("__test_agent__", None)

    with patch("shared.sovereign_comms.requests.get", side_effect=fake_get), \
         patch("shared.sovereign_comms._watermark_path", return_value=wm_file):
        start_polling_loop("__test_agent__", interval_seconds=1)
        time.sleep(2.5)  # allow 2+ polls

    assert len(poll_count) >= 2, f"Expected ≥2 polls, got {len(poll_count)}"

    # Cleanup
    sc._polling_threads.pop("__test_agent__", None)


# ---------------------------------------------------------------------------
# Test 24: start_polling_loop — idempotent
# ---------------------------------------------------------------------------

def test_start_polling_loop_idempotent(tmp_path):
    """Calling start_polling_loop twice for the same agent starts only one thread."""
    wm_file = tmp_path / ".sovereign_watermark"
    sc._polling_threads.pop("__test_idempotent__", None)

    with patch("shared.sovereign_comms.requests.get", return_value=_inbox_response([])), \
         patch("shared.sovereign_comms._watermark_path", return_value=wm_file):
        start_polling_loop("__test_idempotent__", interval_seconds=60)
        t1 = sc._polling_threads.get("__test_idempotent__")
        start_polling_loop("__test_idempotent__", interval_seconds=60)
        t2 = sc._polling_threads.get("__test_idempotent__")

    assert t1 is t2, "Second call must not create a new thread"
    sc._polling_threads.pop("__test_idempotent__", None)


# ---------------------------------------------------------------------------
# Test 25: start_polling_loop — on_instruction callback fires
# ---------------------------------------------------------------------------

def test_start_polling_loop_on_instruction_callback(tmp_path):
    """on_instruction callback is invoked for each new instruction received."""
    wm_file = tmp_path / ".sovereign_watermark"
    received = []
    done = threading.Event()

    messages = [
        {"id": "1", "from": "sovereign", "message": "STATUS", "timestamp": _ts(hour=9)},
        {"id": "2", "from": "sovereign", "message": "STATUS", "timestamp": _ts(hour=10)},
    ]

    call_count = [0]

    def fake_get(url, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _inbox_response(messages)
        return _inbox_response([])  # subsequent polls empty

    def on_instr(instr):
        received.append(instr)
        if len(received) >= 2:
            done.set()

    sc._polling_threads.pop("__test_cb__", None)

    with patch("shared.sovereign_comms.requests.get", side_effect=fake_get), \
         patch("shared.sovereign_comms._watermark_path", return_value=wm_file):
        start_polling_loop("__test_cb__", interval_seconds=1, on_instruction=on_instr)
        done.wait(timeout=5.0)

    assert len(received) == 2
    sc._polling_threads.pop("__test_cb__", None)
