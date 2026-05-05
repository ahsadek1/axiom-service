"""
test_g12_config_guard.py — G12: Gateway Config Write Protection Tests
"""
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.config_guard import (
    PROTECTED_PATHS,
    ConfigWriteError,
    assert_not_gateway_config,
)


# ─── T1: Safe path → no error ────────────────────────────────────────────────

def test_t1_safe_path_does_not_raise():
    """T1: A normal safe output path raises no error."""
    assert_not_gateway_config("/tmp/safe_output.json")


# ─── T2: Direct openclaw.json path → ConfigWriteError ────────────────────────

def test_t2_openclaw_json_raises():
    """T2: Writing to ~/.openclaw/openclaw.json raises ConfigWriteError."""
    protected = os.path.expanduser("~/.openclaw/openclaw.json")
    with pytest.raises(ConfigWriteError) as exc_info:
        assert_not_gateway_config(protected)
    assert "openclaw.json" in str(exc_info.value) or "forbidden" in str(exc_info.value).lower()


# ─── T3: Direct config.json path → ConfigWriteError ─────────────────────────

def test_t3_config_json_raises():
    """T3: Writing to ~/.openclaw/config.json raises ConfigWriteError."""
    protected = os.path.expanduser("~/.openclaw/config.json")
    with pytest.raises(ConfigWriteError) as exc_info:
        assert_not_gateway_config(protected)
    assert "forbidden" in str(exc_info.value).lower() or "config.json" in str(exc_info.value)


# ─── T4: Relative path that resolves to protected → ConfigWriteError ─────────

def test_t4_relative_path_resolving_to_protected_raises():
    """T4: Relative path that resolves to a protected path → ConfigWriteError."""
    home = os.path.expanduser("~")
    # Use a relative traversal that ends up at the protected file
    relative = os.path.join(home, ".openclaw", "..", ".openclaw", "openclaw.json")
    with pytest.raises(ConfigWriteError):
        assert_not_gateway_config(relative)


# ─── T5: Symlink to protected path → ConfigWriteError ────────────────────────

def test_t5_symlink_to_protected_raises(tmp_path):
    """T5: A symlink pointing to the protected file → ConfigWriteError."""
    protected = os.path.expanduser("~/.openclaw/openclaw.json")
    symlink_path = str(tmp_path / "fake_config.json")

    # Only create symlink if the protected file exists, otherwise test the resolution path
    if os.path.exists(protected):
        os.symlink(protected, symlink_path)
        with pytest.raises(ConfigWriteError):
            assert_not_gateway_config(symlink_path)
    else:
        # Can't test real symlink without the target, but verify non-protected symlinks pass
        safe_target = str(tmp_path / "safe_target.json")
        with open(safe_target, "w") as f:
            f.write("{}")
        os.symlink(safe_target, symlink_path)
        assert_not_gateway_config(symlink_path)  # safe symlink should not raise


# ─── T6: Truly safe temp file path → no error ────────────────────────────────

def test_t6_temp_file_path_does_not_raise(tmp_path):
    """T6: A safe temp file path raises no error."""
    safe_path = str(tmp_path / "output.json")
    assert_not_gateway_config(safe_path)  # must not raise


# ─── T7: PROTECTED_PATHS contains at least 2 entries ────────────────────────

def test_t7_protected_paths_has_entries():
    """T7: PROTECTED_PATHS list has at least 2 protected paths defined."""
    assert len(PROTECTED_PATHS) >= 2, (
        f"Expected >= 2 protected paths, got {len(PROTECTED_PATHS)}: {PROTECTED_PATHS}"
    )
    # Both entries should be absolute expanded paths
    for p in PROTECTED_PATHS:
        assert os.path.isabs(p), f"Protected path should be absolute: {p}"
