"""
graceful_degradation.py — Nexus v1 Graceful Degradation Manager
================================================================
Part of NEXUS RESILIENCE FRAMEWORK v1.0 — Week 1 Build

RESPONSIBILITIES:
  1. Monitor ORATS API health continuously
  2. Fall back to Polygon IV when ORATS degrades
  3. Maintain CONDITIONAL → HOLD (April 1 pattern — NEVER degrade this gate)
  4. Log all degradation events for OMNI analysis
  5. Alert Ahmed on degradation and recovery

DEGRADATION TIERS:
  TIER_1: ORATS responds but data is stale (> 60s old) → use with warning
  TIER_2: ORATS 4xx/5xx → fall back to Polygon IV snapshot
  TIER_3: Both ORATS + Polygon fail → use last known good IV + flag as DEGRADED
  TIER_4: All data sources dead → HALT (no new positions in degraded data state)

SACRED RULES (never degrade these):
  - CONDITIONAL verdict → always HOLD (never auto-approve on degraded data)
  - If data source tier is TIER_4 → HALT new entries, close nothing
  - Degradation never changes an existing GO → only blocks new GOs

Author: OMNI 🌐
Date:   2026-04-15 (Resilience Framework Week 1)
"""

import os
import time
import json
import logging
import threading
import datetime
import requests
from typing import Optional, Tuple
from enum import Enum

logger = logging.getLogger("omni.degradation")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] %(levelname)s — %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
ORATS_API_KEY   = os.getenv("ORATS_API_KEY", "4476e955-241a-4540-b114-ebbf1a3a3b87")
POLYGON_KEY     = os.getenv("POLYGON_API_KEY", "OaxOzJMu_JZpl7uF64L7FyhowSZtwcvI")
TG_BOT_TOKEN    = os.getenv("TG_BOT_TOKEN", "")
TG_HEALTH_GROUP = os.getenv("TG_HEALTH_GROUP", "-5184172590")
TG_AHMED_DM     = "8573754783"

ORATS_HEALTH_INTERVAL  = 30    # seconds — how often to probe ORATS
CACHE_MAX_AGE          = 120   # seconds — max age before data is "stale"
RECOVERY_ALERT_DELAY   = 60    # seconds — wait before declaring recovery


class DegradationTier(Enum):
    OK       = 0   # All systems nominal
    TIER_1   = 1   # Stale data — using with warning
    TIER_2   = 2   # ORATS down — Polygon fallback active
    TIER_3   = 3   # Both down — using cached IV
    TIER_4   = 4   # All sources dead — HALT new entries


class GracefulDegradationManager:
    """
    Monitors data source health and applies graceful degradation.
    Thread-safe. Runs as a background daemon.
    """

    def __init__(self, tg_bot_token: str = "", tg_group: str = "", tg_dm: str = ""):
        self._lock             = threading.Lock()
        self._tier             = DegradationTier.OK
        self._orats_healthy    = True
        self._polygon_healthy  = True
        self._last_orats_ok    = time.time()
        self._last_polygon_ok  = time.time()
        self._orats_iv_cache   = {}    # ticker → (iv, timestamp)
        self._polygon_iv_cache = {}    # ticker → (iv, timestamp)
        self._last_known_iv    = {}    # ticker → iv (used as last resort)
        self._degradation_log  = []    # list of events
        self._tg_bot_token     = tg_bot_token or TG_BOT_TOKEN
        self._tg_group         = tg_group or TG_HEALTH_GROUP
        self._tg_dm            = tg_dm or TG_AHMED_DM
        self._alerted_tier     = DegradationTier.OK
        self._probe_thread     = None
        self._running          = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Start background health probe thread."""
        self._running = True
        self._probe_thread = threading.Thread(
            target=self._probe_loop, daemon=True, name="degradation-probe"
        )
        self._probe_thread.start()
        logger.info("🛡️ Graceful Degradation Manager started")

    def stop(self):
        self._running = False

    def get_tier(self) -> DegradationTier:
        with self._lock:
            return self._tier

    def is_halted(self) -> bool:
        """TIER_4 = HALT new entries."""
        return self.get_tier() == DegradationTier.TIER_4

    def get_iv_for_ticker(self, ticker: str) -> Tuple[Optional[float], str]:
        """
        Get IV for a ticker from the best available source.
        Returns (iv_value, source_name).
        NEVER returns None silently — always includes source transparency.
        """
        tier = self.get_tier()

        # Try ORATS first (primary)
        if tier in (DegradationTier.OK, DegradationTier.TIER_1):
            iv, source = self._fetch_orats_iv(ticker)
            if iv is not None:
                return iv, f"orats (tier={tier.name})"

        # ORATS down → try Polygon
        if tier in (DegradationTier.TIER_2, DegradationTier.TIER_1) or True:
            iv, source = self._fetch_polygon_iv(ticker)
            if iv is not None:
                return iv, f"polygon_fallback (tier={tier.name})"

        # Both down → use last known good
        with self._lock:
            cached_iv = self._last_known_iv.get(ticker)
        if cached_iv is not None:
            logger.warning(f"🚨 Using last-known IV for {ticker} — all sources degraded")
            return cached_iv, "last_known_cache (DEGRADED)"

        # Absolutely nothing → TIER_4 condition
        return None, "NO_DATA (TIER_4)"

    def should_block_new_entry(self, verdict: str) -> Tuple[bool, str]:
        """
        Check if a new entry should be blocked due to degradation.
        
        SACRED RULES:
        - CONDITIONAL verdict → ALWAYS HOLD (April 1 pattern — never degrade this gate)
        - TIER_4 → block ALL new entries
        - TIER_3 → block new entries (can't price them accurately)
        """
        # SACRED: CONDITIONAL → HOLD regardless of degradation state
        if verdict and verdict.upper() == "CONDITIONAL":
            return True, "CONDITIONAL→HOLD: sacred gate, never auto-approved (April 1 pattern)"

        tier = self.get_tier()
        if tier == DegradationTier.TIER_4:
            return True, f"TIER_4 HALT: all data sources dead — no new entries until recovery"
        if tier == DegradationTier.TIER_3:
            return True, f"TIER_3 BLOCK: running on cached IV only — too risky for new entries"

        return False, f"tier={tier.name}, new entries allowed"

    def get_status(self) -> dict:
        """Return current degradation status for health endpoints."""
        with self._lock:
            return {
                "tier":            self._tier.name,
                "tier_value":      self._tier.value,
                "orats_healthy":   self._orats_healthy,
                "polygon_healthy": self._polygon_healthy,
                "halt_active":     self._tier == DegradationTier.TIER_4,
                "recent_events":   self._degradation_log[-5:],
                "timestamp":       datetime.datetime.utcnow().isoformat(),
            }

    # ── Internal: Data Fetchers ───────────────────────────────────────────────

    def _fetch_orats_iv(self, ticker: str) -> Tuple[Optional[float], str]:
        """Fetch 30-day IV from ORATS summaries endpoint."""
        cache_key = f"orats_{ticker}"
        cached = self._orats_iv_cache.get(cache_key)
        if cached:
            iv, ts = cached
            if time.time() - ts < CACHE_MAX_AGE:
                return iv, "orats_cache"

        try:
            r = requests.get(
                "https://api.orats.io/datav2/summaries",
                params={"token": ORATS_API_KEY, "ticker": ticker},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    iv = data[0].get("iv30d", None)
                    if iv is not None:
                        with self._lock:
                            self._orats_iv_cache[cache_key] = (iv, time.time())
                            self._last_known_iv[ticker] = iv
                        return float(iv), "orats_live"
        except Exception as e:
            logger.warning(f"ORATS IV fetch failed for {ticker}: {e}")

        return None, "orats_failed"

    def _fetch_polygon_iv(self, ticker: str) -> Tuple[Optional[float], str]:
        """Fetch IV approximation from Polygon options snapshot."""
        cache_key = f"polygon_{ticker}"
        cached = self._polygon_iv_cache.get(cache_key)
        if cached:
            iv, ts = cached
            if time.time() - ts < CACHE_MAX_AGE:
                return iv, "polygon_cache"

        try:
            r = requests.get(
                f"https://api.polygon.io/v3/snapshot/options/{ticker}",
                params={"apiKey": POLYGON_KEY, "limit": 10},
                timeout=5,
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                ivs = []
                for opt in results:
                    iv = opt.get("greeks", {}).get("iv") or opt.get("implied_volatility")
                    if iv:
                        ivs.append(float(iv))
                if ivs:
                    avg_iv = sum(ivs) / len(ivs)
                    with self._lock:
                        self._polygon_iv_cache[cache_key] = (avg_iv, time.time())
                        self._last_known_iv[ticker] = avg_iv
                    return avg_iv, "polygon_live"
        except Exception as e:
            logger.warning(f"Polygon IV fetch failed for {ticker}: {e}")

        return None, "polygon_failed"

    # ── Internal: Probe Loop ──────────────────────────────────────────────────

    def _probe_loop(self):
        """Background thread: probe ORATS and Polygon every 30 seconds."""
        while self._running:
            try:
                self._probe_orats()
                self._probe_polygon()
                self._update_tier()
            except Exception as e:
                logger.error(f"Probe loop error: {e}")
            time.sleep(ORATS_HEALTH_INTERVAL)

    def _probe_orats(self):
        """Check ORATS health with SPY as test ticker."""
        try:
            r = requests.get(
                "https://api.orats.io/datav2/summaries",
                params={"token": ORATS_API_KEY, "ticker": "SPY"},
                timeout=5,
            )
            healthy = r.status_code == 200 and bool(r.json().get("data"))
        except Exception:
            healthy = False

        with self._lock:
            prev = self._orats_healthy
            self._orats_healthy = healthy
            if healthy:
                self._last_orats_ok = time.time()
            if prev != healthy:
                event = f"ORATS {'RECOVERED' if healthy else 'DEGRADED'} at {datetime.datetime.utcnow().isoformat()}"
                self._degradation_log.append(event)
                logger.warning(f"🛡️ {event}")

    def _probe_polygon(self):
        """Check Polygon health with a simple ticker lookup."""
        try:
            r = requests.get(
                "https://api.polygon.io/v2/aggs/ticker/SPY/prev",
                params={"apiKey": POLYGON_KEY},
                timeout=5,
            )
            healthy = r.status_code == 200
        except Exception:
            healthy = False

        with self._lock:
            prev = self._polygon_healthy
            self._polygon_healthy = healthy
            if healthy:
                self._last_polygon_ok = time.time()
            if prev != healthy:
                event = f"Polygon {'RECOVERED' if healthy else 'DEGRADED'} at {datetime.datetime.utcnow().isoformat()}"
                self._degradation_log.append(event)
                logger.warning(f"🛡️ {event}")

    def _update_tier(self):
        """Compute current degradation tier and alert if changed."""
        with self._lock:
            orats_ok   = self._orats_healthy
            polygon_ok = self._polygon_healthy
            orats_age  = time.time() - self._last_orats_ok

        if orats_ok:
            new_tier = DegradationTier.OK
        elif not orats_ok and polygon_ok:
            new_tier = DegradationTier.TIER_2  # ORATS down, Polygon fallback
        elif not orats_ok and not polygon_ok:
            # Check if we have cached data
            if self._last_known_iv:
                new_tier = DegradationTier.TIER_3
            else:
                new_tier = DegradationTier.TIER_4

        # Alert on tier change
        with self._lock:
            prev_tier = self._tier
            self._tier = new_tier

        if new_tier != prev_tier:
            self._on_tier_change(prev_tier, new_tier)

    def _on_tier_change(self, prev: DegradationTier, new: DegradationTier):
        """Handle tier transition — log and alert."""
        direction = "⬆️ DEGRADING" if new.value > prev.value else "✅ RECOVERING"
        msg = (
            f"🛡️ NEXUS DEGRADATION MANAGER\n"
            f"{direction}: {prev.name} → {new.name}\n"
            f"ORATS: {'✅' if self._orats_healthy else '❌'} | "
            f"Polygon: {'✅' if self._polygon_healthy else '❌'}\n"
        )
        if new == DegradationTier.TIER_4:
            msg += "⛔ TIER 4: ALL DATA SOURCES DEAD — NEW ENTRIES HALTED"
        elif new == DegradationTier.TIER_3:
            msg += "🚨 TIER 3: Running on cached IV — new entries blocked"
        elif new == DegradationTier.TIER_2:
            msg += "⚠️ TIER 2: ORATS down — Polygon IV fallback active"
        elif new == DegradationTier.OK:
            msg += "All systems nominal — full trading resumed"

        logger.warning(msg)
        self._send_telegram(msg)

    def _send_telegram(self, text: str):
        """Send alert to health group and Ahmed DM."""
        if not self._tg_bot_token:
            return
        for chat_id in [self._tg_group, self._tg_dm]:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self._tg_bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=5,
                )
            except Exception as e:
                logger.error(f"Telegram alert failed: {e}")


# ── Module-level singleton ─────────────────────────────────────────────────────
_degradation_manager: Optional[GracefulDegradationManager] = None


def get_degradation_manager() -> GracefulDegradationManager:
    """Get or create the singleton degradation manager."""
    global _degradation_manager
    if _degradation_manager is None:
        _degradation_manager = GracefulDegradationManager()
        _degradation_manager.start()
    return _degradation_manager


def check_new_entry_allowed(verdict: str) -> Tuple[bool, str]:
    """
    Module-level helper: can we enter a new position?
    Wraps the degradation manager's sacred gate check.
    """
    return get_degradation_manager().should_block_new_entry(verdict)


def get_iv(ticker: str) -> Tuple[Optional[float], str]:
    """Module-level helper: get IV with graceful degradation."""
    return get_degradation_manager().get_iv_for_ticker(ticker)


if __name__ == "__main__":
    print("🛡️ Testing Graceful Degradation Manager...")
    mgr = GracefulDegradationManager()
    mgr.start()
    time.sleep(5)
    print(f"Status: {mgr.get_status()}")

    # Test sacred gate
    blocked, reason = mgr.should_block_new_entry("CONDITIONAL")
    print(f"CONDITIONAL verdict blocked: {blocked} — {reason}")

    iv, source = mgr.get_iv_for_ticker("SPY")
    print(f"SPY IV: {iv} from {source}")
