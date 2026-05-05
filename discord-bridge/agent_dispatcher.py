"""
agent_dispatcher.py — Dispatches classified messages to agents and collects responses.

Each agent has its own dispatch method:
- Message bus agents: POST /send, poll /inbox/discord-bridge
- Direct HTTP agents: REST call to service endpoint
- Self (GENESIS): read build pipeline directly
"""
import json
import logging
import os
import sys
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Service secrets from env
NEXUS_SECRET = os.getenv("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
NEXUS_PRIME_SECRET = os.getenv("NEXUS_PRIME_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
MESSAGE_BUS_URL = os.getenv("MESSAGE_BUS_URL", "http://localhost:9999")
COMMAND_TIMEOUT_S = int(os.getenv("DISCORD_COMMAND_TIMEOUT_S", "30"))

# Agent emoji map
AGENT_EMOJI = {
    "sovereign":       "👑",
    "omni":            "🧠",
    "genesis":         "🌱",
    "alpha-execution": "⚡",
    "prime-execution": "💎",
    "thesis":          "📖",
    "vector":          "🔍",
    "cipher":          "🔐",
}

# Agent color map
AGENT_COLOR = {
    "sovereign":       "blue",
    "omni":            "purple",
    "genesis":         "green",
    "alpha-execution": "teal",
    "prime-execution": "teal",
    "thesis":          "indigo",
    "vector":          "gray",
    "cipher":          "blue",
}


class AgentDispatcher:
    """
    Dispatches messages to agents and returns their response text.
    Never raises — all exceptions produce an error string.
    """

    def dispatch(self, agent: str, action: str, context: str) -> str:
        """
        Dispatch a message to an agent and return the response.

        Args:
            agent:   Target agent name (lowercase)
            action:  Action type from MessageRouter
            context: Original message or extracted context (e.g. ticker symbol)

        Returns:
            Response text string. Never raises.
        """
        try:
            if agent == "genesis":
                return self._dispatch_genesis(action, context)
            elif agent in ("alpha-execution", "prime-execution"):
                return self._dispatch_execution(agent, action, context)
            else:
                return self._dispatch_via_bus(agent, action, context)
        except Exception as exc:
            logger.error(f"Dispatch error for {agent}/{action}: {exc}", exc_info=True)
            return f"Error dispatching to {agent}: {exc}"

    def get_emoji(self, agent: str) -> str:
        """Return emoji for the agent."""
        return AGENT_EMOJI.get(agent, "🤖")

    def get_color(self, agent: str) -> str:
        """Return embed color for the agent."""
        return AGENT_COLOR.get(agent, "blue")

    # -------------------------------------------------------------------------
    # GENESIS — self-report (no bus needed)
    # -------------------------------------------------------------------------

    def _dispatch_genesis(self, action: str, context: str) -> str:
        """Read build pipeline state directly."""
        try:
            sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
            from build_pipeline import get_dashboard
            items = get_dashboard()
            if not items:
                return "Build queue is empty."
            lines = []
            for item in items:
                state = item.get("state_display", item.get("build_state", "?"))
                lines.append(
                    f"**{item['component_name']}** — {state}"
                )
            return "**Build Queue:**\n" + "\n".join(lines)
        except Exception as exc:
            return f"Could not read build pipeline: {exc}"

    # -------------------------------------------------------------------------
    # Direct HTTP — Execution services
    # -------------------------------------------------------------------------

    def _dispatch_execution(self, agent: str, action: str, context: str) -> str:
        """Call execution service /trades endpoint directly."""
        if agent == "alpha-execution":
            url = "http://localhost:8005/trades"
            secret = NEXUS_SECRET
            header = "X-Nexus-Secret"
        else:
            url = "http://localhost:8006/trades"
            secret = NEXUS_PRIME_SECRET
            header = "X-Nexus-Prime-Secret"

        try:
            resp = requests.get(url, headers={header: secret}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                trades = data.get("trades", data.get("positions", []))
                if not trades:
                    return f"No recent trades in {agent}."
                lines = []
                for t in trades[:10]:  # cap at 10
                    ticker = t.get("ticker", t.get("symbol", "?"))
                    status = t.get("status", "?")
                    entry = t.get("entry_price", t.get("avg_price", "?"))
                    lines.append(f"**{ticker}** — {status} @ {entry}")
                return f"**Recent trades ({agent}):**\n" + "\n".join(lines)
            elif resp.status_code == 404:
                return f"{agent} /trades endpoint not found. Service may be running an older version."
            else:
                return f"{agent} returned {resp.status_code}: {resp.text[:200]}"
        except requests.ConnectionError:
            return f"{agent} is not reachable at {url}. Service may be down."
        except requests.Timeout:
            return f"{agent} timed out after 10s."

    # -------------------------------------------------------------------------
    # Message bus — all other agents
    # -------------------------------------------------------------------------

    def _dispatch_via_bus(self, agent: str, action: str, context: str) -> str:
        """
        Send message to agent via message bus and poll for response.

        Returns response text or None-equivalent error string on timeout.
        """
        message_id = f"discord-ahmed-{int(time.time() * 1000)}"

        # Build the message body based on action
        body = self._build_message_body(action, context, message_id)

        payload = {
            "id": message_id,
            "from": "discord-bridge",
            "to": agent,
            "message": json.dumps(body),
        }

        try:
            resp = requests.post(
                f"{MESSAGE_BUS_URL}/send",
                json=payload,
                timeout=5,
            )
            if resp.status_code not in (200, 201, 202):
                return f"Message bus rejected message: {resp.status_code} {resp.text[:100]}"
        except requests.RequestException as exc:
            return f"Message bus unreachable: {exc}"

        logger.info(f"Dispatched {action} to {agent} (id={message_id})")

        # Poll for response
        response = self._poll_response(message_id)
        if response is None:
            return f"No response from {agent.upper()} within {COMMAND_TIMEOUT_S}s."
        return response

    def _build_message_body(self, action: str, context: str, message_id: str) -> dict:
        """Build the message body dict for the bus payload."""
        base = {
            "source": "discord-ahmed-channel",
            "message_id": message_id,
            "reply_to": message_id,
        }

        if action == "health_sweep":
            base.update({"type": "health_request", "query": context})
        elif action == "ticker_query":
            base.update({"type": "verdict_request", "ticker": context, "query": f"Latest concordance for {context}"})
        elif action == "latest_verdict":
            base.update({"type": "verdict_request", "query": context})
        elif action == "pause_execution":
            base.update({"type": "command", "command": "pause_execution", "query": context})
        elif action == "resume_execution":
            base.update({"type": "command", "command": "resume_execution", "query": context})
        elif action == "thesis_summary":
            base.update({"type": "thesis_request", "query": context})
        elif action == "investigate":
            base.update({"type": "investigation_request", "query": context})
        else:
            base.update({"type": "general_query", "query": context})

        return base

    def _poll_response(self, message_id: str) -> Optional[str]:
        """Poll message bus inbox for a response matching message_id."""
        deadline = time.time() + COMMAND_TIMEOUT_S
        poll_interval = 1.0

        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{MESSAGE_BUS_URL}/inbox/discord-bridge",
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for msg in data.get("messages", []):
                        try:
                            body = json.loads(msg.get("message", "{}"))
                            if (body.get("reply_to") == message_id or
                                    body.get("message_id") == message_id):
                                self._ack(msg.get("id"))
                                return (body.get("response") or
                                        body.get("text") or
                                        str(body))
                        except (json.JSONDecodeError, TypeError):
                            pass
            except requests.RequestException as exc:
                logger.warning(f"Poll error: {exc}")

            time.sleep(poll_interval)

        return None

    def _ack(self, msg_id: Optional[str]) -> None:
        """Best-effort acknowledge a message."""
        if not msg_id:
            return
        try:
            requests.delete(
                f"{MESSAGE_BUS_URL}/messages/{msg_id}",
                timeout=3,
            )
        except requests.RequestException:
            pass
