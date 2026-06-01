"""
SovereignClient — Message bus client for SOVEREIGN communication.

THESIS sends alerts to SOVEREIGN via the Nexus message bus whenever a
thesis-breaking event is detected or a new thesis is ready.

All methods return bool and never raise — SOVEREIGN communication failure
must never block trading operations.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


class SovereignClient:
    """Async HTTP client for the Nexus message bus.

    Args:
        bus_url: Base URL of the message bus (e.g. http://192.168.1.141:9999).
    """

    def __init__(self, bus_url: str) -> None:
        """Store the bus URL; no connection opened yet."""
        self._bus_url = bus_url.rstrip("/")

    async def send(self, message: str, event_type: str = "alert") -> bool:
        """Send a message to SOVEREIGN via the Nexus message bus.

        Args:
            message: Human-readable message text.
            event_type: Classification hint (e.g. "alert", "status", "event").

        Returns:
            True if bus accepted the message (2xx), False otherwise.
        """
        payload = {
            "from": "thesis",
            "to": "sovereign",
            "message": f"[THESIS/{event_type.upper()}] {message}",
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._bus_url}/send",
                    json=payload,
                )
                if resp.status_code < 300:
                    return True
                logger.warning(
                    "SovereignClient.send() HTTP %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception as exc:
            logger.error(
                "SovereignClient.send() error: %s", exc, exc_info=True
            )
            return False
