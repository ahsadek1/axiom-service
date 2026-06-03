"""
PerplexityClient — AI-powered news and macro query client for THESIS.

Uses the Perplexity Sonar API to answer macro questions with cited,
synthesized responses. Replaces Brave raw-search for thesis generation.

Every public method catches all exceptions and returns a safe sentinel
rather than propagating — THESIS must never crash due to a news failure.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

_PERPLEXITY_BASE_URL = "https://api.perplexity.ai"
_DEFAULT_MODEL = "sonar"
_TIMEOUT_S = 60.0


class PerplexityClient:
    """Async HTTP client for Perplexity Sonar API.

    Used by THESIS to fetch AI-synthesized macro intelligence:
    Fed posture, credit conditions, geopolitical risks, sector themes.

    Args:
        api_key: Perplexity API key (from PERPLEXITY_API_KEY env var).
        model: Sonar model to use (default: sonar).
        timeout: Request timeout in seconds (default: 30).
    """

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        timeout: float = _TIMEOUT_S,
    ) -> None:
        """Store config; no HTTP connection opened at init."""
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def query(
        self,
        question: str,
        system_prompt: str = (
            "You are a professional macro analyst providing concise, factual "
            "market intelligence. Be direct and data-focused. Cite sources when possible."
        ),
    ) -> Optional[str]:
        """Send a macro question to Perplexity and return the synthesized answer.

        Args:
            question: The macro question to ask (e.g. 'What is the Fed's current rate posture?').
            system_prompt: System context for the AI model.

        Returns:
            Synthesized answer string, or None on any failure.
        """
        if not self._api_key:
            logger.warning("PerplexityClient: no API key configured — skipping query")
            return None

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{_PERPLEXITY_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code != 200:
                    logger.error(
                        "PerplexityClient: HTTP %d — %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return None
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                logger.debug("PerplexityClient: received %d chars for query", len(content))
                return content
        except httpx.TimeoutException:
            logger.error("PerplexityClient: request timed out after %ss", self._timeout)
            return None
        except Exception as exc:
            logger.error("PerplexityClient: unexpected error — %s", exc, exc_info=True)
            return None

    async def search_news(
        self,
        topic: str,
        recency: str = "week",
    ) -> Optional[str]:
        """Search for recent news on a macro topic.

        Args:
            topic: Macro topic to search (e.g. 'Federal Reserve interest rate policy').
            recency: Recency filter — 'day', 'week', or 'month'.

        Returns:
            Synthesized news summary, or None on any failure.
        """
        question = (
            f"Summarize the most important recent developments (last {recency}) "
            f"about: {topic}. Focus on market implications."
        )
        return await self.query(question)

    async def get_macro_context(self) -> Optional[str]:
        """Fetch a broad macro market context snapshot.

        Returns:
            Multi-topic macro summary, or None on failure.
        """
        question = (
            "Provide a concise macro market briefing covering: "
            "1) Federal Reserve policy and rate expectations, "
            "2) US Treasury yield curve shape and credit spreads, "
            "3) Key equity market risks this week, "
            "4) Dollar strength and commodity signals. "
            "Be factual and current."
        )
        return await self.query(question)

    async def check_health(self) -> bool:
        """Ping Perplexity API with a minimal request to verify connectivity.

        Returns:
            True if API is reachable and key is valid, False otherwise.
        """
        result = await self.query("Reply with the single word: ok")
        return result is not None
