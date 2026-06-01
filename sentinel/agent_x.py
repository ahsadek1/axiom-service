"""
agent_x.py — NEXUS Universal Substitute Agent
===============================================
Agent X is a hot standby that activates automatically when any critical
agent goes silent during market hours.

It does NOT try to match the real agent's intelligence.
It keeps the pipeline flowing at reduced quality while recovery runs.
A deterministic 70% substitute beats zero trades from a silent agent.

Substitution modes:
  CIPHER-X  — Technical scoring via backtest win rates + price momentum
  ATLAS-X   — IV/options scoring via ORACLE IV rank data
  SAGE-X    — Macro scoring via Axiom regime classification
  OMNI-X    — Direct concordance bridge bypassing OMNI

Activation: Agent silent 5+ min during market hours
Deactivation: Real agent resumes submissions (auto-detected)
Reporting: FYI to NEXUS HEALTH GROUP — always transparent

Ahmed directive May 2026:
  "Create a substitute agent that gets pulled deterministically to fill in
   for any agent that goes down, maintaining the trading process while
   the real agent gets fixed."
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("nexus.agent_x")
_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEXUS_SECRET    = os.getenv("NEXUS_WEBHOOK_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
HEALTH_GROUP    = os.getenv("NEXUS_HEALTH_GROUP_ID", "-5241272802")
BACKTEST_DB     = "/Users/ahmedsadek/nexus/data/backtest.db"
ALPHA_BUFFER_DB = "/Users/ahmedsadek/nexus/data/alpha_buffer.db"
ALPHA_BUFFER_URL = "http://localhost:8002"
AXIOM_URL       = "http://localhost:8001"
ORACLE_URL      = "http://localhost:8007"
OMNI_URL        = "http://localhost:8004"
ALPHA_EXEC_URL  = "http://localhost:8005"

# How many top tickers Agent X submits per cycle
TOP_TICKERS_PER_CYCLE = 10

# Minimum score to submit
MIN_SCORE = 60.0

# ---------------------------------------------------------------------------
# Active substitution state
# ---------------------------------------------------------------------------

_active_substitutions: set[str] = set()  # which agents are currently being substituted


# ---------------------------------------------------------------------------
# FYI notification
# ---------------------------------------------------------------------------

def _fyi(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": HEALTH_GROUP, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_current_regime() -> str:
    """Get current market regime from Axiom."""
    try:
        resp = requests.get(f"{AXIOM_URL}/health", timeout=4)
        return resp.json().get("regime", "NORMAL")
    except Exception:
        return "NORMAL"


def _get_current_window_id() -> str:
    """Generate current window ID (matches alpha-buffer format)."""
    now = datetime.now(_ET)
    return now.strftime("%Y-%m-%d-%H%M")


def _get_last_submission(agent: str) -> Optional[datetime]:
    """Get last submission time for an agent."""
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=5)
        row = conn.execute(
            "SELECT MAX(received_at) FROM submissions WHERE agent = ?",
            (agent,),
        ).fetchone()
        conn.close()
        if row and row[0]:
            ts = row[0].replace("Z", "+00:00")
            return datetime.fromisoformat(ts)
    except Exception:
        pass
    return None


def _agent_recovered(agent: str, activation_time: datetime) -> bool:
    """Check if real agent has submitted since Agent X activated."""
    last = _get_last_submission(agent)
    if last is None:
        return False
    return last > activation_time


def _submit_to_alpha_buffer(
    agent_name: str,
    ticker: str,
    direction: str,
    score: float,
    reasoning: str,
) -> bool:
    """Submit a pick to alpha-buffer as Agent X substitute."""
    window_id = _get_current_window_id()
    try:
        resp = requests.post(
            f"{ALPHA_BUFFER_URL}/submit",
            json={
                "agent":     agent_name,
                "ticker":    ticker,
                "direction": direction,
                "score":     round(score, 1),
                "reasoning": reasoning,
                "window_id": window_id,
            },
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=10,
        )
        return resp.status_code in (200, 201, 202)
    except Exception as exc:
        log.warning("AgentX submit failed for %s/%s: %s", ticker, agent_name, exc)
        return False


# ---------------------------------------------------------------------------
# CIPHER-X — Technical scoring via backtest win rates
# ---------------------------------------------------------------------------

def run_cipher_x() -> int:
    """
    Substitute for Cipher (technical analysis).
    Uses backtest bull_put_spread win rates as proxy for technical quality.
    High win rate in current regime = technically sound setup.
    Returns number of picks submitted.
    """
    regime = _get_current_regime()
    submitted = 0

    try:
        conn = sqlite3.connect(BACKTEST_DB, timeout=5)
        rows = conn.execute(
            """
            SELECT ticker, win_rate, sample_count
            FROM historical_win_rates
            WHERE strategy = 'bull_put_spread'
              AND regime = ?
              AND direction = 'bullish'
              AND sample_count >= 20
              AND win_rate >= 0.85
            ORDER BY win_rate DESC
            LIMIT ?
            """,
            (regime, TOP_TICKERS_PER_CYCLE),
        ).fetchall()
        conn.close()

        for ticker, win_rate, samples in rows:
            # Scale win rate to score: 85% → 70, 99% → 89
            score = 50 + (win_rate * 40)
            score = min(89.0, max(MIN_SCORE, score))

            ok = _submit_to_alpha_buffer(
                agent_name = "Cipher",
                ticker     = ticker,
                direction  = "bullish",
                score      = score,
                reasoning  = (
                    f"[CIPHER-X] Backtest win_rate={win_rate:.1%} in {regime} "
                    f"({samples} samples). Deterministic substitute active."
                ),
            )
            if ok:
                submitted += 1

    except Exception as exc:
        log.error("CIPHER-X error: %s", exc)

    log.info("CIPHER-X submitted %d picks (regime=%s)", submitted, regime)
    return submitted


# ---------------------------------------------------------------------------
# ATLAS-X — IV/options scoring via ORACLE
# ---------------------------------------------------------------------------

def run_atlas_x() -> int:
    """
    Substitute for Atlas (IV/options analysis).
    Uses ORACLE IV rank data — low IV rank = good for credit spreads.
    Returns number of picks submitted.
    """
    regime = _get_current_regime()
    submitted = 0

    try:
        # Get top tickers from backtest DB
        conn = sqlite3.connect(BACKTEST_DB, timeout=5)
        tickers = [row[0] for row in conn.execute(
            """
            SELECT DISTINCT ticker FROM historical_win_rates
            WHERE strategy = 'bull_put_spread'
              AND regime = ?
              AND win_rate >= 0.90
            ORDER BY win_rate DESC LIMIT 20
            """,
            (regime,),
        ).fetchall()]
        conn.close()

        for ticker in tickers[:TOP_TICKERS_PER_CYCLE]:
            # Query ORACLE for IV rank
            try:
                resp = requests.get(
                    f"{ORACLE_URL}/volatility/{ticker}",
                    headers={"X-Nexus-Secret": NEXUS_SECRET},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    iv_rank = data.get("iv_rank", 50)
                    # Low IV rank (< 50) = good for credit spreads
                    # Score: IV rank 10 → score 85, IV rank 50 → score 65
                    score = max(MIN_SCORE, 90 - (iv_rank * 0.5))
                else:
                    # No IV data — use moderate score
                    score = 65.0
                    iv_rank = "N/A"
            except Exception:
                score = 65.0
                iv_rank = "N/A"

            ok = _submit_to_alpha_buffer(
                agent_name = "Atlas",
                ticker     = ticker,
                direction  = "bullish",
                score      = score,
                reasoning  = (
                    f"[ATLAS-X] IV_rank={iv_rank} regime={regime}. "
                    f"Deterministic substitute active."
                ),
            )
            if ok:
                submitted += 1

    except Exception as exc:
        log.error("ATLAS-X error: %s", exc)

    log.info("ATLAS-X submitted %d picks", submitted)
    return submitted


# ---------------------------------------------------------------------------
# SAGE-X — Macro scoring via Axiom regime
# ---------------------------------------------------------------------------

def run_sage_x() -> int:
    """
    Substitute for Sage (macro analysis).
    Uses Axiom regime classification as macro proxy.
    NORMAL → favorable, ELEVATED → cautious, STRESS → defensive.
    Returns number of picks submitted.
    """
    regime = _get_current_regime()

    # Regime-based base score
    regime_scores = {
        "LOW_VOL":    85,
        "NORMAL":     80,
        "ELEVATED":   70,
        "STRESS":     60,
        "HIGH_STRESS": 55,
        "CRISIS":     50,
    }
    base_score = regime_scores.get(regime, 70)

    if base_score < MIN_SCORE:
        log.info("SAGE-X: regime %s too risky — no picks submitted", regime)
        return 0

    submitted = 0

    try:
        conn = sqlite3.connect(BACKTEST_DB, timeout=5)
        rows = conn.execute(
            """
            SELECT ticker, win_rate FROM historical_win_rates
            WHERE strategy = 'bull_put_spread'
              AND regime = ?
              AND direction = 'bullish'
              AND win_rate >= 0.90
              AND sample_count >= 20
            ORDER BY win_rate DESC LIMIT ?
            """,
            (regime, TOP_TICKERS_PER_CYCLE),
        ).fetchall()
        conn.close()

        for ticker, win_rate in rows:
            score = min(85.0, base_score * win_rate)
            score = max(MIN_SCORE, score)

            ok = _submit_to_alpha_buffer(
                agent_name = "Sage",
                ticker     = ticker,
                direction  = "bullish",
                score      = score,
                reasoning  = (
                    f"[SAGE-X] Regime={regime} base_score={base_score} "
                    f"win_rate={win_rate:.1%}. Deterministic substitute active."
                ),
            )
            if ok:
                submitted += 1

    except Exception as exc:
        log.error("SAGE-X error: %s", exc)

    log.info("SAGE-X submitted %d picks (regime=%s, base_score=%d)", submitted, regime, base_score)
    return submitted


# ---------------------------------------------------------------------------
# OMNI-X — Direct concordance bridge
# ---------------------------------------------------------------------------

def run_omni_x() -> int:
    """
    Substitute for OMNI (concordance engine).
    When OMNI is down, Agent X directly evaluates pending concordances
    and routes GO verdicts to alpha-execution.
    This is the most critical substitution — keeps execution alive.
    Returns number of verdicts issued.
    """
    verdicts = 0
    try:
        # Get undispatched concordances from alpha-buffer
        resp = requests.get(
            f"{ALPHA_BUFFER_URL}/concordance/pending",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=10,
        )
        if resp.status_code != 200:
            return 0

        pending = resp.json().get("concordances", [])

        for concordance in pending:
            ticker         = concordance.get("ticker")
            direction      = concordance.get("direction")
            weighted_score = concordance.get("weighted_score", 0)
            pathway        = concordance.get("pathway", "P2")
            window_id      = concordance.get("window_id")

            # Deterministic threshold check
            thresholds = {"P1": 64.0, "P2": 63.0, "P3": 68.0, "P4": 68.0}
            threshold = thresholds.get(pathway, 65.0)

            if weighted_score < threshold:
                continue

            verdict = "STRONG_GO" if weighted_score >= 75 else "GO"
            sizing  = 1.0 if verdict == "STRONG_GO" else 0.75

            # Route directly to alpha-execution
            payload = {
                "ticker":            ticker,
                "direction":         direction,
                "system":            "alpha",
                "pathway":           pathway,
                "weighted_score":    weighted_score,
                "verdict":           verdict,
                "sizing_mult":       sizing,
                "position_size_usd": 2000 * sizing,
                "window_id":         window_id,
                "agent_scores":      concordance.get("scores", {}),
                "brain_summary":     {"omni_x": f"Deterministic substitute | score={weighted_score}"},
                "auto_execute":      True,
            }

            try:
                exec_resp = requests.post(
                    f"{ALPHA_EXEC_URL}/execute",
                    json=payload,
                    headers={"X-Nexus-Secret": NEXUS_SECRET},
                    timeout=10,
                )
                if exec_resp.status_code in (200, 201, 202):
                    verdicts += 1
                    log.info("OMNI-X: %s %s %s → %s routed to execution",
                             ticker, direction, pathway, verdict)
            except Exception as exc:
                log.warning("OMNI-X execution route failed for %s: %s", ticker, exc)

    except Exception as exc:
        log.error("OMNI-X error: %s", exc)

    if verdicts > 0:
        log.info("OMNI-X issued %d verdicts", verdicts)
    return verdicts


# ---------------------------------------------------------------------------
# Agent X Controller
# ---------------------------------------------------------------------------

class AgentX:
    """
    Universal substitute agent controller.
    Monitors agent health and activates substitution as needed.
    """

    def __init__(self) -> None:
        self._active: dict[str, datetime] = {}  # agent → activation time
        self._last_fyi: dict[str, float] = {}   # agent → last FYI timestamp

    def _market_hours(self) -> bool:
        now = datetime.now(_ET)
        return (
            now.weekday() < 5
            and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
            and now.hour < 16
        )

    def _agent_silent(self, agent: str, threshold_minutes: int = 5) -> bool:
        """Check if agent has been silent for threshold_minutes."""
        last = _get_last_submission(agent)
        if last is None:
            return True
        minutes_ago = (datetime.now(timezone.utc) - last).total_seconds() / 60
        return minutes_ago > threshold_minutes

    def _should_fyi(self, key: str, interval_seconds: int = 300) -> bool:
        """Rate-limit FYI messages — don't spam."""
        now = time.time()
        last = self._last_fyi.get(key, 0)
        if now - last > interval_seconds:
            self._last_fyi[key] = now
            return True
        return False

    def tick(self) -> None:
        """
        Run one Agent X evaluation cycle.
        Only activates during market hours.
        Activation requires: silent > 5min AND recovery already attempted.
        """
        # Agent X NEVER activates outside market hours
        # Agents are quiet by design when market is closed
        if not self._market_hours():
            if self._active:
                for agent in list(self._active.keys()):
                    self._deactivate(agent)
            return

        # Skip first 15 minutes of market open — agents warming up
        now_et = datetime.now(_ET)
        if now_et.hour == 9 and now_et.minute < 45:
            return

        for agent, substitute_fn, threshold in [
            ("Cipher", run_cipher_x, 10),  # 10 min: recovery attempted first at 5 min
            ("Atlas",  run_atlas_x,  10),  # 10 min: recovery attempted first at 5 min
            ("Sage",   run_sage_x,   10),  # 10 min: Manus down confirmed
        ]:
            if self._agent_silent(agent, threshold):
                if agent not in self._active:
                    self._activate(agent)
                else:
                    # Check if real agent recovered
                    if _agent_recovered(agent, self._active[agent]):
                        self._deactivate(agent)
                        continue
                    # Still silent — run substitute
                    count = substitute_fn()
                    if count > 0 and self._should_fyi(f"{agent}_active"):
                        _fyi(
                            f"🤖 <b>AGENT-X ACTIVE: {agent.upper()}-X</b>\n"
                            f"Submitted {count} deterministic picks.\n"
                            f"Real {agent} still silent — recovery in progress."
                        )
            else:
                # Agent is healthy — deactivate if was substituting
                if agent in self._active:
                    self._deactivate(agent)

        # Check OMNI separately
        try:
            resp = requests.get("http://localhost:8004/health", timeout=4)
            omni_ok = resp.status_code == 200 and resp.json().get("status") == "healthy"
        except Exception:
            omni_ok = False

        if not omni_ok:
            if "omni" not in self._active:
                self._activate("omni")
            count = run_omni_x()
            if count > 0 and self._should_fyi("omni_active"):
                _fyi(
                    f"🤖 <b>AGENT-X ACTIVE: OMNI-X</b>\n"
                    f"Issued {count} direct verdicts.\n"
                    f"Real OMNI down — execution pipeline maintained."
                )
        else:
            if "omni" in self._active:
                self._deactivate("omni")

    def _activate(self, agent: str) -> None:
        self._active[agent] = datetime.now(timezone.utc)
        log.warning("AGENT-X ACTIVATED: %s-X", agent.upper())
        _fyi(
            f"🟡 <b>AGENT-X ACTIVATING: {agent.upper()}-X</b>\n"
            f"{agent} has been silent {5}+ min during market hours.\n"
            f"Deterministic substitute now filling in.\n"
            f"Real {agent} recovery running in parallel."
        )

    def _deactivate(self, agent: str) -> None:
        if agent in self._active:
            duration = (datetime.now(timezone.utc) - self._active[agent]).total_seconds() / 60
            del self._active[agent]
            log.info("AGENT-X DEACTIVATED: %s recovered after %.0f min", agent, duration)
            _fyi(
                f"✅ <b>AGENT-X STANDING DOWN: {agent.upper()}</b>\n"
                f"Real {agent} has resumed submissions.\n"
                f"Substitution active for {duration:.0f} minutes.\n"
                f"Pipeline fully restored."
            )


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------

_agent_x = AgentX()


def run_agent_x_tick() -> None:
    """Called every 5 minutes by the heartbeat monitor."""
    _agent_x.tick()
