"""
omni_client.py — Alpha Buffer → OMNI Dispatcher

Dispatches concordance results to OMNI for synthesis.
Fire-and-forget: Alpha Buffer's job ends at dispatch.
OMNI handles synthesis, GO/NO-GO, and execution routing independently.

HARDENED (GENESIS 2026-05-26): Timeout tuning and diagnostic response capture.
Root cause: Concordance dispatch pipeline was fragile — no timeout increase for OMNI synthesis load.
Two incidents in 1 hour from same root cause → requires architectural hardening.

Fixes:
1. Increased timeout from 10s → 15s (OMNI may take ~10s for synthesis under load)
2. Diagnostic logging captures auth mismatch (403) with response body
3. DOES NOT retry in synchronous dispatch — async retry loop handles persistence
4. Sync dispatch should be non-blocking (no sleep calls) for background thread
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alpha_buffer.omni_client")

DISPATCH_TIMEOUT_SECONDS = 15  # INCREASED from 10s — OMNI synthesis under load


def dispatch_to_omni(
    omni_url:         str,
    auth_headers:     dict[str, str],
    concordance_dict: dict,
) -> tuple[bool, Optional[str]]:
    """
    Dispatch a concordance result to OMNI for synthesis.

    Non-blocking: logs failure and returns False — does NOT block submission response.
    OMNI is expected to return 200/201/202 immediately and process async.

    Does NOT retry in this function — async retry loop in lifespan handles failed dispatches.
    This keeps the background thread non-blocking (no sleep calls).

    Args:
        omni_url:         OMNI service webhook URL.
        auth_headers:     Auth headers to include (X-Nexus-Secret).
        concordance_dict: Full concordance result dict from ConcordanceResult.to_dict().

    Returns:
        Tuple of (success: bool, omni_response_summary: str or None).
    """
    ticker = concordance_dict.get("ticker")
    direction = concordance_dict.get("direction")
    pathway = concordance_dict.get("pathway")

    try:
        resp = requests.post(
            omni_url,
            json    = concordance_dict,
            headers = {**auth_headers, "Content-Type": "application/json"},
            timeout = DISPATCH_TIMEOUT_SECONDS,
        )

        if resp.status_code in (200, 201, 202):
            logger.info(
                "OMNI dispatch success: %s/%s pathway=%s (HTTP %d)",
                ticker, direction, pathway, resp.status_code,
            )
            summary = f"HTTP {resp.status_code}"
            try:
                body    = resp.json()
                summary = body.get("status", summary)
            except Exception:
                pass
            return True, summary

        # 403/401 = auth failure — CRITICAL, needs manual investigation
        if resp.status_code in (401, 403):
            logger.error(
                "🔴 OMNI DISPATCH BLOCKED — AUTH FAILURE (HTTP %d) for %s/%s. "
                "NEXUS_WEBHOOK_SECRET mismatch between Alpha Buffer and OMNI. "
                "This is a configuration error, not transient. "
                "Response: %s",
                resp.status_code, ticker, direction, resp.text[:500],
            )
            return False, f"HTTP {resp.status_code} (auth mismatch — config error)"

        # 4xx (except 403/401) = validation error — likely payload structure
        if 400 <= resp.status_code < 500:
            logger.error(
                "OMNI dispatch rejected (HTTP %d) for %s/%s — validation error. "
                "Payload structure may be incompatible. "
                "Payload: %s | Response: %s",
                resp.status_code, ticker, direction,
                concordance_dict, resp.text[:500],
            )
            return False, f"HTTP {resp.status_code} (validation)"

        # 5xx or other non-2xx — transient, should be retried by async loop
        logger.warning(
            "OMNI dispatch returned %d for %s/%s — transient failure, will be retried",
            resp.status_code, ticker, direction,
        )
        return False, f"HTTP {resp.status_code} (transient)"

    except requests.exceptions.Timeout:
        logger.warning(
            "OMNI dispatch TIMED OUT for %s/%s (limit: %ds) — transient, will be retried by async loop",
            ticker, direction, DISPATCH_TIMEOUT_SECONDS,
        )
        return False, "timeout"

    except Exception as e:
        logger.error(
            "OMNI dispatch error for %s/%s: %s — will be retried by async loop",
            ticker, direction, e,
        )
        return False, f"error: {str(e)[:100]}"
