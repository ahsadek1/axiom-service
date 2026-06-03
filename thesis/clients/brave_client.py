"""
BraveClient — Brave Search API client for web intelligence.

Used by THESIS to gather Fed communications, earnings news, geopolitical
developments, and sector context for framework analysis.

On any error, returns an empty list — callers never need to handle exceptions.
"""

from __future__ import annotations

import logging
from typing import List

import httpx

logger = logging.getLogger(__name__)

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT = 10.0


class BraveClient:
    """Async HTTP client for Brave Search API.

    Args:
        api_key: Brave Search subscription token (X-Subscription-Token header).
    """

    def __init__(self, api_key: str) -> None:
        """Store the API key; no connection opened yet."""
        self._api_key = api_key

    async def search(self, query: str, count: int = 5) -> List[str]:
        """Run a web search and return formatted result strings.

        Each returned string is formatted as "title: snippet" to give
        maximum information density for AI framework prompts.

        On any network error, rate limit, or parse failure, returns []
        so framework analysis continues with available data.

        Args:
            query: Search query string.
            count: Number of results to request (1–20).

        Returns:
            List of "title: snippet" strings, may be empty on error.
        """
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    _BRAVE_SEARCH_URL,
                    params={"q": query, "count": min(count, 20)},
                    headers={
                        "X-Subscription-Token": self._api_key,
                        "Accept": "application/json",
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        "BraveClient.search(%r) HTTP %d", query, resp.status_code
                    )
                    return []
                return self._parse_results(resp.json())
        except Exception as exc:
            logger.error(
                "BraveClient.search(%r) error: %s", query, exc, exc_info=True
            )
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_results(self, data: dict) -> List[str]:
        """Extract title+snippet strings from Brave API response.

        Args:
            data: Parsed JSON from Brave Search API.

        Returns:
            List of "title: snippet" strings.
        """
        try:
            results = data.get("web", {}).get("results", [])
            formatted: List[str] = []
            for r in results:
                title = r.get("title", "").strip()
                snippet = r.get("description", "").strip()
                if title:
                    formatted.append(f"{title}: {snippet}" if snippet else title)
            return formatted
        except Exception as exc:
            logger.error(
                "BraveClient._parse_results() failed: %s", exc, exc_info=True
            )
            return []
