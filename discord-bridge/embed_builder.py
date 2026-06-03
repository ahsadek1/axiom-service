"""
embed_builder.py — Converts POST /send payload into a discord.Embed object.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord

from channel_registry import ChannelRegistry

logger = logging.getLogger(__name__)


class EmbedBuilder:
    """Converts validated POST body dict into a discord.Embed."""

    def __init__(self, registry: ChannelRegistry) -> None:
        self._registry = registry

    def build(self, payload: Dict[str, Any]) -> discord.Embed:
        """
        Build a discord.Embed from a validated payload dict.

        Args:
            payload: Validated POST /send body. Required keys: channel, agent, title.

        Returns:
            discord.Embed ready for sending.
        """
        color_int = self._registry.resolve_color(payload.get("color"))
        embed = discord.Embed(
            title=str(payload["title"])[:256],
            color=color_int,
        )

        # Author = agent name
        embed.set_author(name=payload["agent"])

        # Optional description
        if desc := payload.get("description"):
            embed.description = str(desc)[:4096]

        # Optional fields (max 25)
        for field in payload.get("fields", []):
            embed.add_field(
                name=str(field.get("name", ""))[:256],
                value=str(field.get("value", ""))[:1024],
                inline=bool(field.get("inline", False)),
            )

        # Optional footer
        if footer := payload.get("footer"):
            embed.set_footer(text=str(footer)[:2048])

        # Optional timestamp
        if payload.get("timestamp"):
            embed.timestamp = datetime.now(timezone.utc)

        return embed

    def build_error(self, title: str, description: str) -> discord.Embed:
        """Build a red error embed."""
        return discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=self._registry.resolve_color("red"),
            timestamp=datetime.now(timezone.utc),
        )
