"""
ResilientNewsClient — Perplexity-primary, Brave-fallback news client for THESIS.

Attempts Perplexity Sonar API first for AI-synthesized macro intelligence.
Falls back to Brave Search on any Perplexity failure.

Every public method catches all exceptions and returns safe sentinels —
THESIS never crashes due to a news failure.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from clients.perplexity_client import PerplexityClient
from clients.brave_client import BraveClient

logger = logging.getLogger(__name__)


class ResilientNewsClient:
    """Primary: Perplexity Sonar. Fallback: Brave Search.

    Exposes a single `search()` method that mirrors the BraveClient interface
    so ThesisEngine and all generators can use it without modification.

    Args:
        perplexity: PerplexityClient instance (primary).
        brave: BraveClient instance (fallback).
    """

    def __init__(
        self,
        perplexity: PerplexityClient,
        brave: BraveClient,
    ) -> None:
        """Store primary and fallback news clients."""
        self._perplexity = perplexity
        self._brave = brave

    async def search(self, query: str, count: int = 4) -> List[str]:
        """Search for macro news — Perplexity first, Brave on failure.

        Perplexity returns a synthesized AI answer; we wrap it as a single
        rich headline. Brave returns raw result snippets as a list.

        Args:
            query: Macro search query (e.g. 'Federal Reserve rate policy 2026').
            count: Number of results to request from Brave if used as fallback.

        Returns:
            List of headline/snippet strings. Empty list if both sources fail.
        """
        # Try Perplexity first
        try:
            result = await self._perplexity.query(query)
            if result:
                logger.debug("ResilientNewsClient: Perplexity answered query (len=%d)", len(result))
                # Return as single rich answer wrapped in a list — compatible with news_results.extend()
                return [result]
        except Exception as exc:
            logger.warning(
                "ResilientNewsClient: Perplexity failed for query '%s' — %s. Falling back to Brave.",
                query[:60], exc,
            )

        # Fallback: Brave
        try:
            results = await self._brave.search(query, count=count)
            if results:
                logger.info(
                    "ResilientNewsClient: Brave fallback returned %d results for '%s'",
                    len(results), query[:60],
                )
                return results
        except Exception as exc:
            logger.error(
                "ResilientNewsClient: Brave fallback also failed for query '%s' — %s",
                query[:60], exc,
            )

        logger.error(
            "ResilientNewsClient: both Perplexity and Brave failed for query '%s' — returning empty",
            query[:60],
        )
        return []

    async def check_health(self) -> dict:
        """Check health of both news sources.

        Returns:
            Dict with perplexity_ok and brave_ok booleans.
        """
        perplexity_ok = False
        brave_ok = False

        try:
            perplexity_ok = await self._perplexity.check_health()
        except Exception as exc:
            logger.error("ResilientNewsClient: Perplexity health check failed — %s", exc)

        try:
            brave_ok = await self._brave.check_health() if hasattr(self._brave, "check_health") else True
        except Exception as exc:
            logger.error("ResilientNewsClient: Brave health check failed — %s", exc)

        return {"perplexity_ok": perplexity_ok, "brave_ok": brave_ok}
