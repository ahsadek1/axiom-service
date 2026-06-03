"""
verify.py — VECTOR Restart Verification Contract (V2).
Spec: vector-resilience-v1.md v1.1

Typed RestartVerification dataclass — checks body status, not just HTTP 200.
Per-service timeouts: local services 5s, Railway 120s.
Missing status key is logged as suspicious and treated as degraded (not healthy).
"""

from __future__ import annotations

import logging
import sys
import os
from dataclasses import dataclass
from typing import Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

logger = logging.getLogger("vector.resilience.verify")

# ── Per-service verification timeouts ────────────────────────────────────────
# Local services: 5s. Railway cold starts can take 45-90s → 120s.

SERVICE_VERIFY_TIMEOUT: dict[str, int] = {
    "axiom":           5,
    "alpha-buffer":    5,
    "prime-buffer":    5,
    "omni":            5,
    "alpha-execution": 5,
    "prime-execution": 5,
    "oracle":          5,
    "ails":            5,
    "guardian-angel":  5,
    # Railway services (add with 120s if needed):
    # "alpha-railway": 120,
    # "prime-railway": 120,
}

HEALTHY_STATUSES = {"healthy", "active"}


@dataclass
class RestartVerification:
    """
    Typed result of a service health verification after restart.

    Attributes:
        service:        Service name.
        http_code:      HTTP status code (0 if connection failed).
        body_status:    Value of 'status' field in response body.
                        "MISSING" if key absent, "UNREACHABLE" on connection failure.
        latency_ms:     Round-trip latency in milliseconds.
        is_healthy:     True only if HTTP 200 AND status in ("healthy", "active").
        failure_reason: Human-readable reason if is_healthy=False.
    """
    service:        str
    http_code:      int
    body_status:    str
    latency_ms:     float
    is_healthy:     bool
    failure_reason: str


def verify_service_recovery(name: str, port: int) -> RestartVerification:
    """
    Verify a service is healthy after restart.

    Checks HTTP 200 AND body status field — not just HTTP 200.
    Per-service timeout from SERVICE_VERIFY_TIMEOUT (defaults to 10s if unknown).

    Rules:
    - is_healthy = True ONLY if HTTP 200 AND status in ("healthy", "active")
    - Missing status key: body_status="MISSING", logged as suspicious, is_healthy=False
    - Exception: http_code=0, body_status="UNREACHABLE", is_healthy=False

    Args:
        name: Service name (used for timeout lookup and logging).
        port: Port number to check.

    Returns:
        RestartVerification with full diagnostic context.
    """
    import time
    timeout = SERVICE_VERIFY_TIMEOUT.get(name, 10)
    start = time.time()

    try:
        r = requests.get(f"http://localhost:{port}/health", timeout=timeout)
        latency_ms = (time.time() - start) * 1000

        # Parse body
        body: dict = {}
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                logger.warning("verify_service_recovery: %s returned non-JSON body", name)

        # Extract status field
        if "status" not in body:
            if r.status_code == 200:
                logger.warning(
                    "verify_service_recovery: %s returned HTTP 200 with no 'status' field — suspicious",
                    name
                )
            body_status = "MISSING"
        else:
            body_status = str(body["status"])

        is_healthy = r.status_code == 200 and body_status in HEALTHY_STATUSES
        failure_reason = ""
        if not is_healthy:
            if r.status_code != 200:
                failure_reason = f"HTTP {r.status_code}"
            elif body_status == "MISSING":
                failure_reason = "status field absent from response body"
            else:
                failure_reason = f"status={body_status}"

        return RestartVerification(
            service=name,
            http_code=r.status_code,
            body_status=body_status,
            latency_ms=round(latency_ms, 1),
            is_healthy=is_healthy,
            failure_reason=failure_reason,
        )

    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        return RestartVerification(
            service=name,
            http_code=0,
            body_status="UNREACHABLE",
            latency_ms=round(latency_ms, 1),
            is_healthy=False,
            failure_reason=str(e)[:200],
        )
