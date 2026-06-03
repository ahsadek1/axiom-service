"""
Aurexis Discord Server Setup
ONE-TIME script to create the Aurexis server structure.
Creates: 7 categories, ~30 channels, 5 roles, TRADING category hidden.
"""

import asyncio
import logging
import os
import discord

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("AUREXIS_SETUP_BOT_TOKEN")
SERVER_NAME = "AUREXIS"

SERVER_STRUCTURE = {
    "GOVERNANCE": [
        "ahmed-decisions",
        "sovereign-coordination",
        "escalations",
        "daily-health-report"
    ],
    "QA COUNCIL": [
        "qa-council-status",
        "findings-stream-a",
        "findings-stream-b",
        "review-coordination",
        "convergence-divergence"
    ],
    "STREAM A — BUILD": [
        "stream-a-status",
        "spec-reviews",
        "code-reviews",
        "pre-deployment",
        "build-deployments"
    ],
    "STREAM B — CRITIQUE": [
        "stream-b-status",
        "service-reviews",
        "fix-implementation",
        "hardening-deployments"
    ],
    "FIELD INVESTIGATIONS": [
        "vector-investigations",
        "operational-diagnostics"
    ],
    "STRATEGIC INTELLIGENCE": [
        "thesis-weekly",
        "thesis-daily",
        "thesis-events",
        "thesis-reflection"
    ],
    "DOMAIN CONSULTATIONS": [
        "consultation-requests",
        "omni-synthesis",
        "atlas-iv",
        "sage-macro",
        "axiom-risk",
        "primus-sqs"
    ],
    "TRADING": [
        "trading-status",
        "execution-monitoring",
        "go-verdicts",
        "fill-confirmations"
    ]
}

ROLES_TO_CREATE = [
    {"name": "Coordinator", "color": discord.Color.gold(), "hoist": True},
    {"name": "Builder", "color": discord.Color.green(), "hoist": True},
    {"name": "Reviewer", "color": discord.Color.blue(), "hoist": True},
    {"name": "Domain Expert", "color": discord.Color.purple(), "hoist": True},
    {"name": "Intelligence", "color": discord.Color.teal(), "hoist": True},
]

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")

    guild = None
    for g in client.guilds:
        if g.name.upper() == SERVER_NAME.upper():
            guild = g
            break

    if not guild:
        logger.error(f"Could not find server '{SERVER_NAME}'. Bots in: {[g.name for g in client.guilds]}")
        await client.close()
        return

    logger.info(f"Found server: {guild.name} (id={guild.id})")

    # Step 1: Create roles
    logger.info("Creating roles...")
    existing_roles = {r.name for r in guild.roles}
    created_roles = {}
    for role_def in ROLES_TO_CREATE:
        if role_def["name"] in existing_roles:
            logger.info(f"  Role already exists: {role_def['name']}")
            created_roles[role_def["name"]] = discord.utils.get(guild.roles, name=role_def["name"])
        else:
            role = await guild.create_role(
                name=role_def["name"],
                color=role_def["color"],
                hoist=role_def["hoist"],
                reason="Aurexis setup"
            )
            created_roles[role_def["name"]] = role
            logger.info(f"  Created role: {role.name}")
        await asyncio.sleep(0.5)

    # Step 2: Create categories and channels
    logger.info("Creating categories and channels...")
    existing_categories = {c.name.upper(): c for c in guild.categories}

    for category_name, channels in SERVER_STRUCTURE.items():
        # Create category if it doesn't exist
        if category_name.upper() in existing_categories:
            category = existing_categories[category_name.upper()]
            logger.info(f"  Category already exists: {category_name}")
        else:
            # TRADING category is hidden by default
            overwrites = {}
            if category_name == "TRADING":
                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

            category = await guild.create_category(
                name=category_name,
                overwrites=overwrites,
                reason="Aurexis setup"
            )
            logger.info(f"  Created category: {category_name}")
        await asyncio.sleep(0.5)

        # Create channels within category
        existing_channels = {c.name for c in category.channels}
        for channel_name in channels:
            if channel_name in existing_channels:
                logger.info(f"    Channel already exists: #{channel_name}")
            else:
                await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    reason="Aurexis setup"
                )
                logger.info(f"    Created channel: #{channel_name}")
            await asyncio.sleep(0.3)

    logger.info("="*50)
    logger.info("SETUP COMPLETE")
    logger.info(f"Server: {guild.name}")
    logger.info(f"Categories: {len(guild.categories)}")
    logger.info(f"Channels: {len(guild.text_channels)}")
    logger.info(f"Roles created: {list(created_roles.keys())}")
    logger.info("="*50)

    await client.close()

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("ERROR: AUREXIS_SETUP_BOT_TOKEN not set")
        exit(1)
    client.run(BOT_TOKEN)
