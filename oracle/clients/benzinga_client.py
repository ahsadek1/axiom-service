"""
ORACLE — Benzinga Cloud API Client (Engine 8 — News Engine)
Provides: real-time news feed with ticker associations and sentiment signals for Sage.

API Key: stored in BENZINGA_API_KEY env var
Base URL: https://api.benzinga.com/api/v2/news
Rate limit: 250 req/sec / 4000 req/min (very generous)
Auth: token query parameter + Accept: application/json header

Strategy: Pull latest N articles as a bulk feed, match to pool tickers
via the 'stocks' array. More reliable than per-ticker queries on free tier.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

import config
import rate_limiter

logger = logging.getLogger(__name__)

NEWS_URL = "https://api.benzinga.com/api/v2/news"
TIMEOUT = (5, 15)
HEADERS = {"Accept": "application/json"}


def get_news_feed(page_size: int = 50, channels: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch latest news articles from Benzinga.
    Returns bulk feed — caller filters to relevant tickers.

    Args:
        page_size: Number of articles to fetch (max 100). Default 50.
        channels:  Optional channel filter (e.g. "News", "Earnings").

    Returns:
        List of article dicts with id, title, teaser, created, stocks (ticker list).
    """
    if not rate_limiter.acquire("benzinga"):
        logger.warning("Benzinga rate limit timeout")
        return []

    params: Dict[str, Any] = {
        "token": config.BENZINGA_API_KEY,
        "pageSize": min(page_size, 100),
        "displayOutput": "full",
        "sort": "created:desc",
    }
    if channels:
        params["channels"] = channels

    start = time.monotonic()
    try:
        resp = requests.get(NEWS_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 200:
            articles = resp.json()
            logger.debug("Benzinga fetched %d articles in %dms", len(articles), latency_ms)
            return articles if isinstance(articles, list) else []
        else:
            logger.error("Benzinga error %s: %s", resp.status_code, resp.text[:200])
            return []

    except requests.Timeout:
        logger.error("Benzinga news feed timeout")
        return []
    except requests.ConnectionError as e:
        logger.error("Benzinga connection error: %s", e)
        return []
    except (ValueError, KeyError) as e:
        logger.error("Benzinga response parse error: %s", e)
        return []


def get_news_for_tickers(tickers: List[str], page_size: int = 50) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch news and return articles grouped by ticker.

    Fetches a bulk feed and matches each article to tickers in the pool
    using Benzinga's 'stocks' array. More reliable than per-ticker queries.

    Args:
        tickers:   List of ticker symbols to match against.
        page_size: Number of articles to fetch from feed.

    Returns:
        Dict mapping ticker -> list of relevant article summaries.
    """
    ticker_set = {t.upper() for t in tickers}
    articles = get_news_feed(page_size=page_size)

    result: Dict[str, List[Dict[str, Any]]] = {t: [] for t in ticker_set}

    for article in articles:
        article_tickers = {
            s.get("name", "").upper()
            for s in article.get("stocks", [])
        }
        matched = ticker_set & article_tickers

        summary = {
            "id": article.get("id"),
            "title": article.get("title", ""),
            "teaser": article.get("teaser", ""),
            "created": article.get("created", ""),
            "url": article.get("url", ""),
            "tickers_in_article": list(article_tickers),
        }

        for ticker in matched:
            result[ticker].append(summary)

    return result


def score_sentiment(title: str, teaser: str) -> str:
    """
    Compute basic sentiment for a news article using keyword matching.
    Used as fast fallback when DeepSeek sentiment scoring is not needed.

    Args:
        title:  Article headline.
        teaser: Article teaser/summary.

    Returns:
        "BULLISH" | "BEARISH" | "NEUTRAL"
    """
    text = (title + " " + teaser).lower()

    bullish_keywords = {
        "beat", "beats", "surpasses", "upgrade", "upgraded", "outperform",
        "buy", "strong buy", "raised", "raises", "record", "growth",
        "bullish", "rally", "soar", "surge", "jump", "gain", "positive",
        "exceeded", "exceeds", "strong", "winning", "profit", "breakout",
    }
    bearish_keywords = {
        "miss", "misses", "disappoints", "downgrade", "downgraded", "underperform",
        "sell", "cut", "cuts", "lowered", "lowers", "loss", "losses",
        "bearish", "fall", "drop", "slump", "decline", "weak", "warning",
        "below", "concerns", "risk", "debt", "layoff", "lawsuit", "fraud",
    }

    bull_score = sum(1 for w in bullish_keywords if w in text)
    bear_score = sum(1 for w in bearish_keywords if w in text)

    if bull_score > bear_score + 1:
        return "BULLISH"
    elif bear_score > bull_score + 1:
        return "BEARISH"
    return "NEUTRAL"
