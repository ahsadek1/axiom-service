"""
ORACLE — Engine 8: News Engine
Source: Benzinga Cloud API
Strategy: Bulk feed fetch, matched to pool tickers.
Cache TTL: 5 minutes (news is time-sensitive)

Per-cycle operation: fetch once per Axiom cycle for all pool tickers simultaneously.
Per-ticker operation: returns pre-matched articles from the cycle cache.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import cache
import config
from clients import benzinga_client

logger = logging.getLogger(__name__)

ENGINE = "news"
CYCLE_FEED_KEY = "__NEWS_FEED__"  # Cycle-level cache for the raw bulk feed


def fetch_cycle_feed(pool_tickers: List[str]) -> Dict[str, Any]:
    """
    Fetch news for all pool tickers in one call. Called once per Axiom cycle.
    Results cached at cycle level and used for per-ticker lookups.

    Args:
        pool_tickers: All tickers in the current pool.

    Returns:
        Dict mapping ticker -> list of news article summaries.
    """
    start = time.monotonic()

    # Check if cycle feed is already warm
    cached = cache.get(CYCLE_FEED_KEY, ENGINE, "full")
    if cached is not None:
        logger.debug("News cycle feed cache hit")
        return cached

    # Fetch bulk feed and match to pool tickers
    ticker_news = benzinga_client.get_news_for_tickers(pool_tickers, page_size=50)

    # Score sentiment on each article
    for ticker, articles in ticker_news.items():
        for article in articles:
            article["sentiment"] = benzinga_client.score_sentiment(
                article.get("title", ""),
                article.get("teaser", ""),
            )

    cache.set(CYCLE_FEED_KEY, ENGINE, ticker_news, config.NEWS_TTL, "full")
    cache.log_api_call(ENGINE, "benzinga", None,
                       int((time.monotonic() - start) * 1000), True)
    logger.info("News cycle feed fetched for %d tickers in %dms",
                len(pool_tickers), int((time.monotonic() - start) * 1000))
    return ticker_news


def fetch(ticker: str, pool_tickers: Optional[List[str]] = None,
          card_type: str = "full") -> tuple:
    """
    Fetch news intelligence for a single ticker.

    Args:
        ticker:       Target ticker symbol.
        pool_tickers: Full pool list (used to warm cycle feed if not cached).
        card_type:    "preliminary" or "full".

    Returns:
        Tuple of (news_data dict or None, freshness string).
    """
    start = time.monotonic()

    # Try per-ticker cache first
    cached = cache.get(ticker, ENGINE, card_type)
    if cached is not None:
        return cached, config.FRESHNESS_LIVE

    # Get or fetch cycle feed
    if pool_tickers is None:
        pool_tickers = [ticker]

    cycle_feed = fetch_cycle_feed(pool_tickers)
    articles = cycle_feed.get(ticker.upper(), [])

    if not articles:
        news_data = {
            "article_count": 0,
            "articles": [],
            "net_sentiment": "NEUTRAL",
            "bullish_count": 0,
            "bearish_count": 0,
        }
    else:
        bullish = sum(1 for a in articles if a.get("sentiment") == "BULLISH")
        bearish = sum(1 for a in articles if a.get("sentiment") == "BEARISH")
        net = "BULLISH" if bullish > bearish else "BEARISH" if bearish > bullish else "NEUTRAL"

        news_data = {
            "article_count": len(articles),
            "articles": articles[:10],  # cap at 10 per ticker
            "net_sentiment": net,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "most_recent_headline": articles[0].get("title", "") if articles else "",
        }

    cache.set(ticker, ENGINE, news_data, config.NEWS_TTL, card_type)
    cache.log_api_call(ENGINE, "benzinga", ticker,
                       int((time.monotonic() - start) * 1000), True)
    return news_data, config.FRESHNESS_LIVE
