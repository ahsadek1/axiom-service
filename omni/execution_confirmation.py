"""
execution_confirmation.py — Execution Order Confirmation Polling

After routing a trade to Alpha or Prime, poll the respective execution engine
to confirm the order was actually submitted. Do NOT assume a 202 response means
the order is live.

FIX-EXEC-CONFIRM (2026-05-13): Added to eliminate state desync where OMNI
thinks an order executed but it actually failed internally in Alpha/Prime.
"""

import logging
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger("omni.execution_confirmation")

# Poll every 500ms for up to 30 seconds (60 attempts)
POLL_INTERVAL_MS = 500
MAX_POLL_ATTEMPTS = 60
POLL_TIMEOUT_SECONDS = (MAX_POLL_ATTEMPTS * POLL_INTERVAL_MS) / 1000.0


def confirm_order_submitted(
    system: str,
    ticker: str,
    execution_url: str,
    auth_secret: str,
) -> Tuple[bool, Optional[str]]:
    """
    Poll the execution engine to confirm an order was actually submitted.

    After OMNI routes a verdict to Alpha or Prime, this function polls
    the execution engine's /positions endpoint to confirm the order exists.

    Args:
        system:         'alpha' or 'prime'
        ticker:         Ticker symbol (for logging/context)
        execution_url:  Base URL of the execution engine (Alpha or Prime)
        auth_secret:    Authentication secret for the execution engine

    Returns:
        Tuple of (confirmed: bool, detail: str or None).
        - True if order was confirmed to exist within timeout
        - False if polling timed out or order was never found
    """
    auth_header = "X-Nexus-Secret" if system == "alpha" else "X-Nexus-Prime-Secret"
    positions_url = f"{execution_url}/positions"

    start_time = time.time()
    attempt = 0

    while attempt < MAX_POLL_ATTEMPTS:
        attempt += 1
        elapsed = time.time() - start_time

        try:
            resp = requests.get(
                positions_url,
                headers={auth_header: auth_secret, "Content-Type": "application/json"},
                timeout=5,
            )

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    positions = data.get("positions", [])
                    
                    # Check if any position matches this ticker
                    matching_positions = [p for p in positions if p.get("ticker") == ticker]
                    
                    if matching_positions:
                        logger.info(
                            "EXEC_CONFIRM: %s/%s order confirmed on attempt %d (%.1fs)",
                            system.upper(), ticker, attempt, elapsed,
                        )
                        return True, f"confirmed after {elapsed:.1f}s"

                except Exception as e:
                    logger.warning(
                        "EXEC_CONFIRM: failed to parse /positions response: %s", e
                    )
                    # Continue polling — transient parse error
            elif resp.status_code == 202:
                # 202 Accepted — order still processing, keep polling
                pass
            else:
                logger.warning(
                    "EXEC_CONFIRM: /positions returned %d for %s/%s",
                    resp.status_code, system, ticker,
                )
                # Non-200 non-202 likely means auth error or server issue — don't retry
                return False, f"HTTP {resp.status_code}"

        except requests.exceptions.Timeout:
            logger.debug(
                "EXEC_CONFIRM: /positions poll timeout on attempt %d for %s/%s",
                attempt, system, ticker,
            )
        except Exception as e:
            logger.debug(
                "EXEC_CONFIRM: /positions poll failed on attempt %d for %s/%s: %s",
                attempt, system, ticker, e,
            )

        # Wait before retrying
        if attempt < MAX_POLL_ATTEMPTS:
            time.sleep(POLL_INTERVAL_MS / 1000.0)

    elapsed = time.time() - start_time
    logger.warning(
        "EXEC_CONFIRM: %s/%s order not confirmed after %d attempts (%.1fs) — "
        "order may have failed silently on the execution engine",
        system.upper(), ticker, MAX_POLL_ATTEMPTS, elapsed,
    )
    return False, f"no confirmation after {elapsed:.1f}s"
