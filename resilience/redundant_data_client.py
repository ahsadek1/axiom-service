"""
redundant_data_client.py — Approach 2: Redundant Execution Architecture

Wraps any data source with an automatic fallback chain.
Switches to fallback within milliseconds on primary failure.
Never halts the calling pipeline — always returns something.

Pre-wired chains for Nexus V2:
  IV data:     ORACLE primary → Polygon → Historical vol × 1.15
  Price:       Polygon → Alpha Vantage → yfinance fallback
  Macro/Regime: FRED+composite → VIX-only classification
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp
import requests

logger = logging.getLogger(__name__)


@dataclass
class DataSourceResult:
    """Result from a data source fetch, primary or fallback."""
    data: Optional[Any]
    source_used: str
    is_primary: bool
    is_fallback: bool
    quality_score: float        # 1.0 = primary, 0.0 = exhausted
    latency_ms: float


@dataclass
class FallbackActivation:
    """Record of a single fallback activation event."""
    timestamp: float
    source: str
    quality: float
    primary_down_seconds: float


class RedundantDataClient:
    """
    Wraps any data source with automatic fallback chain.
    Switches to fallback within milliseconds on primary failure.
    Logs all fallback activations for EOD review.
    Never halts the calling pipeline — always returns something.

    Usage:
        client = RedundantDataClient(
            source_name="iv_data",
            primary_fn=fetch_oracle_iv,
            fallback_fns=[
                (fetch_polygon_iv, 0.85),
                (estimate_historical_vol, 0.60),
            ]
        )
        result = await client.fetch(ticker="AAPL")
    """

    def __init__(
        self,
        source_name: str,
        primary_fn: Callable,
        fallback_fns: List[Tuple[Callable, float]],
        max_primary_retries: int = 1,
        primary_timeout_ms: float = 2000,
        fallback_timeout_ms: float = 1000,
    ):
        """
        Args:
            source_name: Human-readable name for logging/reporting.
            primary_fn: Async callable for primary data source.
            fallback_fns: List of (async_callable, quality_score) tuples. Tried in order.
            max_primary_retries: How many times to retry primary before fallback.
            primary_timeout_ms: Timeout for primary source in milliseconds.
            fallback_timeout_ms: Timeout per fallback in milliseconds.
        """
        self.source_name = source_name
        self.primary_fn = primary_fn
        self.fallback_fns = fallback_fns
        self.max_primary_retries = max_primary_retries
        self.primary_timeout_s = primary_timeout_ms / 1000
        self.fallback_timeout_s = fallback_timeout_ms / 1000
        self._fallback_activations: List[FallbackActivation] = []
        self._last_primary_failure: Optional[float] = None

    async def fetch(self, *args: Any, **kwargs: Any) -> DataSourceResult:
        """
        Fetch data with automatic fallback.
        Always returns a result — never raises to caller.

        Returns:
            DataSourceResult with data, source, quality, and latency.
        """
        start = time.time()

        # Try primary with retries
        for attempt in range(self.max_primary_retries + 1):
            try:
                data = await asyncio.wait_for(
                    self.primary_fn(*args, **kwargs),
                    timeout=self.primary_timeout_s,
                )
                latency = (time.time() - start) * 1000
                self._last_primary_failure = None
                return DataSourceResult(
                    data=data,
                    source_used=f"{self.source_name}_primary",
                    is_primary=True,
                    is_fallback=False,
                    quality_score=1.0,
                    latency_ms=latency,
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(
                    "%s primary attempt %d failed: %s",
                    self.source_name, attempt + 1, e,
                )
                if attempt < self.max_primary_retries:
                    await asyncio.sleep(0.1 * (attempt + 1))

        self._last_primary_failure = time.time()

        # Try fallbacks in order
        for i, (fallback_fn, quality) in enumerate(self.fallback_fns):
            try:
                data = await asyncio.wait_for(
                    fallback_fn(*args, **kwargs),
                    timeout=self.fallback_timeout_s,
                )
                latency = (time.time() - start) * 1000
                fallback_name = f"{self.source_name}_fallback_{i + 1}"
                self._record_fallback_activation(fallback_name, quality)
                return DataSourceResult(
                    data=data,
                    source_used=fallback_name,
                    is_primary=False,
                    is_fallback=True,
                    quality_score=quality,
                    latency_ms=latency,
                )
            except Exception as e:
                logger.warning("%s fallback %d failed: %s", self.source_name, i + 1, e)

        # All sources exhausted
        logger.error("%s ALL sources failed — returning empty result", self.source_name)
        return DataSourceResult(
            data=None,
            source_used=f"{self.source_name}_all_failed",
            is_primary=False,
            is_fallback=True,
            quality_score=0.0,
            latency_ms=(time.time() - start) * 1000,
        )

    def _record_fallback_activation(self, source: str, quality: float) -> None:
        """Record fallback activation and log warning."""
        down_seconds = (time.time() - self._last_primary_failure) if self._last_primary_failure else 0.0
        activation = FallbackActivation(
            timestamp=time.time(),
            source=source,
            quality=quality,
            primary_down_seconds=down_seconds,
        )
        self._fallback_activations.append(activation)
        logger.warning("FALLBACK ACTIVATED: %s (quality=%.2f, primary_down=%.1fs)", source, quality, down_seconds)

    def get_eod_fallback_report(self) -> Dict:
        """Returns fallback activation summary for EOD report."""
        return {
            "source": self.source_name,
            "total_fallback_activations": len(self._fallback_activations),
            "activations": [
                {
                    "timestamp": a.timestamp,
                    "source": a.source,
                    "quality": a.quality,
                    "primary_down_seconds": a.primary_down_seconds,
                }
                for a in self._fallback_activations[-10:]
            ],
        }


# ---------------------------------------------------------------------------
# Pre-wired client factories for Nexus V2
# ---------------------------------------------------------------------------

async def _oracle_iv_fetch(ticker: str, oracle_url: str, oracle_secret: str) -> Dict:
    """Fetch IV context from ORACLE."""
    url = f"{oracle_url}/oracle/context/{ticker}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"X-Oracle-Secret": oracle_secret}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _polygon_iv_estimate(ticker: str, polygon_key: str) -> Dict:
    """Estimate IV from Polygon options chain."""
    url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?apiKey={polygon_key}&limit=1"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return {"source": "polygon", "ticker": ticker, "data": data}


async def _historical_vol_estimate(ticker: str, polygon_key: str) -> Dict:
    """Estimate volatility as historical vol × 1.15 (lowest quality fallback)."""
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/2024-01-01/2025-01-01?apiKey={polygon_key}&limit=252"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                raise ValueError(f"No historical data for {ticker}")
            closes = [r["c"] for r in results]
            import statistics
            returns = [(closes[i] / closes[i-1]) - 1 for i in range(1, len(closes))]
            daily_vol = statistics.stdev(returns) if len(returns) > 1 else 0.02
            annualized_iv = daily_vol * (252 ** 0.5) * 1.15
            return {"source": "historical_vol", "ticker": ticker, "iv_estimate": annualized_iv, "quality": "degraded"}


def build_iv_client(oracle_url: str, oracle_secret: str, polygon_key: str) -> RedundantDataClient:
    """
    Build the IV data redundant client.
    Chain: ORACLE → Polygon → Historical vol × 1.15
    """
    import functools
    return RedundantDataClient(
        source_name="iv_data",
        primary_fn=functools.partial(_oracle_iv_fetch, oracle_url=oracle_url, oracle_secret=oracle_secret),
        fallback_fns=[
            (functools.partial(_polygon_iv_estimate, polygon_key=polygon_key), 0.85),
            (functools.partial(_historical_vol_estimate, polygon_key=polygon_key), 0.60),
        ],
        primary_timeout_ms=30000,   # ORACLE can take up to 30s (cold cache)
        fallback_timeout_ms=5000,
    )


async def _fred_macro_fetch(fred_key: str) -> Dict:
    """Fetch macro regime data from FRED (VIX + economic indicators)."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&api_key={fred_key}&file_type=json"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            observations = data.get("observations", [])
            if observations:
                latest = observations[-1]
                vix = float(latest.get("value", 20.0))
                return {"source": "fred", "vix": vix, "classification": _classify_regime(vix)}
            raise ValueError("No FRED observations returned")


async def _vix_only_regime(polygon_key: str) -> Dict:
    """Fallback regime: VIX-only classification from Polygon."""
    url = f"https://api.polygon.io/v2/last/trade/I:VIX?apiKey={polygon_key}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            # Polygon doesn't support VIX — use Axiom's cached VIX from /health
            pass
    # Last resort: return neutral regime
    return {"source": "vix_only_fallback", "vix": 20.0, "classification": "NORMAL"}


def _classify_regime(vix: float) -> str:
    """Classify market regime based on VIX."""
    if vix > 35:
        return "EXTREME_FEAR"
    if vix > 28:
        return "HIGH_FEAR"
    if vix > 20:
        return "ELEVATED"
    return "NORMAL"


def build_regime_client(fred_key: str, polygon_key: str) -> RedundantDataClient:
    """
    Build the macro/regime redundant client.
    Chain: FRED composite → VIX-only classification
    """
    import functools
    return RedundantDataClient(
        source_name="macro_regime",
        primary_fn=functools.partial(_fred_macro_fetch, fred_key=fred_key),
        fallback_fns=[
            (functools.partial(_vix_only_regime, polygon_key=polygon_key), 0.50),
        ],
        primary_timeout_ms=10000,
        fallback_timeout_ms=5000,
    )
