"""
omni_client.py — Alpha Buffer → OMNI Dispatcher

Dispatches concordance results to OMNI for synthesis.
Fire-and-forget: Alpha Buffer's job ends at dispatch.
OMNI handles synthesis, GO/NO-GO, and execution routing independently.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alpha_buffer.omni_client")

DISPATCH_TIMEOUT_SECONDS = 10


def dispatch_to_omni(
    omni_url:         str,
    auth_headers:     dict[str, str],
    concordance_dict: dict,
) -> tuple[bool, Optional[str]]:
    """
    Dispatch a concordance result to OMNI for synthesis.

    Non-blocking: logs failure and returns False — does NOT block submission response.
    OMNI is expected to return 200/201/202 immediately and process async.

    Args:
        omni_url:         OMNI service webhook URL.
        auth_headers:     Auth headers to include (X-Nexus-Secret).
        concordance_dict: Full concordance result dict from ConcordanceResult.to_dict().

    Returns:
        Tuple of (success: bool, omni_response_summary: str or None).
    """
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
                concordance_dict.get("ticker"),
                concordance_dict.get("direction"),
                concordance_dict.get("pathway"),
                resp.status_code,
            )
            summary = f"HTTP {resp.status_code}"
            try:
                body    = resp.json()
                summary = body.get("status", summary)
            except Exception:
                pass
            return True, summary

        logger.warning(
            "OMNI dispatch returned non-2xx %d for %s/%s",
            resp.status_code,
            concordance_dict.get("ticker"),
            concordance_dict.get("direction"),
        )
        return False, f"HTTP {resp.status_code}"

    except requests.exceptions.Timeout:
        logger.warning(
            "OMNI dispatch timed out for %s/%s (limit: %ds)",
            concordance_dict.get("ticker"),
            concordance_dict.get("direction"),
            DISPATCH_TIMEOUT_SECONDS,
        )
        return False, "timeout"

    except Exception as e:
        logger.error(
            "OMNI dispatch error for %s/%s: %s",
            concordance_dict.get("ticker"),
            concordance_dict.get("direction"),
            e,
        )
        return False, f"error: {str(e)[:100]}"
