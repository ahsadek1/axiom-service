"""
audit.py — VECTOR Action Audit Decorator (V7).
Spec: vector-resilience-v1.md v1.1

Enforces audit logging via decorator — no code path can skip it.
Every VECTOR infrastructure action (restart, check) is wrapped automatically.

Records: agent, action_type, target, pre_state, post_state, outcome, elapsed_sec, error, timestamp.
Writes to CHRONICLE. Fail-open — never blocks the decorated function.
Exceptions are re-raised after logging.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Callable

import pytz
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("vector.resilience.audit")

ET = pytz.timezone("America/New_York")
HEALTH_PROBE_TIMEOUT = 3  # seconds — quick probe, not a full verify


def _probe_health(service: str) -> str:
    """
    Quick health probe for a service.

    Used to capture pre/post state around infrastructure actions.
    Accepts service names in "name:port" format or looks up known ports.

    Args:
        service: Service name or "name:port" string.

    Returns:
        "healthy", "degraded", or "unreachable". Never raises.
    """
    # Port lookup for known services
    KNOWN_PORTS = {
        "axiom": 8001, "alpha-buffer": 8002, "prime-buffer": 8003,
        "omni": 8004, "alpha-execution": 8005, "prime-execution": 8006,
        "oracle": 8007, "ails": 8008, "guardian-angel": 8009,
    }

    # Parse "name:port" format
    if ":" in service:
        parts = service.rsplit(":", 1)
        name, port_str = parts[0], parts[1]
        try:
            port = int(port_str)
        except ValueError:
            return "unreachable"
    else:
        name = service
        port = KNOWN_PORTS.get(service)

    if not port:
        return "unreachable"

    try:
        r = requests.get(f"http://localhost:{port}/health", timeout=HEALTH_PROBE_TIMEOUT)
        if r.status_code == 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            status = body.get("status", "MISSING")
            return "healthy" if status in ("healthy", "active") else "degraded"
        return "degraded"
    except Exception:
        return "unreachable"


def _write_action_audit(record: dict) -> None:
    """
    Write an audit record to CHRONICLE.

    Fail-open — CHRONICLE failure is logged but never propagated.
    Also logs locally at INFO level.

    Args:
        record: Structured audit record dict.
    """
    logger.info(
        "AUDIT: action=%s target=%s outcome=%s elapsed=%.2fs",
        record.get("action_type"), record.get("target"),
        record.get("outcome"), record.get("elapsed_sec", 0)
    )
    try:
        sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
        from chronicle_reader import chronicle_write  # type: ignore
        chronicle_write("action_audit_log", record)
    except Exception as e:
        logger.warning("_write_action_audit: CHRONICLE write failed (non-blocking): %s", e)


def audit_action(action_type: str) -> Callable:
    """
    Decorator factory that wraps a VECTOR infrastructure action with audit logging.

    Usage:
        @audit_action(action_type="service_restart")
        def restart_service(service: str, port: int) -> RestartVerification:
            ...

    The decorated function's first arg or `service` kwarg is used as the audit target.
    Captures pre_state and post_state via _probe_health().
    Always writes audit record — even on exception (outcome="failed").
    Re-raises exceptions after logging.

    Args:
        action_type: Category label for this action (e.g. "service_restart", "health_check").

    Returns:
        Decorator function.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Determine target from first arg or 'service' kwarg
            target = kwargs.get("service", args[0] if args else "unknown")
            if not isinstance(target, str):
                target = str(target)

            started     = time.time()
            pre_state   = _probe_health(target)
            outcome     = "failed"
            error: Any  = None
            result: Any = None

            try:
                result  = fn(*args, **kwargs)
                outcome = "recovered"
                return result
            except Exception as e:
                error   = str(e)[:300]
                outcome = "failed"
                raise
            finally:
                elapsed = round(time.time() - started, 2)
                post_state = _probe_health(target)
                _write_action_audit({
                    "agent":       "vector",
                    "action_type": action_type,
                    "target":      target,
                    "pre_state":   pre_state,
                    "post_state":  post_state,
                    "outcome":     outcome,
                    "elapsed_sec": elapsed,
                    "error":       error,
                    "timestamp":   datetime.now(ET).isoformat(),
                })

        return wrapper
    return decorator
