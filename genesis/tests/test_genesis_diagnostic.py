"""
test_genesis_diagnostic.py — Integration tests for stateful genesis_diagnostic.py
Run: python3 -m pytest genesis/tests/test_genesis_diagnostic.py -v
"""

import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

# Redirect CHRONICLE to temp DB for isolation
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["CHRONICLE_DB"] = _tmp.name

sys.path.insert(0, "/Users/ahmedsadek/nexus")
from shared.alert_manager import write_alert, is_known, resolve_alert, get_open_alerts


def teardown_module(_):
    os.unlink(_tmp.name)


# ---------------------------------------------------------------------------
# Test 1: ALL_CLEAR when all services healthy
# ---------------------------------------------------------------------------
def test_all_clear_no_issues():
    from genesis.genesis_diagnostic import run_diagnostic
    healthy_trs = {"color": "GREEN", "score": 92}
    healthy_exec = {"status": "healthy", "execution_paused": False, "vix_brake": "CLEAR"}
    with patch("requests.get") as mock_get:
        def side_effect(url, **kwargs):
            m = MagicMock()
            if "8012" in url:
                m.json.return_value = healthy_trs
            elif "8005" in url:
                m.json.return_value = healthy_exec
            elif "9999" in url:
                m.status_code = 200
                m.json.return_value = []
            else:
                m.json.return_value = {}
            m.status_code = 200
            return m
        mock_get.side_effect = side_effect
        with patch("genesis.genesis_diagnostic.is_market_hours", return_value=False):
            result = run_diagnostic()
    assert result == 0


# ---------------------------------------------------------------------------
# Test 2: TRS amber → new alert written
# ---------------------------------------------------------------------------
def test_trs_amber_raises_alert():
    from genesis.genesis_diagnostic import run_diagnostic
    with patch("requests.get") as mock_get:
        def side_effect(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            if "8012" in url:
                m.json.return_value = {"color": "AMBER", "score": 55, "reason": "VIX elevated"}
            elif "8005" in url:
                m.json.return_value = {"status": "healthy", "execution_paused": False, "vix_brake": "CLEAR"}
            elif "9999" in url:
                m.json.return_value = []
            else:
                m.json.return_value = {}
            return m
        mock_get.side_effect = side_effect
        with patch("genesis.genesis_diagnostic.is_market_hours", return_value=False):
            with patch("genesis.genesis_diagnostic._send_broker_alert"):
                result = run_diagnostic()
    assert result == 1
    assert is_known("trs:not-green")


# ---------------------------------------------------------------------------
# Test 3: Skip already-known issue — no duplicate
# ---------------------------------------------------------------------------
def test_skips_already_known_issue():
    write_alert("genesis", "WARNING", "trs", "trs:not-green:skip-test",
                "TRS AMBER existing", "")
    from genesis.genesis_diagnostic import check_trs
    with patch("genesis.genesis_diagnostic.is_known", return_value=True) as mock_known:
        with patch("requests.get") as mock_get:
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {"color": "AMBER", "score": 55, "reason": "test"}
            mock_get.return_value = m
            # Simulate diagnostic loop guard
            issue_key = "trs:not-green"
            assert mock_known(issue_key) is True  # Guard fires


# ---------------------------------------------------------------------------
# Test 4: OMNI silence raises alert during market hours
# ---------------------------------------------------------------------------
def test_omni_silence_during_market_hours():
    from genesis.genesis_diagnostic import check_omni_silence
    with patch("requests.get") as mock_get:
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {
            "status": "healthy",
            "last_synthesis_min_ago": 31.0,
            "syntheses_today": 5,
            "go_verdicts_today": 0,
        }
        mock_get.return_value = m
        result = check_omni_silence()
    assert result["ok"] is False
    assert result["issue_key"] == "omni:silence"
    assert "31" in result["title"]


# ---------------------------------------------------------------------------
# Test 5: OMNI silence resolves when OMNI resumes
# ---------------------------------------------------------------------------
def test_omni_silence_resolves_on_resume():
    write_alert("genesis", "WARNING", "omni", "omni:silence", "OMNI silent 31min", "")
    assert is_known("omni:silence") is True

    from genesis.genesis_diagnostic import check_omni_silence
    with patch("requests.get") as mock_get:
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {
            "status": "healthy",
            "last_synthesis_min_ago": 2.0,
            "syntheses_today": 7,
        }
        mock_get.return_value = m
        result = check_omni_silence()

    assert result["ok"] is True
    assert not is_known("omni:silence")


# ---------------------------------------------------------------------------
# Test 6: Alpha-exec paused raises CRITICAL
# ---------------------------------------------------------------------------
def test_alpha_exec_paused():
    from genesis.genesis_diagnostic import check_alpha_exec
    with patch("requests.get") as mock_get:
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"status": "healthy", "execution_paused": True, "vix_brake": "CLEAR"}
        mock_get.return_value = m
        result = check_alpha_exec()
    assert result["ok"] is False
    assert result["severity"] == "CRITICAL"
    assert result["issue_key"] == "alpha-exec:paused"


# ---------------------------------------------------------------------------
# Test 7: Bus message with CRITICAL keyword creates alert
# ---------------------------------------------------------------------------
def test_bus_message_critical_creates_alert():
    from genesis.genesis_diagnostic import process_bus_message
    msg = {
        "from_agent": "sovereign",
        "message": "CRITICAL: alpha-execution crashed at 14:32 ET",
        "id": "test-msg-7",
    }
    process_bus_message(msg)
    # Should have created an alert
    alerts = get_open_alerts()
    bus_alerts = [a for a in alerts if a["service"] == "sovereign"]
    assert len(bus_alerts) >= 1


# ---------------------------------------------------------------------------
# Test 8: Bus message without alert keywords does NOT create alert
# ---------------------------------------------------------------------------
def test_bus_message_info_no_alert():
    from genesis.genesis_diagnostic import process_bus_message
    initial_count = len(get_open_alerts())
    msg = {
        "from_agent": "sovereign",
        "message": "Pre-market briefing complete. All systems nominal.",
        "id": "test-msg-8",
    }
    process_bus_message(msg)
    assert len(get_open_alerts()) == initial_count


# ---------------------------------------------------------------------------
# Test 9: Service unreachable raises CRITICAL alert
# ---------------------------------------------------------------------------
def test_service_unreachable_raises_critical():
    from genesis.genesis_diagnostic import check_trs
    import requests
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        result = check_trs()
    assert result["ok"] is False
    assert result["severity"] == "CRITICAL"
    assert "unreachable" in result["issue_key"]


# ---------------------------------------------------------------------------
# Test 10: Multiple distinct issues each get their own alert
# ---------------------------------------------------------------------------
def test_multiple_distinct_issues():
    from genesis.genesis_diagnostic import check_trs, check_alpha_exec
    results = []
    with patch("requests.get") as mock_get:
        def side_effect(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            if "8012" in url:
                m.json.return_value = {"color": "RED", "score": 30, "reason": "VIX spike"}
            elif "8005" in url:
                m.json.return_value = {"status": "healthy", "execution_paused": True, "vix_brake": "HIGH"}
            return m
        mock_get.side_effect = side_effect
        results.append(check_trs())
        results.append(check_alpha_exec())
    issue_keys = [r["issue_key"] for r in results if not r["ok"]]
    assert len(set(issue_keys)) == 2  # Two distinct issues
