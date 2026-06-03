"""
test_sovereign_comms_v2.py — Comprehensive tests for sovereign_comms.py v2

Covers 25 test cases across all public API methods:

REPORT / BUS (1-10) — carried from v1, all still passing
PUSH_DIRECTIVE (11-16) — SOVEREIGN pushes HTTP directives to agents
FLEET_STATUS  (17-20) — parallel agent status queries
BROADCAST     (21-23) — one directive to N agents simultaneously
POLL_AND_EXECUTE (24-25) — convenience wrapper
"""

import json
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import shared.sovereign_comms as sc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status: int = 200, body: dict = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = json.dumps(body or {})
    return r


def _inbox_response(messages: list) -> MagicMock:
    return _make_response(200, {"messages": messages})


def _ts(year=2026, month=4, day=20, hour=12, minute=0, second=0) -> str:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tests 1-10: report() and get_instructions() — full regression from v1
# ---------------------------------------------------------------------------

def test_01_report_happy_path():
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


def test_02_report_retry_exhaustion_then_telegram():
    """Bus fails 3 times → Telegram fallback fires."""
    call_log = []
    done = threading.Event()

    def fake_post(url, *args, **kwargs):
        call_log.append(url)
        if len(call_log) < 4:
            raise ConnectionError("bus down")
        done.set()
        return _make_response(200)

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post), \
         patch("shared.sovereign_comms._RETRY_DELAYS", [0.0, 0.0, 0.0]):
        sc.report("atlas", "alert", {"data": "critical"})
        done.wait(timeout=3.0)

    assert len(call_log) == 4
    assert "api.telegram.org" in call_log[3]


def test_03_report_full_fallback_no_raise():
    """All paths fail → no exception, error logged."""
    with patch("shared.sovereign_comms.requests.post",
               side_effect=ConnectionError("all down")), \
         patch("shared.sovereign_comms.time.sleep"), \
         patch("shared.sovereign_comms.logger") as mock_log:
        sc.report("sage", "escalation", {"issue": "critical"})
        time.sleep(0.3)
    mock_log.error.assert_called()


def test_04_get_instructions_no_watermark(tmp_path):
    """No watermark → all messages returned, watermark written."""
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


def test_05_get_instructions_with_watermark(tmp_path):
    """Watermark at msg 3 → only msgs 4 and 5 returned."""
    wm = tmp_path / ".sovereign_watermark"
    wm.write_text(_ts(hour=12))
    messages = [
        {"id": str(i), "from": "s", "message": "x", "timestamp": _ts(hour=9+i)}
        for i in range(6)
    ]
    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path", return_value=wm):
        result = sc.get_instructions("atlas")
    ids = [m["id"] for m in result]
    assert "4" in ids and "5" in ids
    assert "3" not in ids and "2" not in ids


def test_06_get_instructions_bus_down():
    """Bus unreachable → returns [], logs warning, no raise."""
    with patch("shared.sovereign_comms.requests.get",
               side_effect=ConnectionError("bus down")), \
         patch("shared.sovereign_comms.logger") as mock_log:
        result = sc.get_instructions("sage")
    assert result == []
    mock_log.warning.assert_called()


def test_07_corrupt_watermark_resets_to_epoch(tmp_path):
    """Corrupt watermark → reset to epoch → all messages returned."""
    wm = tmp_path / ".sovereign_watermark"
    wm.write_text("THIS IS NOT A TIMESTAMP")
    messages = [
        {"id": "1", "from": "s", "message": "a", "timestamp": _ts(hour=9)},
        {"id": "2", "from": "s", "message": "b", "timestamp": _ts(hour=10)},
    ]
    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path", return_value=wm), \
         patch("shared.sovereign_comms.logger") as mock_log:
        result = sc.get_instructions("omni")
    assert len(result) == 2
    mock_log.warning.assert_called()


def test_08_non_serialisable_payload():
    """datetime in payload → str() conversion, message still sent."""
    bad_payload = {"ts": datetime(2026, 4, 21, tzinfo=timezone.utc)}
    with patch("shared.sovereign_comms.requests.post",
               return_value=_make_response(200)) as mock_post, \
         patch("shared.sovereign_comms.time.sleep"), \
         patch("shared.sovereign_comms.logger") as mock_log:
        sc.report("genesis", "build_complete", bad_payload)
        time.sleep(0.3)
    mock_post.assert_called_once()
    mock_log.error.assert_called()


def test_09_malformed_inbox_message_skipped(tmp_path):
    """Message missing timestamp → skipped; others returned."""
    wm = tmp_path / ".sovereign_watermark"
    messages = [
        {"id": "1", "from": "s", "message": "good", "timestamp": _ts(hour=9)},
        {"id": "2", "from": "s", "message": "bad"},   # no timestamp
        {"id": "3", "from": "s", "message": "also_good", "timestamp": _ts(hour=11)},
    ]
    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path", return_value=wm), \
         patch("shared.sovereign_comms.logger") as mock_log:
        result = sc.get_instructions("cipher")
    ids = [m["id"] for m in result]
    assert "1" in ids and "3" in ids
    assert "2" not in ids
    mock_log.warning.assert_called()


def test_10_concurrent_report_calls():
    """5 threads call report() simultaneously → 5 sends, no contention."""
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


# ---------------------------------------------------------------------------
# Tests 11-16: push_directive()
# ---------------------------------------------------------------------------

def test_11_push_directive_http_success():
    """HTTP push succeeds → returns ok=True, method=http."""
    fake_resp = _make_response(200, {"ok": True, "directive": "HALT", "halted": True})
    with patch("shared.sovereign_comms.requests.post", return_value=fake_resp):
        result = sc.push_directive("cipher", "HALT")
    assert result["ok"] is True
    assert result["method"] == "http"
    assert result["agent"] == "cipher"


def test_12_push_directive_http_fail_bus_fallback():
    """HTTP push fails → bus fallback → method=bus."""
    call_log = []

    def fake_post(url, *args, **kwargs):
        call_log.append(url)
        if "sovereign/directive" in url:
            raise ConnectionError("HTTP down")
        return _make_response(200)  # bus succeeds

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post), \
         patch("shared.sovereign_comms.time.sleep"):
        result = sc.push_directive("cipher", "STATUS")
    assert result["ok"] is True
    assert result["method"] == "bus"


def test_13_push_directive_all_paths_fail():
    """HTTP and bus both fail → ok=False, method=failed."""
    with patch("shared.sovereign_comms.requests.post",
               side_effect=ConnectionError("all down")), \
         patch("shared.sovereign_comms.time.sleep"):
        result = sc.push_directive("cipher", "HALT")
    assert result["ok"] is False
    assert result["method"] == "failed"


def test_14_push_directive_unknown_agent():
    """Unknown agent → ok=False immediately, no network call."""
    with patch("shared.sovereign_comms.requests.post") as mock_post:
        result = sc.push_directive("nonexistent_agent", "HALT")
    assert result["ok"] is False
    mock_post.assert_not_called()


def test_15_push_directive_with_data_payload():
    """Directive with data payload sends correct body to agent."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        if "sovereign/directive" in url:
            captured.update(json or {})
            return _make_response(200, {"ok": True})
        return _make_response(200)

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post):
        sc.push_directive("cipher", "SET_THRESHOLD", {"value": 75.0})

    assert captured.get("directive") == "SET_THRESHOLD"
    assert captured.get("data") == {"value": 75.0}
    assert captured.get("from") == "sovereign"


def test_16_push_directive_non_200_http_falls_back():
    """HTTP returns 403 → falls back to bus."""
    call_log = []

    def fake_post(url, *args, **kwargs):
        call_log.append(url)
        if "sovereign/directive" in url:
            return _make_response(403)
        return _make_response(200)  # bus succeeds

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post), \
         patch("shared.sovereign_comms.time.sleep"):
        result = sc.push_directive("atlas", "HALT")
    assert result["ok"] is True
    assert result["method"] == "bus"


# ---------------------------------------------------------------------------
# Tests 17-20: fleet_status()
# ---------------------------------------------------------------------------

def test_17_fleet_status_all_agents_respond():
    """All agents reachable → all results ok=True."""
    fake_resp = _make_response(200, {"ok": True, "halted": False, "port": 9001})
    with patch("shared.sovereign_comms.requests.get", return_value=fake_resp):
        results = sc.fleet_status(agents=["cipher", "atlas", "sage"])
    assert all(r["ok"] for r in results.values())
    assert set(results.keys()) == {"cipher", "atlas", "sage"}


def test_18_fleet_status_partial_failure():
    """One agent unreachable → that agent ok=False, others ok=True."""
    call_count = [0]

    def fake_get(url, headers=None, timeout=None):
        call_count[0] += 1
        if "9001" in url:  # cipher
            raise ConnectionError("down")
        return _make_response(200, {"ok": True})

    with patch("shared.sovereign_comms.requests.get", side_effect=fake_get):
        results = sc.fleet_status(agents=["cipher", "atlas"])
    assert results["cipher"]["ok"] is False
    assert results["atlas"]["ok"] is True


def test_19_fleet_status_non_200_response():
    """Agent returns 500 → ok=False with HTTP status in response."""
    with patch("shared.sovereign_comms.requests.get",
               return_value=_make_response(500)):
        results = sc.fleet_status(agents=["omni"])
    assert results["omni"]["ok"] is False
    assert "500" in str(results["omni"]["status"])


def test_20_fleet_status_unknown_agent_not_in_registry():
    """Agent not in AGENT_REGISTRY → ok=False, no network call."""
    with patch("shared.sovereign_comms.requests.get") as mock_get:
        results = sc.fleet_status(agents=["unknown_agent_xyz"])
    assert results["unknown_agent_xyz"]["ok"] is False
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Tests 21-23: broadcast()
# ---------------------------------------------------------------------------

def test_21_broadcast_all_agents_http_success():
    """Broadcast STATUS to 3 agents → all ok=True via HTTP."""
    fake_resp = _make_response(200, {"ok": True, "directive": "STATUS"})
    with patch("shared.sovereign_comms.requests.post", return_value=fake_resp):
        results = sc.broadcast("STATUS", targets=["cipher", "atlas", "sage"])
    assert all(r["ok"] for r in results.values())
    assert set(results.keys()) == {"cipher", "atlas", "sage"}


def test_22_broadcast_partial_failure_logged():
    """One agent fails during broadcast → others still succeed."""
    call_log = []

    def fake_post(url, *args, **kwargs):
        call_log.append(url)
        if "9001" in url:  # cipher fails
            raise ConnectionError("down")
        return _make_response(200, {"ok": True})

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post), \
         patch("shared.sovereign_comms.time.sleep"):
        results = sc.broadcast("HALT", targets=["cipher", "atlas"])
    # atlas should succeed (via HTTP), cipher should fall back to bus (also fails)
    assert results["atlas"]["ok"] is True
    assert results["cipher"]["ok"] is False or results["cipher"]["method"] in ("bus", "failed")


def test_23_broadcast_with_data_payload():
    """Broadcast SET_THRESHOLD with data → all agents receive correct payload."""
    received_bodies = []

    def fake_post(url, json=None, headers=None, timeout=None):
        if json and "directive" in json:
            received_bodies.append(json)
        return _make_response(200, {"ok": True})

    with patch("shared.sovereign_comms.requests.post", side_effect=fake_post):
        sc.broadcast("SET_THRESHOLD", data={"value": 80.0}, targets=["cipher", "sage"])

    # Both agents receive the correct directive and data
    directives = [b.get("directive") for b in received_bodies]
    assert all(d == "SET_THRESHOLD" for d in directives)
    datas = [b.get("data") for b in received_bodies]
    assert all(d == {"value": 80.0} for d in datas)


# ---------------------------------------------------------------------------
# Tests 24-25: poll_and_execute()
# ---------------------------------------------------------------------------

def test_24_poll_and_execute_dispatches_all(tmp_path):
    """poll_and_execute calls dispatch_fn once per instruction."""
    messages = [
        {"id": "1", "from": "sovereign", "message": "HALT", "timestamp": _ts(hour=10)},
        {"id": "2", "from": "sovereign", "message": "STATUS", "timestamp": _ts(hour=11)},
    ]
    dispatched = []

    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path",
               return_value=tmp_path / ".sovereign_watermark"):
        count = sc.poll_and_execute("cipher", lambda instr: dispatched.append(instr))

    assert count == 2
    assert len(dispatched) == 2
    assert dispatched[0]["message"] == "HALT"
    assert dispatched[1]["message"] == "STATUS"


def test_25_poll_and_execute_dispatch_exception_does_not_propagate(tmp_path):
    """dispatch_fn raises for one instruction → others still processed, no crash."""
    messages = [
        {"id": "1", "from": "sovereign", "message": "HALT", "timestamp": _ts(hour=10)},
        {"id": "2", "from": "sovereign", "message": "RESUME", "timestamp": _ts(hour=11)},
    ]
    call_log = []
    call_num = [0]

    def flaky_dispatch(instr):
        call_num[0] += 1
        if call_num[0] == 1:
            raise RuntimeError("dispatch error on first")
        call_log.append(instr)

    with patch("shared.sovereign_comms.requests.get",
               return_value=_inbox_response(messages)), \
         patch("shared.sovereign_comms._watermark_path",
               return_value=tmp_path / ".sovereign_watermark"), \
         patch("shared.sovereign_comms.logger") as mock_log:
        count = sc.poll_and_execute("cipher", flaky_dispatch)

    # First errored (not counted), second succeeded
    assert count == 1
    assert len(call_log) == 1
    assert call_log[0]["message"] == "RESUME"
    mock_log.error.assert_called()
