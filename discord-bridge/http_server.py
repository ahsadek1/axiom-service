"""
http_server.py — HTTP server for POST /send, POST /approve-request, GET /health.
Runs in its own thread alongside the Discord bot asyncio event loop.
"""
import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bridge import NexusDiscordBot, EmbedTask
    from channel_registry import ChannelRegistry

logger = logging.getLogger(__name__)

# Populated by main.py after bot init
_bot: Optional["NexusDiscordBot"] = None
_secret: Optional[str] = None
_channel_registry: Optional["ChannelRegistry"] = None


def set_dependencies(
    bot: "NexusDiscordBot",
    secret: str,
    registry: "ChannelRegistry",
) -> None:
    """Inject dependencies from main.py after initialization."""
    global _bot, _secret, _channel_registry
    _bot = bot
    _secret = secret
    _channel_registry = registry


class NexusHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the bridge API."""

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route HTTP access logs through Python logging."""
        logger.debug(f"HTTP {fmt % args}")

    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_status(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _check_auth(self) -> bool:
        """Validate X-Discord-Bridge-Secret header."""
        provided = self.headers.get("X-Discord-Bridge-Secret", "")
        return bool(_secret) and provided == _secret

    def _read_json_body(self) -> Optional[dict]:
        """Read and parse JSON body. Returns None on error (response already sent)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            return json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": "invalid_json", "detail": str(exc)})
            return None

    def do_GET(self) -> None:
        if self.path == "/health":
            self._handle_health()
        else:
            self._send_status(404)

    def do_POST(self) -> None:
        if self.path == "/send":
            self._handle_send()
        elif self.path == "/approve-request":
            self._handle_approve_request()
        elif self.path == "/mirror":
            self._handle_mirror()
        else:
            self._send_status(404)

    # -------------------------------------------------------------------------
    # GET /health
    # -------------------------------------------------------------------------

    def _handle_health(self) -> None:
        """GET /health — no auth required."""
        if _bot is None:
            self._send_json(503, {"status": "starting", "bot_connected": False})
            return
        connected = _bot.is_bot_connected()
        self._send_json(200, {
            "status": "ok" if connected else "degraded",
            "bot_connected": connected,
            "queue_depth": _bot.queue_depth(),
            "uptime_s": round(_bot.uptime_s(), 1),
        })

    # -------------------------------------------------------------------------
    # POST /send
    # -------------------------------------------------------------------------

    def _handle_send(self) -> None:
        """POST /send — requires auth."""
        if not self._check_auth():
            self._send_status(401)
            return

        if _bot is None:
            self._send_json(503, {"error": "service_not_ready"})
            return

        if not _bot.is_bot_connected():
            self._send_json(503, {"error": "bot_not_connected"})
            return

        payload = self._read_json_body()
        if payload is None:
            return

        # Validate required fields
        for required in ("channel", "agent", "title"):
            if not payload.get(required):
                self._send_json(400, {"error": "missing_field", "field": required})
                return

        # Validate channel
        if _channel_registry and not _channel_registry.is_known(payload["channel"]):
            self._send_json(400, {
                "error": "unknown_channel",
                "channel": payload["channel"],
            })
            return

        # Validate title length
        if len(str(payload.get("title", ""))) > 256:
            self._send_json(400, {"error": "title_too_long", "max": 256})
            return

        # Validate field count
        fields = payload.get("fields", [])
        if len(fields) > 25:
            self._send_json(400, {
                "error": "too_many_fields",
                "max": 25,
                "received": len(fields),
            })
            return

        # Enqueue
        from bridge import EmbedTask
        task = EmbedTask(
            channel_name=payload["channel"],
            payload=payload,
            urgent=bool(payload.get("urgent", False)),
        )
        enqueued = _bot.enqueue(task)

        if not enqueued:
            self._send_json(503, {"error": "queue_full"})
            return

        self._send_json(202, {
            "status": "queued",
            "queue_depth": _bot.queue_depth(),
        })

    # -------------------------------------------------------------------------
    # POST /approve-request
    # -------------------------------------------------------------------------

    def _handle_mirror(self) -> None:
        """POST /mirror — mirror an outbound Telegram message to Discord."""
        # Auth — reuse bridge secret
        provided = self.headers.get("X-Mirror-Secret", "") or self.headers.get("X-Discord-Bridge-Secret", "")
        if not (_secret and provided == _secret):
            self._send_status(401)
            return

        payload = self._read_json_body()
        if payload is None:
            return

        agent   = str(payload.get("agent", "main")).lower().strip()
        message = str(payload.get("message", "")).strip()
        session = str(payload.get("session", "")).strip()

        if not message:
            self._send_json(400, {"error": "empty message"})
            return

        # Skip internal/system noise
        if message in ("NO_REPLY", "HEARTBEAT_OK") or message.startswith("HEARTBEAT"):
            self._send_json(200, {"status": "skipped", "reason": "system_message"})
            return

        # Route to correct channel — caller may override (e.g. nexus-command-center for group chats)
        channel = payload.get("channel") or _MIRROR_CHANNEL_MAP.get(agent, _MIRROR_DEFAULT_CHANNEL)
        color   = _MIRROR_COLOR_MAP.get(agent, "blue")

        # Truncate long messages for embed description (Discord limit 4096)
        description = message if len(message) <= 4000 else message[:3997] + "..."

        if _bot is None or not _bot.is_bot_connected():
            self._send_json(503, {"error": "bot_not_connected"})
            return

        # Agent display names and emojis for clear identification
        _AGENT_EMOJI = {
            "genesis":   "🌱",
            "omni":      "🌐",
            "cipher":    "🔐",
            "atlas":     "🗺️",
            "sage":      "🔮",
            "sovereign": "👑",
            "primus":    "⚙️",
            "vector":    "📡",
            "axiom":     "🔷",
            "main":      "🤖",
            "ahmed":     "👤",   # Ahmed's messages forwarded from Telegram
        }
        emoji = _AGENT_EMOJI.get(agent, "🤖")
        display_name = agent.upper()

        from bridge import EmbedTask
        task = EmbedTask(
            channel_name=channel,
            payload={
                "channel":     channel,
                "agent":       agent,
                "title":       f"{emoji} {display_name}",
                "description": description,
                "color":       color,
                "footer":      f"via Telegram  •  {session[:8] if session else 'mirror'}",
                "timestamp":   True,
            },
            urgent=False,
        )
        enqueued = _bot.enqueue(task)
        if enqueued:
            self._send_json(202, {"status": "queued", "channel": channel})
        else:
            self._send_json(503, {"error": "queue_full"})

    def _handle_approve_request(self) -> None:
        """POST /approve-request — post approval embed with Approve/Reject buttons."""
        if not self._check_auth():
            self._send_status(401)
            return

        if _bot is None or not _bot.is_bot_connected():
            self._send_json(503, {"error": "bot_not_connected"})
            return

        payload = self._read_json_body()
        if payload is None:
            return

        for required in ("request_id", "title", "agent", "reply_to"):
            if not payload.get(required):
                self._send_json(400, {"error": "missing_field", "field": required})
                return

        # Schedule async call on bot's event loop (this runs in HTTP thread)
        loop = _bot._loop
        if loop is None or not loop.is_running():
            self._send_json(503, {"error": "bot_loop_not_ready"})
            return

        future = asyncio.run_coroutine_threadsafe(
            _bot.post_approval_embed(
                request_id  = payload["request_id"],
                title       = payload["title"],
                description = payload.get("description", ""),
                agent       = payload["agent"],
                reply_to    = payload["reply_to"],
                color       = payload.get("color", "gold"),
                fields      = payload.get("fields", []),
            ),
            loop,
        )
        try:
            result = future.result(timeout=10)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return

        if result:
            self._send_json(202, {
                "status": "posted",
                "request_id": payload["request_id"],
            })
        else:
            self._send_json(503, {"error": "failed_to_post"})


class ReusableHTTPServer(HTTPServer):
    """HTTPServer with SO_REUSEADDR to avoid port conflicts on restart."""
    allow_reuse_address = True


def run_http_server(host: str = "0.0.0.0", port: int = 8010) -> None:
    """Start the HTTP server (blocking — run in a thread)."""
    server = ReusableHTTPServer((host, port), NexusHTTPHandler)
    logger.info(f"HTTP server listening on {host}:{port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# POST /mirror — Telegram→Discord mirror (added 2026-04-28)
# OpenClaw calls this endpoint for every outbound agent message.
# Payload: { "agent": str, "message": str, "session": str (optional) }
# Auth: X-Mirror-Secret header (same DISCORD_BRIDGE_SECRET)
# ---------------------------------------------------------------------------

# Agent → Discord channel routing for mirror
_MIRROR_CHANNEL_MAP = {
    "genesis":   "findings-stream-a",
    "omni":      "go-verdicts",
    "cipher":    "ahmed-decisions",
    "atlas":     "go-verdicts",
    "sage":      "go-verdicts",
    "sovereign": "daily-health-report",
    "primus":    "primus-sqs",
    "vector":    "vector-investigations",
    "axiom":     "daily-health-report",
    "main":      "daily-health-report",
}
_MIRROR_DEFAULT_CHANNEL = "general"

# Agent → embed color
_MIRROR_COLOR_MAP = {
    "genesis":   "green",
    "omni":      "teal",
    "cipher":    "gold",
    "atlas":     "blue",
    "sage":      "indigo",
    "sovereign": "purple",
    "primus":    "orange",
    "vector":    "gray",
    "axiom":     "blue",
    "main":      "blue",
    "ahmed":     "red",    # Ahmed's messages — distinct colour so he stands out
}
