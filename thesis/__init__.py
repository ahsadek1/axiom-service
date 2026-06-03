"""
clients — THESIS external service clients.

Exports all client classes used throughout the THESIS service.
"""

from __future__ import annotations

from clients.chronicle_client import ChronicleClient, ResilientChronicleClient
from clients.fallback_client import FallbackChronicleClient
from clients.oracle_client import OracleClient
from clients.brave_client import BraveClient
from clients.perplexity_client import PerplexityClient
from clients.news_client import ResilientNewsClient
from clients.sovereign_client import SovereignClient
from clients.telegram_client import TelegramClient

__all__ = [
    "ChronicleClient",
    "ResilientChronicleClient",
    "FallbackChronicleClient",
    "OracleClient",
    "BraveClient",
    "PerplexityClient",
    "ResilientNewsClient",
    "SovereignClient",
    "TelegramClient",
]
