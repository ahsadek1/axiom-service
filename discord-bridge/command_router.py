"""
command_router.py — Slash command definitions and message bus routing.
Routes /status, /pause, /resume to SOVEREIGN; /trades to execution services; /queue to Primus.
"""
import json
import logging
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


class CommandRouter:
    """Routes Discord slash commands to Nexus agents via the message bus."""

    def __init__(self, bus_url: str, timeout_s: int = 30) -> None:
        """
        Args:
            bus_url: Base URL of the message bus, e.g. http://localhost:9999
            timeout_s: Seconds to wait for agent response before giving up.
        """
        self._bus_url = bus_url.rstrip("/")
        self._timeout_s = timeout_s

    def route(
        self,
        command: str,
        args: Dict[str, Any],
        requester: str,
        reply_channel: str,
    ) -> Optional[str]:
        """
        Send command to the appropriate agent and wait for response.

        Args:
            command: Slash command name, e.g. "/status"
            args: Parsed command arguments dict
            requester: Name of the user issuing the command
            reply_channel: Discord channel name for the reply

        Returns:
            Agent response text, or None on timeout.

        Raises:
            RuntimeError: On message bus connectivity failure.
        """
        target = self._resolve_target(command, args)
        message_id = f"discord-cmd-{int(time.time() * 1000)}"

        payload = {
            "id": message_id,
            "from": "discord-bridge",
            "to": target,
            "message": json.dumps({
                "command": command,
                "args": args,
                "requester": requester,
                "reply_channel": reply_channel,
                "message_id": message_id,
            }),
        }

        try:
            resp = requests.post(
                f"{self._bus_url}/send",
                json=payload,
                timeout=5,
            )
            if resp.status_code not in (200, 201, 202):
                raise RuntimeError(
                    f"Message bus rejected command: {resp.status_code} {resp.text}"
                )
        except requests.RequestException as exc:
            raise RuntimeError(f"Message bus unreachable: {exc}") from exc

        logger.info(f"Command {command} sent to {target} (id={message_id})")

        # Poll for response
        return self._poll_response(message_id)

    def _resolve_target(self, command: str, args: Dict[str, Any]) -> str:
        """Map slash command to the target agent name on the message bus."""
        if command == "/trades":
            system = args.get("system", "all").lower()
            return "prime-execution" if system == "prime" else "alpha-execution"
        if command == "/queue":
            return "primus"
        # /status, /pause, /resume → SOVEREIGN
        return "sovereign"

    def _poll_response(self, message_id: str) -> Optional[str]:
        """
        Poll the message bus inbox for a response to the given message_id.

        Returns:
            Response text if found within timeout, else None.
        """
        deadline = time.time() + self._timeout_s
        poll_interval = 1.0

        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{self._bus_url}/inbox/discord-bridge",
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for msg in data.get("messages", []):
                        try:
                            body = json.loads(msg.get("message", "{}"))
                            if body.get("reply_to") == message_id:
                                # Ack the message
                                self._ack(msg.get("id"))
                                return body.get("response") or str(body)
                        except (json.JSONDecodeError, TypeError):
                            pass
            except requests.RequestException as exc:
                logger.warning(f"Poll error: {exc}")

            time.sleep(poll_interval)

        return None

    def _ack(self, msg_id: Optional[str]) -> None:
        """Acknowledge (delete) a processed message from the bus."""
        if not msg_id:
            return
        try:
            requests.delete(
                f"{self._bus_url}/messages/{msg_id}",
                timeout=3,
            )
        except requests.RequestException:
            pass  # Best-effort ack
