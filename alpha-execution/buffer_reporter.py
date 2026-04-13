"""
buffer_reporter.py — Alpha Execution → Alpha Buffer Reporter

Reports closed trade outcomes back to Alpha Buffer.
Alpha Buffer uses these to update the circuit breaker state.
Fire-and-forget: execution continues regardless of reporter success.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alpha_exec.buffer_reporter")

REPORT_TIMEOUT = 8


class BufferReporter:
    """
    Reports trade outcomes to Alpha Buffer for circuit breaker updates.

    Args:
        buffer_url:    Alpha Buffer base URL.
        auth_secret:   NEXUS_WEBHOOK_SECRET for X-Nexus-Secret header.
    """

    def __init__(self, buffer_url: str, auth_secret: str) -> None:
        self.buffer_url  = buffer_url.rstrip("/")
        self.auth_secret = auth_secret

    def report_outcome(
        self,
        ticker:  str,
        won:     bool,
        pnl_pct: float,
        pathway: Optional[str] = None,
    ) -> bool:
        """
        Report a closed trade outcome to Alpha Buffer.

        Args:
            ticker:  Stock ticker symbol.
            won:     True if the trade was profitable.
            pnl_pct: P&L as decimal (0.35 = +35%, -0.15 = -15%).
            pathway: Trade pathway (P1/P2/P3) — optional.

        Returns:
            True if reported successfully.
        """
        try:
            resp = requests.post(
                f"{self.buffer_url}/trade-outcome",
                json    = {
                    "ticker":  ticker,
                    "won":     won,
                    "pnl_pct": pnl_pct,
                    "pathway": pathway,
                },
                headers = {
                    "X-Nexus-Secret": self.auth_secret,
                    "Content-Type":   "application/json",
                },
                timeout = REPORT_TIMEOUT,
            )
            if resp.status_code == 200:
                logger.info(
                    "Outcome reported to buffer: %s %s pnl=%.1f%%",
                    "WIN" if won else "LOSS",
                    ticker,
                    pnl_pct * 100,
                )
                return True
            logger.warning("Buffer report failed HTTP %d for %s", resp.status_code, ticker)
            return False

        except Exception as e:
            logger.warning("Buffer report error for %s: %s", ticker, e)
            return False
