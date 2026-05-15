"""
greeks_tracker.py — Portfolio-Level Greeks Tracker for Alpha Execution
Aggregates delta, gamma, vega, theta across all open credit spread positions.

Refresh cycle: 15 minutes (background thread).
Greeks source: ORATS strikes API (falls back to last cached on failure).
Exposed via: GET /greeks (X-Nexus-Secret auth, handled by main.py)

Risk flags generated when:
    |net_vega|  > 5.0   → HIGH_VEGA_EXPOSURE
    |net_delta| > 0.5   → HIGH_DELTA_EXPOSURE
    |net_gamma| > 0.15  → HIGH_GAMMA_EXPOSURE
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
VEGA_WARN_THRESHOLD  = 5.0
DELTA_WARN_THRESHOLD = 0.5
GAMMA_WARN_THRESHOLD = 0.15

# ── ORATS ──────────────────────────────────────────────────────────────────────
ORATS_TOKEN = os.getenv("ORATS_API_KEY", "4476e955-241a-4540-b114-ebbf1a3a3b87")
ORATS_STRIKES_URL = "https://api.orats.io/datav2/strikes"

# ── Refresh interval ──────────────────────────────────────────────────────────
REFRESH_INTERVAL_S = 900  # 15 minutes


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PositionGreeks:
    """Per-position Greek values."""
    ticker:        str
    direction:     str       # "bullish" | "bearish"
    short_strike:  float
    long_strike:   float
    expiration:    str
    contracts:     int
    net_delta:     float
    net_gamma:     float
    net_vega:      float
    net_theta:     float
    stale:         bool = False


@dataclass
class RiskFlag:
    """A single portfolio risk flag."""
    type:        str
    description: str
    severity:    str   # "WARNING" | "CRITICAL"


@dataclass
class GreeksPacket:
    """Full portfolio Greeks snapshot."""
    timestamp:          str
    position_count:     int
    stale:              bool
    portfolio_greeks:   dict              # net_delta/gamma/vega/theta
    risk_flags:         list              # list of RiskFlag dicts
    per_position:       list              # list of PositionGreeks dicts
    last_refresh_error: Optional[str] = None


# ── Main tracker class ────────────────────────────────────────────────────────

class GreeksTracker:
    """
    Background thread that refreshes portfolio Greeks every 15 minutes.

    Usage (in main.py startup):
        tracker = GreeksTracker(db_path=settings.alpha_db_path)
        tracker.start()

    Query:
        packet = tracker.get()   # returns latest GreeksPacket dict
    """

    def __init__(self, db_path: str) -> None:
        """
        Args:
            db_path: Path to alpha_execution.db (reads open positions).
        """
        self._db_path      = db_path
        self._packet: Optional[GreeksPacket] = None
        self._lock         = threading.Lock()
        self._stop         = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None

    def start(self) -> None:
        """Start background refresh thread (daemon)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="greeks-tracker", daemon=True
        )
        self._thread.start()
        logger.info("[GREEKS] Tracker started — refresh every %ds", REFRESH_INTERVAL_S)

    def stop(self) -> None:
        """Signal the tracker to stop."""
        self._stop.set()

    def get(self) -> dict:
        """
        Return the latest GreeksPacket as a dict.
        If no data yet, returns a zero packet with stale=True.
        """
        with self._lock:
            if self._packet is None:
                return self._zero_packet()
            return asdict(self._packet)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Main refresh loop."""
        # Immediate first run
        self._refresh()
        while not self._stop.wait(timeout=REFRESH_INTERVAL_S):
            self._refresh()

    def _refresh(self) -> None:
        """Fetch open positions → look up Greeks → build packet → store."""
        try:
            positions = self._load_open_positions()
            if not positions:
                packet = GreeksPacket(
                    timestamp=_now_iso(),
                    position_count=0,
                    stale=False,
                    portfolio_greeks={"net_delta": 0.0, "net_gamma": 0.0,
                                      "net_vega": 0.0, "net_theta": 0.0},
                    risk_flags=[],
                    per_position=[],
                )
                with self._lock:
                    self._packet = packet
                return

            position_greeks_list = []
            for pos in positions:
                pg = self._greeks_for_position(pos)
                position_greeks_list.append(pg)

            # Aggregate
            net_delta = sum(p.net_delta for p in position_greeks_list)
            net_gamma = sum(p.net_gamma for p in position_greeks_list)
            net_vega  = sum(p.net_vega  for p in position_greeks_list)
            net_theta = sum(p.net_theta for p in position_greeks_list)
            any_stale = any(p.stale for p in position_greeks_list)

            risk_flags = _compute_risk_flags(net_delta, net_gamma, net_vega, net_theta)

            packet = GreeksPacket(
                timestamp=_now_iso(),
                position_count=len(position_greeks_list),
                stale=any_stale,
                portfolio_greeks={
                    "net_delta": round(net_delta, 4),
                    "net_gamma": round(net_gamma, 4),
                    "net_vega":  round(net_vega, 4),
                    "net_theta": round(net_theta, 4),
                },
                risk_flags=[asdict(f) for f in risk_flags],
                per_position=[asdict(p) for p in position_greeks_list],
                last_refresh_error=None,
            )
            with self._lock:
                self._packet = packet
                self._last_error = None
            logger.info(
                "[GREEKS] Refreshed: %d positions | Δ=%.3f Γ=%.4f V=%.3f Θ=%.3f | flags=%d",
                len(position_greeks_list), net_delta, net_gamma, net_vega, net_theta,
                len(risk_flags),
            )

        except Exception as e:
            err = str(e)
            logger.error("[GREEKS] Refresh failed: %s", err, exc_info=True)
            # Keep last packet, mark stale
            with self._lock:
                self._last_error = err
                if self._packet is not None:
                    self._packet.stale = True
                    self._packet.last_refresh_error = err

    def _load_open_positions(self) -> list:
        """Load open positions from alpha_execution.db."""
        if not os.path.exists(self._db_path):
            return []
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT ticker, direction, short_strike, long_strike,
                          expiration_date, contracts
                   FROM positions
                   WHERE status IN ('open', 'pending')"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _greeks_for_position(self, pos: dict) -> PositionGreeks:
        """
        Fetch Greeks from ORATS for both legs of a credit spread.
        Returns net Greeks for the spread (short - long or vice versa depending on direction).

        Credit spread convention:
            Bull put spread:  short higher put + long lower put → net positive delta
            Bear call spread: short higher call + long lower call → net negative delta

        Greeks for short leg are negated (selling = flipped sign).
        Greeks for long leg are natural.
        Net = -(short_greeks) + long_greeks

        Fails safe: returns zero Greeks with stale=True if ORATS unavailable.
        """
        ticker     = pos["ticker"]
        direction  = pos["direction"]   # "bullish" | "bearish"
        short_k    = pos["short_strike"]
        long_k     = pos["long_strike"]
        expiry     = pos["expiration_date"]   # YYYY-MM-DD
        contracts  = pos["contracts"]

        opt_type = "put" if direction == "bullish" else "call"

        try:
            short_g = self._fetch_orats_greeks(ticker, expiry, short_k, opt_type)
            long_g  = self._fetch_orats_greeks(ticker, expiry, long_k,  opt_type)

            if short_g is None or long_g is None:
                raise ValueError(f"ORATS returned None for {ticker} {short_k}/{long_k}")

            # Spread net Greeks (per contract):
            # Short leg: flip sign (selling = opposite exposure)
            # Long leg: natural sign
            # Then multiply by contracts
            mult = contracts
            net_delta = (-short_g["delta"] + long_g["delta"]) * mult
            net_gamma = (-short_g["gamma"] + long_g["gamma"]) * mult
            net_vega  = (-short_g["vega"]  + long_g["vega"])  * mult
            net_theta = (-short_g["theta"] + long_g["theta"]) * mult

            return PositionGreeks(
                ticker=ticker, direction=direction,
                short_strike=short_k, long_strike=long_k,
                expiration=expiry, contracts=contracts,
                net_delta=round(net_delta, 4),
                net_gamma=round(net_gamma, 4),
                net_vega=round(net_vega, 4),
                net_theta=round(net_theta, 4),
                stale=False,
            )

        except Exception as e:
            logger.warning("[GREEKS] ORATS fetch failed for %s %s/%s: %s — using zeros",
                           ticker, short_k, long_k, e)
            return PositionGreeks(
                ticker=ticker, direction=direction,
                short_strike=short_k, long_strike=long_k,
                expiration=expiry, contracts=contracts,
                net_delta=0.0, net_gamma=0.0, net_vega=0.0, net_theta=0.0,
                stale=True,
            )

    def _fetch_orats_greeks(
        self, ticker: str, expiry: str, strike: float, opt_type: str
    ) -> Optional[dict]:
        """
        Fetch ORATS greeks for a specific contract.
        Returns dict with delta, gamma, vega, theta, or None on failure.

        ORATS convention: delta field is the call delta (positive).
        Put delta = 1 - call_delta? No: ORATS gives the raw put/call delta.
        For puts: delta is negative (e.g. -0.30).
        For calls: delta is positive (e.g. +0.45).
        """
        # Parse expiry YYYY-MM-DD to compute DTE
        try:
            exp_dt = datetime.strptime(expiry, "%Y-%m-%d")
            dte = (exp_dt - datetime.utcnow()).days
        except ValueError:
            dte = 30  # safe default

        resp = requests.get(
            ORATS_STRIKES_URL,
            params={
                "token": ORATS_TOKEN,
                "ticker": ticker,
                "dte": f"{max(dte-5,1)},{dte+5}",  # ±5 day window
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])

        if not data:
            return None

        # Find closest strike + expiry match
        best = None
        best_dist = float("inf")
        for row in data:
            row_expiry = row.get("expirDate", "")
            row_strike = float(row.get("strike", 0))
            dist = abs(row_strike - strike)

            # Prefer exact expiry match
            expiry_match = row_expiry == expiry
            if expiry_match:
                dist -= 10000  # heavy preference for exact expiry

            if dist < best_dist:
                best_dist = dist
                best = row

        if best is None:
            return None

        # ORATS delta is call delta. For put: put_delta = delta - 1 (by put-call parity)
        call_delta = float(best.get("delta", 0))
        if opt_type == "put":
            greek_delta = call_delta - 1.0
        else:
            greek_delta = call_delta

        return {
            "delta": greek_delta,
            "gamma": float(best.get("gamma", 0)),
            "vega":  float(best.get("vega", 0)),
            "theta": float(best.get("theta", 0)),
        }

    @staticmethod
    def _zero_packet() -> dict:
        """Return a zero-value packet (no positions loaded yet)."""
        return {
            "timestamp": _now_iso(),
            "position_count": 0,
            "stale": True,
            "portfolio_greeks": {"net_delta": 0.0, "net_gamma": 0.0,
                                 "net_vega": 0.0, "net_theta": 0.0},
            "risk_flags": [],
            "per_position": [],
            "last_refresh_error": "No data yet — initial refresh pending",
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_risk_flags(
    net_delta: float, net_gamma: float, net_vega: float, net_theta: float
) -> list:
    """Generate risk flags based on portfolio Greek thresholds."""
    flags = []

    if abs(net_vega) > VEGA_WARN_THRESHOLD:
        vol_cost = abs(net_vega) * 5 * 100  # 5pt vol spike × $100 multiplier
        flags.append(RiskFlag(
            type="HIGH_VEGA_EXPOSURE",
            description=(
                f"Net short vega {net_vega:.2f} — "
                f"vol spike of 5pts costs ~${vol_cost:,.0f}"
            ),
            severity="WARNING" if abs(net_vega) < 10.0 else "CRITICAL",
        ))

    if abs(net_delta) > DELTA_WARN_THRESHOLD:
        flags.append(RiskFlag(
            type="HIGH_DELTA_EXPOSURE",
            description=f"Net delta {net_delta:.3f} — directional risk elevated",
            severity="WARNING" if abs(net_delta) < 1.0 else "CRITICAL",
        ))

    if abs(net_gamma) > GAMMA_WARN_THRESHOLD:
        flags.append(RiskFlag(
            type="HIGH_GAMMA_EXPOSURE",
            description=(
                f"Net gamma {net_gamma:.4f} — "
                "delta sensitivity high, adverse moves amplify quickly"
            ),
            severity="WARNING",
        ))

    return flags
