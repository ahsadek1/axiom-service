"""
bridge.py — Core bridge: asyncio queue, Discord bot client, embed delivery.

The bridge owns the asyncio event loop (runs in the Discord bot thread).
The HTTP server thread puts EmbedTask objects into an asyncio-safe queue.
The bot coroutine dequeues and sends them.
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import discord
from discord import app_commands

from channel_registry import ChannelRegistry
from embed_builder import EmbedBuilder
from command_router import CommandRouter
from message_router import MessageRouter
from agent_dispatcher import AgentDispatcher
from approval_handler import ApprovalHandler, build_approval_view
import pending_approvals

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent / "queue.db")


@dataclass
class EmbedTask:
    """A queued embed delivery task."""
    channel_name: str
    payload: Dict[str, Any]
    urgent: bool = False
    task_id: str = field(default_factory=lambda: str(int(time.time() * 1000)))


class NexusDiscordBot(discord.Client):
    """
    Discord bot client with slash command tree and embed delivery loop.
    """

    def __init__(
        self,
        registry: ChannelRegistry,
        embed_builder: EmbedBuilder,
        command_router: CommandRouter,
        queue_max: int = 500,
        db_path: str = DB_PATH,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # requires "Message Content Intent" enabled in Discord Dev Portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._registry = registry
        self._embed_builder = embed_builder
        self._command_router = command_router
        self._queue_max = queue_max
        self._db_path = db_path
        self._queue: Optional[asyncio.Queue] = None  # created in on_ready with correct loop
        self._queue_max = queue_max
        self._start_time = time.time()
        self._connected = False
        self._guild_id: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ahmed_channel = os.getenv("DISCORD_AHMED_CHANNEL", "ahmed")
        self._decisions_channel = os.getenv("DISCORD_DECISIONS_CHANNEL", "ahmed-decisions")
        self._message_router = MessageRouter()
        self._agent_dispatcher = AgentDispatcher()
        self._approval_handler = ApprovalHandler(registry=registry)
        self._setup_db()
        pending_approvals.init_db()
        self._register_commands()

    def _setup_db(self) -> None:
        """Initialize SQLite WAL queue persistence."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queued_embeds (
                id TEXT PRIMARY KEY,
                channel_name TEXT NOT NULL,
                urgent INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"Queue DB initialized at {self._db_path}")

    def _persist_task(self, task: EmbedTask) -> None:
        """Write a task to the persistent queue DB."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO queued_embeds (id, channel_name, urgent, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (task.task_id, task.channel_name, int(task.urgent),
                 json.dumps(task.payload), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def _remove_persisted_task(self, task_id: str) -> None:
        """Remove a delivered task from the persistent queue."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM queued_embeds WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()

    def _load_persisted_tasks(self) -> list:
        """Load undelivered tasks from DB on startup."""
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id, channel_name, urgent, payload FROM queued_embeds ORDER BY created_at ASC"
            ).fetchall()
        finally:
            conn.close()
        tasks = []
        for row in rows:
            try:
                payload = json.loads(row[3])
                tasks.append(EmbedTask(
                    channel_name=row[1],
                    payload=payload,
                    urgent=bool(row[2]),
                    task_id=row[0],
                ))
            except Exception as exc:
                logger.warning(f"Could not reload persisted task {row[0]}: {exc}")
        return tasks

    def _register_commands(self) -> None:
        """Register Discord slash commands on the command tree."""

        @self.tree.command(name="status", description="Get Nexus system status")
        @app_commands.describe(agent="Optional: specific agent name")
        async def status_cmd(interaction: discord.Interaction, agent: str = "") -> None:
            await self._handle_slash(interaction, "/status", {"agent": agent})

        @self.tree.command(name="pause", description="Pause a Nexus service")
        @app_commands.describe(service="Service name to pause")
        async def pause_cmd(interaction: discord.Interaction, service: str) -> None:
            await self._handle_slash(interaction, "/pause", {"service": service})

        @self.tree.command(name="resume", description="Resume a paused Nexus service")
        @app_commands.describe(service="Service name to resume")
        async def resume_cmd(interaction: discord.Interaction, service: str) -> None:
            await self._handle_slash(interaction, "/resume", {"service": service})

        @self.tree.command(name="trades", description="List recent Nexus trades")
        @app_commands.describe(system="System: alpha, prime, or all (default: all)")
        async def trades_cmd(interaction: discord.Interaction, system: str = "all") -> None:
            await self._handle_slash(interaction, "/trades", {"system": system})

        @self.tree.command(name="queue", description="Check Primus SQS queue depth")
        async def queue_cmd(interaction: discord.Interaction) -> None:
            await self._handle_slash(interaction, "/queue", {})

    async def _handle_slash(
        self,
        interaction: discord.Interaction,
        command: str,
        args: Dict[str, Any],
    ) -> None:
        """Handle a slash command interaction."""
        # Ack within 3s (Discord requirement)
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel_name = interaction.channel.name if interaction.channel else "unknown"
        requester = interaction.user.display_name if interaction.user else "Ahmed"

        logger.info(f"Slash command {command} from {requester} in #{channel_name}")

        try:
            loop = asyncio.get_running_loop()
            response_text = await loop.run_in_executor(
                None,
                lambda: self._command_router.route(
                    command=command,
                    args=args,
                    requester=requester,
                    reply_channel=channel_name,
                ),
            )
        except Exception as exc:
            logger.error(f"Command router error for {command}: {exc}")
            error_embed = self._embed_builder.build_error(
                "Routing Error",
                f"Failed to route {command}: {exc}",
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)
            return

        import datetime
        if response_text is None:
            timeout_embed = self._embed_builder.build_error(
                "No Response",
                f"No response from agent within {self._command_router._timeout_s}s",
            )
            if interaction.channel:
                await interaction.channel.send(embed=timeout_embed)
            await interaction.followup.send("⏱ No response received.", ephemeral=True)
        else:
            response_embed = discord.Embed(
                title=f"Response to {command}",
                description=str(response_text)[:4096],
                color=self._registry.resolve_color("blue"),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            response_embed.set_footer(text=f"Requested by {requester}")
            if interaction.channel:
                await interaction.channel.send(embed=response_embed)
            await interaction.followup.send("✅ Response posted.", ephemeral=True)

    async def setup_hook(self) -> None:
        """Called before the bot connects — sync slash commands."""
        self._loop = asyncio.get_running_loop()
        guild_id_str = os.getenv("DISCORD_GUILD_ID", "")
        if guild_id_str:
            try:
                self._guild_id = int(guild_id_str)
                guild = discord.Object(id=self._guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info(f"Slash commands synced to guild {self._guild_id}")
            except Exception as exc:
                logger.error(f"Failed to sync slash commands to guild {guild_id_str}: {exc}")
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally")

    async def on_ready(self) -> None:
        """Bot is connected and ready."""
        self._loop = asyncio.get_running_loop()
        # Create queue bound to the correct running loop
        self._queue = asyncio.Queue(maxsize=self._queue_max)
        self._connected = True
        logger.info(f"Discord bot ready: {self.user} ({self.user.id})")
        # Reload persisted tasks
        persisted = self._load_persisted_tasks()
        if persisted:
            logger.info(f"Reloading {len(persisted)} persisted embed tasks")
            for task in persisted:
                try:
                    self._queue.put_nowait(task)
                except asyncio.QueueFull:
                    logger.warning(f"Queue full, dropping persisted task {task.task_id}")
        # Start delivery loop
        asyncio.create_task(self._delivery_loop())

    async def on_disconnect(self) -> None:
        """Bot disconnected."""
        self._connected = False
        logger.warning("Discord bot disconnected")

    async def on_resumed(self) -> None:
        """Bot reconnected after disconnect."""
        self._connected = True
        logger.info("Discord bot reconnected")

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle button interactions (approval buttons in #ahmed-decisions)."""
        if self._approval_handler.is_approval_interaction(interaction):
            await self._approval_handler.handle(interaction)
            return
        # Pass all other interactions to the command tree
        await self.tree.process_application_commands(interaction)

    async def post_approval_embed(
        self,
        request_id:  str,
        title:       str,
        description: str,
        agent:       str,
        reply_to:    str,
        color:       str = "gold",
        fields:      list = None,
    ) -> bool:
        """
        Post an approval request embed with Approve/Reject buttons to #ahmed-decisions.
        Stores the request in pending_approvals DB.
        
        Returns True if posted successfully.
        """
        import datetime
        channel = discord.utils.get(self.get_all_channels(), name=self._decisions_channel)
        if channel is None:
            logger.error(f"#ahmed-decisions channel not found")
            return False

        embed = discord.Embed(
            title=f"✋ {title}",
            description=description,
            color=self._registry.resolve_color(color),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_author(name=agent.upper())
        for f in (fields or []):
            embed.add_field(
                name=f.get("name", ""),
                value=f.get("value", ""),
                inline=f.get("inline", True),
            )
        embed.set_footer(text=f"Request ID: {request_id}")

        view = build_approval_view(request_id)

        try:
            msg = await channel.send(embed=embed, view=view)
            pending_approvals.insert(
                request_id=request_id,
                title=title,
                agent=agent,
                reply_to=reply_to,
                payload={"title": title, "description": description,
                         "agent": agent, "reply_to": reply_to,
                         "color": color, "fields": fields or []},
            )
            pending_approvals.set_message(
                request_id=request_id,
                message_id=str(msg.id),
                channel_id=str(channel.id),
            )
            logger.info(f"Approval request posted: {request_id}")
            return True
        except Exception as exc:
            logger.error(f"Failed to post approval embed: {exc}")
            return False

    # ── Channels that accept inbound commands from Ahmed ──────────────────────
    INTERACTIVE_CHANNELS = {"ahmed", "nexus-command-center"}

    async def on_message(self, message: discord.Message) -> None:
        """
        Handle free-form messages in #ahmed or #nexus-command-center.
        - Ignores bot messages (prevents echo loops)
        - Routes commands to agents via message bus
        - Forwards Ahmed's human messages back to the Telegram Health Group
        """
        if message.author.bot:
            return

        channel_name = message.channel.name if message.channel else ""
        if channel_name not in self.INTERACTIVE_CHANNELS:
            return

        text = message.content.strip()
        if not text:
            return

        author = message.author.display_name
        logger.info(f"#{channel_name} message from {author}: {text[:80]}")

        # ── Always forward Ahmed's Discord message back to Telegram Health Group ──
        # This closes the loop: Discord → Telegram so agents see it in their session
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._forward_to_telegram(text, author, channel_name))

        # Acknowledge immediately
        try:
            await message.add_reaction("✅")
        except Exception:
            pass

        # Classify and dispatch in executor (blocking I/O)
        route = self._message_router.classify(text)
        logger.info(f"Routed to {route.agent}/{route.action}")

        try:
            response_text = await loop.run_in_executor(
                None,
                lambda: self._agent_dispatcher.dispatch(
                    agent=route.agent,
                    action=route.action,
                    context=route.context,
                ),
            )
        except Exception as exc:
            response_text = f"Dispatch error: {exc}"

        import datetime
        emoji = self._agent_dispatcher.get_emoji(route.agent)
        color = self._agent_dispatcher.get_color(route.agent)
        embed = discord.Embed(
            title=f"{emoji} {route.agent.upper()}",
            description=str(response_text)[:4096],
            color=self._registry.resolve_color(color),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text=f'Re: "{text[:80]}"')

        try:
            await message.channel.send(embed=embed)
        except Exception as exc:
            logger.error(f"Failed to send #{channel_name} response: {exc}")

    def _forward_to_telegram(self, text: str, author: str, source_channel: str) -> None:
        """
        Forward a Discord message from Ahmed back to the Telegram Health Group.
        This closes the bidirectional loop: agents see Ahmed's Discord messages
        in their Telegram session context.
        """
        import requests as _req
        # Determine Telegram group based on source channel
        # nexus-command-center mirrors V1 Health Group
        HEALTH_GROUP_TG_ID = "-1003954790884"

        # Use the Nexus bot token to post back to Telegram
        TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc")

        formatted = f"📡 *[Discord → {source_channel}]*\n*{author}:* {text}"
        try:
            resp = _req.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": HEALTH_GROUP_TG_ID,
                    "text": formatted,
                    "parse_mode": "Markdown",
                },
                timeout=5,
            )
            if resp.status_code == 200:
                logger.info(f"Forwarded Discord message from {author} → Telegram Health Group")
            else:
                logger.warning(f"Telegram forward failed: {resp.status_code} {resp.text[:100]}")
        except Exception as exc:
            logger.warning(f"Telegram forward error: {exc}")

    def is_bot_connected(self) -> bool:
        """Return True if the bot is connected to Discord."""
        return self._connected

    def queue_depth(self) -> int:
        """Return current in-memory queue depth."""
        return self._queue.qsize() if self._queue else 0

    def uptime_s(self) -> float:
        """Return seconds since bot process started."""
        return time.time() - self._start_time

    def enqueue(self, task: EmbedTask) -> bool:
        """
        Enqueue an embed task from the HTTP server thread (thread-safe).

        Returns:
            True if enqueued successfully, False if no running event loop.
        """
        if self._queue is None:
            # Queue not ready yet (bot not connected) — persist and return False
            self._persist_task(task)
            return False

        if self._queue.qsize() >= self._queue_max:
            logger.warning(
                f"Embed queue at max ({self._queue_max}). Dropping oldest item."
            )
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

        self._persist_task(task)

        # Thread-safe put into asyncio queue
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False

        if loop.is_running():
            loop.call_soon_threadsafe(self._queue.put_nowait, task)
            return True
        return False

    async def _delivery_loop(self) -> None:
        """Continuously dequeue and deliver embed tasks to Discord."""
        logger.info("Embed delivery loop started")
        while True:
            try:
                task: EmbedTask = await self._queue.get()
                await self._deliver(task)
                self._queue.task_done()
            except Exception as exc:
                logger.error(f"Delivery loop error: {exc}", exc_info=True)
                await asyncio.sleep(1)

    async def _deliver(self, task: EmbedTask) -> None:
        """Deliver a single embed task to its Discord channel."""
        try:
            channel = discord.utils.get(self.get_all_channels(), name=task.channel_name)
            if channel is None:
                logger.error(f"Discord channel not found: #{task.channel_name}")
                self._remove_persisted_task(task.task_id)
                return

            embed = self._embed_builder.build(task.payload)
            content = "@here" if task.urgent else None

            await channel.send(content=content, embed=embed)
            self._remove_persisted_task(task.task_id)
            logger.info(f"Delivered embed to #{task.channel_name} (task={task.task_id})")

        except discord.HTTPException as exc:
            logger.error(f"Discord HTTP error delivering to #{task.channel_name}: {exc}")
        except Exception as exc:
            logger.error(
                f"Unexpected error delivering to #{task.channel_name}: {exc}",
                exc_info=True,
            )
            self._remove_persisted_task(task.task_id)
