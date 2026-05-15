"""
test_g12_config_write_protection.py — G12: Gateway Config Write Protection Tests

Verifies assert_not_gateway_config blocks writes to protected OpenClaw config paths.
Root cause of April 10, 2026 gateway crash (3.5h downtime).
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.config_guard import assert_not_gateway_config, ConfigWriteError, PROTECTED_PATHS


# ─── T1: Writing to openclaw.json raises ConfigWriteError ────────────────────

def test_t1_openclaw_json_raises():
    """T1: Direct write to ~/.openclaw/openclaw.json → ConfigWriteError."""
    protected = os.path.expanduser("~/.openclaw/openclaw.json")
    with pytest.raises(ConfigWriteError):
        assert_not_gateway_config(protected)


# ─── T2: Writing to config.json raises ConfigWriteError ──────────────────────

def test_t2_config_json_raises():
    """T2: Direct write to ~/.openclaw/config.json → ConfigWriteError."""
    protected = os.path.expanduser("~/.openclaw/config.json")
    with pytest.raises(ConfigWriteError):
        assert_not_gateway_config(protected)


# ─── T3: Writing to a normal data file → no error ────────────────────────────

def test_t3_normal_file_passes():
    """T3: Writing to /tmp/nexus_test_output.json → no error raised."""
    assert_not_gateway_config("/tmp/nexus_test_output.json")


# ─── T4: Writing to nexus DB → no error ──────────────────────────────────────

def test_t4_nexus_db_passes():
    """T4: Writing to nexus data DB → no error raised."""
    assert_not_gateway_config("/Users/ahmedsadek/nexus/data/chronicle.db")


# ─── T5: Relative path resolving to openclaw.json raises ─────────────────────

def test_t5_relative_path_to_protected_raises():
    """T5: Relative path that resolves to openclaw.json → ConfigWriteError."""
    # Construct relative path that resolves to the protected file
    protected_abs = os.path.expanduser("~/.openclaw/openclaw.json")
    # Make it relative from current dir
    try:
        rel = os.path.relpath(protected_abs)
        with pytest.raises(ConfigWriteError):
            assert_not_gateway_config(rel)
    except ValueError:
        # On Windows or cross-drive paths, relpath can fail — skip
        pytest.skip("Cannot construct relative path on this platform")


# ─── T6: Error message contains meaningful guidance ──────────────────────────

def test_t6_error_message_mentions_gateway():
    """T6: ConfigWriteError message references config.patch as the correct path."""
    protected = os.path.expanduser("~/.openclaw/openclaw.json")
    with pytest.raises(ConfigWriteError, match="config.patch"):
        assert_not_gateway_config(protected)


# ─── T7: PROTECTED_PATHS contains at least 2 entries ─────────────────────────

def test_t7_protected_paths_has_entries():
    """T7: PROTECTED_PATHS is non-empty and contains the primary config path."""
    assert len(PROTECTED_PATHS) >= 2
    paths_lower = [p.lower() for p in PROTECTED_PATHS]
    assert any("openclaw.json" in p for p in paths_lower)


# ─── T8: ConfigWriteError is a subclass of RuntimeError ─────────────────────

def test_t8_config_write_error_is_runtime_error():
    """T8: ConfigWriteError inherits from RuntimeError for easy catching."""
    assert issubclass(ConfigWriteError, RuntimeError)


# ─── T9: Writing to ~/.openclaw/plugins/ → no error ──────────────────────────

def test_t9_openclaw_plugins_dir_passes():
    """T9: Writing to ~/.openclaw/plugins/foo.json is allowed."""
    assert_not_gateway_config(os.path.expanduser("~/.openclaw/plugins/something.json"))


# ─── T10: Writing to adjacent file does not raise ────────────────────────────

def test_t10_adjacent_file_not_blocked():
    """T10: A file named 'openclaw.json.bak' in same dir is NOT blocked."""
    near = os.path.expanduser("~/.openclaw/openclaw.json.bak")
    assert_not_gateway_config(near)  # should not raise
