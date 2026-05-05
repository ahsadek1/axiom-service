"""
test_g14_log_persistence.py — G14: Log Persistence Tests
"""
import logging
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.log_setup import configure_service_logging


# ─── T1: configure_service_logging creates log directory ────────────────────

def test_t1_creates_log_directory(tmp_path):
    """T1: configure_service_logging creates the log directory if it doesn't exist."""
    log_dir = str(tmp_path / "logs" / "test-service")
    assert not os.path.exists(log_dir)
    configure_service_logging("test-service", log_dir=log_dir)
    assert os.path.exists(log_dir)


# ─── T2: log file is created in log_dir ──────────────────────────────────────

def test_t2_log_file_created(tmp_path):
    """T2: Log file named {service_name}.log is created in log_dir."""
    log_dir = str(tmp_path / "logs" / "my-service")
    configure_service_logging("my-service", log_dir=log_dir)
    log_file = os.path.join(log_dir, "my-service.log")
    assert os.path.exists(log_file), f"Expected log file at {log_file}"


# ─── T3: stdout handler is present ───────────────────────────────────────────

def test_t3_stdout_handler_present(tmp_path):
    """T3: Root logger has a StreamHandler after configure_service_logging."""
    from logging import StreamHandler
    log_dir = str(tmp_path / "logs" / "stdout-svc")
    configure_service_logging("stdout-svc", log_dir=log_dir)
    root = logging.getLogger()
    has_stream = any(isinstance(h, StreamHandler) for h in root.handlers)
    assert has_stream, "Root logger should have a StreamHandler for stdout"


# ─── T4: file handler is present ─────────────────────────────────────────────

def test_t4_file_handler_present(tmp_path):
    """T4: Root logger has a TimedRotatingFileHandler after configure_service_logging."""
    from logging.handlers import TimedRotatingFileHandler
    log_dir = str(tmp_path / "logs" / "file-svc")
    configure_service_logging("file-svc", log_dir=log_dir)
    root = logging.getLogger()
    has_file = any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers)
    assert has_file, "Root logger should have a TimedRotatingFileHandler for file output"


# ─── T5: log level is applied correctly ──────────────────────────────────────

def test_t5_log_level_applied(tmp_path):
    """T5: Root logger level matches the requested log level."""
    log_dir = str(tmp_path / "logs" / "level-svc")
    configure_service_logging("level-svc", log_level="WARNING", log_dir=log_dir)
    root = logging.getLogger()
    assert root.level == logging.WARNING


# ─── T6: default INFO level used when not specified ──────────────────────────

def test_t6_default_info_level(tmp_path):
    """T6: Default log level is INFO when log_level is not specified."""
    log_dir = str(tmp_path / "logs" / "default-svc")
    configure_service_logging("default-svc", log_dir=log_dir)
    root = logging.getLogger()
    assert root.level == logging.INFO


# ─── T7: invalid log_dir does not raise (falls back to stdout) ───────────────

def test_t7_invalid_log_dir_does_not_raise():
    """T7: If log_dir is unwritable, configure_service_logging does NOT raise (graceful fallback)."""
    # Use a path that cannot be created (e.g., under /proc on Linux or a protected dir)
    # We patch makedirs to raise to simulate failure
    from unittest.mock import patch
    with patch("os.makedirs", side_effect=PermissionError("permission denied")):
        # Should not raise — falls back to stdout only
        configure_service_logging("error-svc", log_dir="/fake/unwritable/path")


# ─── T8: retention_days passed to file handler backupCount ───────────────────

def test_t8_retention_days_set_as_backup_count(tmp_path):
    """T8: retention_days parameter sets backupCount on the TimedRotatingFileHandler."""
    from logging.handlers import TimedRotatingFileHandler
    log_dir = str(tmp_path / "logs" / "retention-svc")
    configure_service_logging("retention-svc", retention_days=7, log_dir=log_dir)
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert file_handlers, "Should have at least one TimedRotatingFileHandler"
    assert file_handlers[0].backupCount == 7
