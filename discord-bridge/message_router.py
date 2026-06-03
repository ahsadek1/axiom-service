"""
message_router.py — Classifies Ahmed's free-form messages and determines routing.

Priority order: specific action keywords → ticker detection → catch-all SOVEREIGN.
"""
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Ticker pattern: 1-5 uppercase letters, optionally surrounded by spaces/punctuation
TICKER_RE = re.compile(r'\b([A-Z]{1,5})\b')

# Common English words that look like tickers — exclude them
TICKER_EXCLUSIONS = {
    "I", "A", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS", "IT", "ME",
    "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "AND", "ARE", "BUT", "CAN", "DID", "FOR", "GET", "GOT", "HAD", "HAS",
    "HIM", "HIS", "HOW", "ITS", "LET", "MAY", "NOT", "NOW", "OUR", "OUT",
    "SAY", "SEE", "THE", "TOO", "USE", "WAS", "WAY", "WHO", "WHY", "YES",
    "YOU", "YOUR", "WHAT", "WHEN", "WILL", "WITH", "FROM", "HAVE", "BEEN",
    "DOES", "INTO", "JUST", "LIKE", "MAKE", "MORE", "MOST", "MUCH", "NEED",
    "OVER", "SAME", "SOME", "THAN", "THAT", "THEM", "THEN", "THEY", "THIS",
    "TIME", "VERY", "WANT", "WELL", "WERE", "ALSO", "BACK", "EACH", "EVEN",
    "FULL", "GIVE", "GOOD", "HELP", "KEEP", "LAST", "LEFT", "LIVE", "LONG",
    "LOOK", "MADE", "MANY", "MOVE", "MUST", "NEXT", "PART", "SHOW", "SUCH",
    "TAKE", "TELL", "TURN", "WORK", "YEAR", "KNOW", "DOWN", "HIGH", "OPEN",
    "CALL", "POST", "STOP", "WAIT", "BOTH", "DONE", "HOLD", "ONLY", "SAID",
    "WENT", "CAME", "COME", "FEEL", "FIND", "GOES", "GONE", "HEAR", "SEEN",
    "SIDE", "ELSE", "CASE", "ABLE", "SOON", "REAL", "HERE", "NEAR", "ONCE",
    # Nexus-specific words that look like tickers
    "OMNI", "AILS", "ECHO", "PRIME", "ALPHA", "BUILD", "TEST", "SPEC",
    "LIVE", "PASS", "FAIL", "SKIP", "RISK", "DATA", "LOGS", "API",
}


@dataclass
class RouteResult:
    """Result of message classification."""
    agent: str          # target agent name
    action: str         # action type for the dispatcher
    context: str        # original message or extracted key info
    color: str = "blue" # embed color for the response


# Routing rules: (keywords, agent, action, color)
# Evaluated in order — first match wins
ROUTING_RULES = [
    # Execution control — highest priority
    (["pause execution", "pause trading", "stop execution", "halt trading"],
     "sovereign", "pause_execution", "yellow"),
    (["resume execution", "resume trading", "unpause", "start execution"],
     "sovereign", "resume_execution", "green"),

    # Trade / execution data
    (["trade", "trades", "fill", "fills", "position", "positions", "execution",
      "executed", "p&l", "pnl", "profit", "loss"],
     "alpha-execution", "trades_summary", "teal"),

    # Build / pipeline
    (["build", "queue", "pipeline", "spec", "deploy", "deployed", "genesis",
      "test", "tests", "implementation", "backlog"],
     "genesis", "build_status", "green"),

    # Health / system status
    (["status", "health", "healthy", "running", "service", "services",
      "how is", "how are", "all good", "system", "nexus", "check"],
     "sovereign", "health_sweep", "blue"),

    # Market / macro / intelligence
    (["thesis", "macro", "market", "regime", "vix", "breadth", "sentiment",
      "intelligence", "outlook", "narrative"],
     "thesis", "thesis_summary", "indigo"),

    # Investigation
    (["investigate", "vector", "investigation", "look into", "research",
      "deep dive"],
     "vector", "investigate", "gray"),

    # Concordance / verdict
    (["verdict", "concordance", "omni", "score", "pick", "recommendation"],
     "omni", "latest_verdict", "purple"),
]


class MessageRouter:
    """
    Classifies a free-form message from Ahmed and returns routing metadata.

    Priority:
    1. Explicit action keywords (pause, resume, etc.)
    2. Rule-based keyword matching (longest match wins within same rule)
    3. Ticker detection (isolated uppercase word not in exclusion list)
    4. Catch-all → SOVEREIGN general query
    """

    def classify(self, text: str) -> RouteResult:
        """
        Classify a message and return routing result.

        Args:
            text: Raw message text from Ahmed.

        Returns:
            RouteResult with agent, action, context, and color.
        """
        lower = text.lower().strip()

        if not lower:
            return RouteResult(agent="sovereign", action="general_query",
                               context=text, color="blue")

        # Rule-based matching
        for keywords, agent, action, color in ROUTING_RULES:
            for kw in keywords:
                if kw in lower:
                    logger.debug(f"Routed '{text[:50]}' → {agent}/{action} (keyword: {kw})")
                    return RouteResult(agent=agent, action=action,
                                       context=text, color=color)

        # Ticker detection — uppercase word not in exclusion list
        ticker = self._detect_ticker(text)
        if ticker:
            logger.debug(f"Routed '{text[:50]}' → omni/ticker_query (ticker: {ticker})")
            return RouteResult(agent="omni", action="ticker_query",
                               context=ticker, color="purple")

        # Catch-all
        logger.debug(f"No match for '{text[:50]}' → sovereign/general_query")
        return RouteResult(agent="sovereign", action="general_query",
                           context=text, color="blue")

    def _detect_ticker(self, text: str) -> Optional[str]:
        """
        Detect a stock ticker in the message.

        Returns the first uppercase word that looks like a ticker,
        or None if none found.
        """
        # Look for uppercase words in the original text
        matches = TICKER_RE.findall(text)
        for match in matches:
            if match not in TICKER_EXCLUSIONS and len(match) >= 2:
                return match
        return None
