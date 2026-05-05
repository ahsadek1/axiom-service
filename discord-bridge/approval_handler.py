"""
approval_handler.py — Handles button interactions for approval requests.

Routes decisions (approved/rejected) back to the requesting agent via message bus.
Updates the Discord embed after decision (disables buttons, shows result).
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord
import requests

logger = logging.getLogger(__name__)

MESSAGE_BUS_URL = os.getenv("MESSAGE_BUS_URL", "http://localhost:9999")


class ApprovalHandler:
    """
    Processes Discord component interactions for approval buttons.
    Called from NexusDiscordBot.on_interaction().
    """

    def __init__(self, registry: Any) -> None:
        """
        Args:
            registry: ChannelRegistry instance (for color resolution).
        """
        self._registry = registry

    def is_approval_interaction(self, interaction: discord.Interaction) -> bool:
        """Return True if this interaction is an approval button press."""
        if interaction.type != discord.InteractionType.component:
            return False
        custom_id = (interaction.data or {}).get("custom_id", "")
        return custom_id.startswith("approve:") or custom_id.startswith("reject:")

    async def handle(self, interaction: discord.Interaction) -> None:
        """
        Handle an approval button press.

        1. Parse action + request_id from custom_id
        2. Look up pending approval
        3. Mark decided
        4. Update the embed (disable buttons)
        5. Notify requesting agent via bus
        6. Send ephemeral confirmation to Ahmed
        """
        import pending_approvals

        custom_id = (interaction.data or {}).get("custom_id", "")
        try:
            action, request_id = custom_id.split(":", 1)
        except ValueError:
            await interaction.response.send_message(
                "Invalid button format.", ephemeral=True
            )
            return

        decision = "approved" if action == "approve" else "rejected"

        # Look up the request
        approval = pending_approvals.get(request_id)
        if not approval:
            await interaction.response.send_message(
                "Approval request not found.", ephemeral=True
            )
            return

        if approval["status"] != "pending":
            await interaction.response.send_message(
                f"Already decided: {approval['status']}.", ephemeral=True
            )
            return

        # Mark decided
        pending_approvals.mark_decided(request_id, decision)
        logger.info(f"Approval {request_id}: {decision} by {interaction.user}")

        # Update the embed — replace buttons with result
        await self._update_embed(interaction, approval, decision)

        # Notify agent via message bus (best-effort)
        self._notify_agent(
            reply_to=approval["reply_to"],
            request_id=request_id,
            decision=decision,
            title=approval["title"],
        )

        # Ephemeral confirmation to Ahmed
        emoji = "✅" if decision == "approved" else "❌"
        await interaction.followup.send(
            f"{emoji} {decision.capitalize()}.", ephemeral=True
        )

    async def _update_embed(
        self,
        interaction: discord.Interaction,
        approval: Dict[str, Any],
        decision: str,
    ) -> None:
        """Edit the original message — disable buttons, show result."""
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            color = "green" if decision == "approved" else "red"
            emoji = "✅" if decision == "approved" else "❌"

            result_embed = discord.Embed(
                title=f"{emoji} {decision.upper()} — {approval['title']}",
                description=f"Decision by Ahmed • {now_str}",
                color=self._registry.resolve_color(color),
                timestamp=datetime.now(timezone.utc),
            )
            result_embed.set_footer(text=f"Request ID: {approval['request_id']}")

            # Acknowledge first, then edit the message
            await interaction.response.defer()
            if interaction.message:
                await interaction.message.edit(embed=result_embed, view=None)

        except Exception as exc:
            logger.error(f"Failed to update approval embed: {exc}")

    def _notify_agent(
        self,
        reply_to: str,
        request_id: str,
        decision: str,
        title: str,
    ) -> None:
        """Fire-and-forget bus notification to the requesting agent."""
        message_id = f"approval-result-{int(time.time() * 1000)}"
        payload = {
            "id": message_id,
            "from": "discord-bridge",
            "to": reply_to,
            "message": json.dumps({
                "type": "approval_decision",
                "request_id": request_id,
                "decision": decision,
                "title": title,
                "decided_at": time.time(),
            }),
        }
        try:
            resp = requests.post(
                f"{MESSAGE_BUS_URL}/send",
                json=payload,
                timeout=5,
            )
            if resp.status_code not in (200, 201, 202):
                logger.warning(
                    f"Bus rejected approval notification: {resp.status_code}"
                )
            else:
                logger.info(
                    f"Notified {reply_to} of approval decision: {decision} ({request_id})"
                )
        except requests.RequestException as exc:
            logger.warning(f"Could not notify {reply_to} via bus: {exc}")


def build_approval_view(request_id: str) -> discord.ui.View:
    """
    Build a discord.ui.View with Approve and Reject buttons.

    Args:
        request_id: Unique request identifier embedded in custom_id.

    Returns:
        discord.ui.View with two buttons.
    """
    view = discord.ui.View(timeout=None)  # persistent — no timeout

    approve_btn = discord.ui.Button(
        label="✅  Approve",
        style=discord.ButtonStyle.success,
        custom_id=f"approve:{request_id}",
    )
    reject_btn = discord.ui.Button(
        label="❌  Reject",
        style=discord.ButtonStyle.danger,
        custom_id=f"reject:{request_id}",
    )

    view.add_item(approve_btn)
    view.add_item(reject_btn)
    return view
