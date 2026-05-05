"""
config_guard.py — Gateway Config Write Protection

Prevents any Nexus service from directly writing to the OpenClaw gateway config file.
Direct writes to openclaw.json caused the April 10, 2026 gateway crash (3.5 hours down).

Usage:
    from shared.config_guard import assert_not_gateway_config
    assert_not_gateway_config(output_path)  # raises ConfigWriteError if protected
    with open(output_path, "w") as f:
        ...
"""
import os
from typing import List

PROTECTED_PATHS: List[str] = [
    os.path.expanduser("~/.openclaw/openclaw.json"),
    os.path.expanduser("~/.openclaw/config.json"),
]


class ConfigWriteError(RuntimeError):
    """Raised when code attempts to write to a protected OpenClaw config file."""
    pass


def assert_not_gateway_config(path: str) -> None:
    """
    Raise ConfigWriteError if path resolves to a protected OpenClaw config file.

    Resolves symlinks and relative paths before comparison to prevent bypass.
    Call before any file write operation that accepts a dynamic path.

    Args:
        path: Absolute or relative file path being written to.

    Raises:
        ConfigWriteError: If path resolves to a protected file.
    """
    try:
        resolved = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except Exception:
        return  # If path can't be resolved, don't block (fail open is safer here)

    for protected in PROTECTED_PATHS:
        try:
            protected_resolved = os.path.realpath(os.path.abspath(protected))
        except Exception:
            continue
        if resolved == protected_resolved:
            raise ConfigWriteError(
                f"Direct writes to '{path}' are forbidden. "
                "OpenClaw config must only be changed via gateway(action='config.patch'). "
                "Direct writes caused the April 10, 2026 gateway crash (3.5 hours down). "
                "See AGENTS.md §Config Write Protection."
            )
