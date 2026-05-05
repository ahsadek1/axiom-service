"""
pipeline_client.py — Fire-and-forget trace helper for all Nexus services.

Drop-in usage in any service:

    from shared.pipeline_client import trace_hop

    trace_hop(
        trace_id=pick["trace_id"],
        hop="buffer_accepted",
        service="alpha-buffer",
        ticker=pick["ticker"],
        pathway="alpha",
    )

Contract:
- Never blocks the caller (daemon thread, 2s HTTP timeout)
- Never raises (silently logs and discards all failures)
- Silently no-ops if PIPELINE_SENTINEL_URL is unreachable
- trace_id travels through every Nexus payload as a passthrough field
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("nexus.pipeline_client")

_SENTINEL_URL: str = os.environ.get("PIPELINE_SENTINEL_URL", "http://localhost:8010")
_NEXUS_SECRET: str = os.environ.get("NEXUS_SECRET", "")
_TIMEOUT_S:    int = 2


def trace_hop(
    trace_id: str,
    hop: str,
    service: str,
    ticker: str,
    pathway: str,
    status: str = "ok",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Fire-and-forget pipeline trace call to Pipeline Sentinel.

    Spawns a daemon thread to POST the hop record. Returns immediately.
    If Sentinel is unreachable, the failure is logged at DEBUG level and discarded.
    The calling service is never affected.

    Args:
        trace_id: UUID4 identifying this pick across its full journey.
                  Must originate at Axiom and be passed through every payload.
        hop:      Pipeline stage name. One of:
                  axiom_push | agent_received | buffer_accepted |
                  omni_started | omni_completed | execution_received |
                  alpaca_submitted | alpaca_confirmed
        service:  Name of the calling service. One of:
                  axiom | cipher | atlas | sage | alpha-buffer | prime-buffer |
                  omni | alpha-execution | prime-execution
        ticker:   Stock ticker symbol (e.g. "AAPL").
        pathway:  "alpha" or "prime".
        status:   "ok" | "error" | "timeout" (default "ok").
        metadata: Optional dict of extra context (e.g. {"latency_ms": 340}).
    """
    def _send() -> None:
        try:
            requests.post(
                f"{_SENTINEL_URL}/trace",
                json={
                    "trace_id": trace_id,
                    "hop":      hop,
                    "service":  service,
                    "ticker":   ticker,
                    "pathway":  pathway,
                    "status":   status,
                    "metadata": metadata or {},
                },
                headers={
                    "X-Nexus-Secret": _NEXUS_SECRET,
                    "Content-Type":   "application/json",
                },
                timeout=_TIMEOUT_S,
            )
        except Exception as exc:
            logger.debug("pipeline_client trace failed (non-critical): %s", exc)

    thread = threading.Thread(target=_send, daemon=True, name=f"trace-{hop}")
    thread.start()
