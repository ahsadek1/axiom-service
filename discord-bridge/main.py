"""
main.py — Entry point for the discord-bridge service.

Starts:
  1. HTTP server thread (POST /send, GET /health)
  2. Discord bot (asyncio event loop, slash commands, embed delivery)
"""
import logging
import os
import secrets
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

# Load .env from service directory
load_dotenv(dotenv_path=str(Path(__file__).parent / ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> None:
    """Entry point: validate config, start HTTP server, run Discord bot."""
    import discord

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("DISCORD_BOT_TOKEN is not set. Cannot start.")
        sys.exit(1)

    bridge_secret = os.getenv("DISCORD_BRIDGE_SECRET")
    if not bridge_secret:
        bridge_secret = secrets.token_hex(32)
        logger.warning(
            f"DISCORD_BRIDGE_SECRET not set — generated ephemeral secret: {bridge_secret}"
        )

    port = int(os.getenv("DISCORD_BRIDGE_PORT", "8010"))
    bus_url = os.getenv("MESSAGE_BUS_URL", "http://localhost:9999")
    command_timeout = int(os.getenv("DISCORD_COMMAND_TIMEOUT_S", "30"))
    queue_max = int(os.getenv("DISCORD_QUEUE_MAX", "500"))

    from channel_registry import ChannelRegistry
    from embed_builder import EmbedBuilder
    from command_router import CommandRouter
    from bridge import NexusDiscordBot
    import http_server

    registry = ChannelRegistry()
    builder = EmbedBuilder(registry)
    router = CommandRouter(bus_url=bus_url, timeout_s=command_timeout)
    bot = NexusDiscordBot(
        registry=registry,
        embed_builder=builder,
        command_router=router,
        queue_max=queue_max,
    )

    http_server.set_dependencies(bot=bot, secret=bridge_secret, registry=registry)

    # Start HTTP server in background thread
    http_thread = threading.Thread(
        target=http_server.run_http_server,
        kwargs={"port": port},
        daemon=True,
        name="http-server",
    )
    http_thread.start()
    logger.info(f"HTTP server thread started on port {port}")

    # Run Discord bot (blocks until stopped)
    logger.info("Starting Discord bot...")
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure as exc:
        logger.error(f"DISCORD_BOT_TOKEN rejected by Discord API: {exc}")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "Message Content Intent not enabled. "
            "Go to https://discord.com/developers/applications/1498176189884403812/bot "
            "and enable 'Message Content Intent' under Privileged Gateway Intents."
        )
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Discord bot crashed: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
