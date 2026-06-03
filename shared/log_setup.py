"""
log_setup.py — Nexus Service Logging Configuration

Configures structured logging for each Nexus service with simultaneous
stdout and rotating daily file output.

Usage:
    from shared.log_setup import configure_service_logging
    configure_service_logging(
        service_name="atlas",
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        retention_days=int(os.getenv("LOG_RETENTION_DAYS", "30")),
    )
"""
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Optional


def configure_service_logging(
    service_name: str,
    log_level: str = "INFO",
    retention_days: int = 30,
    log_dir: Optional[str] = None,
) -> None:
    """
    Configure logging to write to BOTH stdout AND a rotating daily file simultaneously.

    File rotation: daily at midnight, keeps `retention_days` backup files.
    Rotated files get suffix: .log.YYYY-MM-DD
    Creates the log directory if it does not exist.
    Never raises an exception — file failures fall back to stdout silently.

    Args:
        service_name:    Name of the service (used for directory and file naming).
        log_level:       Logging level string (e.g. 'INFO', 'DEBUG', 'WARNING').
        retention_days:  Number of daily log files to retain (backupCount).
        log_dir:         Directory for log files. Defaults to ./logs/{service_name}.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Clear any existing handlers to avoid duplicate output
    root_logger.handlers.clear()

    # ── stdout handler ────────────────────────────────────────────────────────
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    # ── rotating file handler ────────────────────────────────────────────────
    try:
        if log_dir is None:
            log_dir = os.path.join(os.getcwd(), "logs", service_name)

        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, f"{service_name}.log")
        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when="midnight",
            backupCount=retention_days,
            encoding="utf-8",
            utc=False,
        )
        # Use YYYY-MM-DD suffix on rotation
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        logging.getLogger("nexus.log_setup").info(
            "Logging configured: service=%s level=%s log_dir=%s retention=%d days",
            service_name, log_level, log_dir, retention_days,
        )

    except Exception as e:
        # File logging failure must never crash the service — fall back to stdout only
        logging.getLogger("nexus.log_setup").warning(
            "Log file setup failed for %s (stdout only): %s", service_name, e
        )
