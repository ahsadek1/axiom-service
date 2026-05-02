#!/usr/bin/env python3
"""
mock_trading_engine.py — Nexus v1 Pipeline Mock Trading Engine
==============================================================
10 mock trades. Standalone (no local services required).
Simulates: screening → Axiom risk gates → OMNI synthesis → verdict → execution sim.
Railway service health is checked for live context.
"""

import json
import random
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pytz

ET = pytz.timezone("America/New_York")
NOW = datetime.now(ET)

RAILWAY_HEALTH_URL = "https://worker-production-2060.up.railway.app/health"
import os as _os_mte
TELEGRAM_TOKEN     = _os_mte.getenv("TELEGRAM_BOT_TOKEN") or ""
AHMED_CHAT_ID      = "8573754783"
HEALTH_GROUP_ID    = "-1003954790884"

# ── Mock universe ─────────────────────────────────────────────────────────────
MOCK_PICKS = [
    {"ticker": "NVDA", "direction": "CALL", "strike": 875.0, "expiry_dte": 35, "ivr": 52, "premium": 4.20, "delta": 0.42, "agent": "Cipher"},
    {"ticker": "AAPL", "direction": "CALL", "strike": 190.0, "expiry_dte": 28, "ivr": 38, "premium": 2.85, "delta": 0.38, "agent": "Atlas"},
    {"ticker": "TSLA", "direction": "PUT",  "strike": 165.0, "expiry_dte": 42, "ivr": 71, "premium": 6.50, "delta": -0.45, "agent": "Sage"},
    {"ticker": "SPY",  "direction": "CALL", "strike": 520.0, "expiry_dte": 14, "ivr": 22, "premium": 1.90, "delta": 0.30, "agent": "Cipher"},  # DTE violation
    {"ticker": "META", "direction": "CALL", "strike": 480.0, "expiry_dte": 33, "ivr": 44, "premium": 5.10, "delta": 0.40, "agent": "Atlas"},
    {"ticker": "AMZN", "direction": "PUT",  "strike": 175.0, "expiry_dte": 29, "ivr": 18, "premium": 3.20, "delta": -0.35, "agent": "Sage"},   # IVR too low
    {"ticker": "MSFT", "direction": "CALL", "strike": 415.0, "expiry_dte": 45, "ivr": 58, "premium": 4.70, "delta": 0.43, "agent": "Cipher"},
    {"ticker": "GOOGL", "direction": "CALL", "strike": 170.0, "expiry_dte": 38, "ivr": 6,  "premium": 2.10, "delta": 0.36, "agent": "Atlas"},  # IVR too low (Axiom block)
    {"ticker": "QQQ",  "direction": "PUT",  "strike": 430.0, "expiry_dte": 31, "ivr": 61, "premium": 5.90, "delta": -0.44, "agent": "Cipher"},
    {"ticker": "COIN", "direction": "CALL", "strike": 220.0, "expiry_dte": 25, "ivr": 88, "premium": 9.80, "delta": 0.48, "agent": "Sage"},    # High IV ok, but position cap
]

@dataclass
class MockTradeResult:
    trade_id: int
    ticker: str
    direction: str
    strike: float
    expiry_dte: int
    ivr: float
    premium: float
    agent: str
    axiom_passed: bool
    omni_verdict: str         # GO / NO-GO / BLOCKED
    confidence: int           # 0-100
    block_reason: Optional[str] = None
    simulated_pnl: Optional[float] = None
    elapsed_ms: float = 0.0
    pipeline_steps: list = field(default_factory=list)

@dataclass
class SessionReport:
    date: str
    started_at: str
    finished_at: str
    duration_s: float
    trades: list = field(default_factory=list)
    go_count: int = 0
    no_go_count: int = 0
    blocked_count: int = 0
    railway_healthy: bool = False
    railway_version: str = "?"
    scanner_active: bool = False
    last_real_verdict: str = "N/A"
    total_simulated_pnl: float = 0.0

# ── Pipeline simulation ───────────────────────────────────────────────────────

def check_railway_health() -> dict:
    try:
        req = urllib.request.Request(RAILWAY_HEALTH_URL, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"__error": str(e)}

def axiom_risk_gates(pick: dict, open_positions: int) -> tuple[bool, Optional[str]]:
    """Simulate Axiom's 11 risk layers for options."""
    # Layer 1: DTE check (25-60 required)
    if pick["expiry_dte"] < 21:
        return False, f"DTE {pick['expiry_dte']} < 21 — too short"
    if pick["expiry_dte"] > 60:
        return False, f"DTE {pick['expiry_dte']} > 60 — too far out"
    # Layer 2: IVR check (need ≥ 15)
    if pick["ivr"] < 15:
        return False, f"IVR {pick['ivr']} < 15 — premium too cheap for credit spread"
    # Layer 3: Position cap (max 3)
    if open_positions >= 3:
        return False, f"Position cap reached ({open_positions}/3 open)"
    # Layer 4: Premium sanity
    if pick["premium"] < 0.50:
        return False, f"Premium ${pick['premium']} too low — insufficient edge"
    # Layers 5-11 pass for mock purposes
    return True, None

def omni_synthesis(pick: dict, axiom_passed: bool) -> tuple[str, int, list]:
    """Simulate OMNI 3-model synthesis."""
    steps = []
    
    if not axiom_passed:
        return "BLOCKED", 0, ["AXIOM BLOCK — synthesis skipped"]
    
    # Claude analysis
    claude_score = random.randint(55, 90)
    steps.append(f"Claude: score={claude_score} | momentum+macro alignment")
    
    # Gemini vol analysis
    iv_context = "elevated" if pick["ivr"] > 50 else "moderate"
    gemini_score = random.randint(50, 88)
    steps.append(f"Gemini: score={gemini_score} | IV={iv_context}, IVR={pick['ivr']}")
    
    # DeepSeek quant
    deepseek_score = random.randint(52, 85)
    steps.append(f"DeepSeek: score={deepseek_score} | quant pattern recognized")
    
    # Synthesis
    consensus = (claude_score + gemini_score + deepseek_score) / 3
    
    if consensus >= 65:
        verdict = "GO"
        confidence = int(consensus)
    else:
        verdict = "NO-GO"
        confidence = int(consensus)
    
    steps.append(f"OMNI consensus: {consensus:.1f} → {verdict}")
    return verdict, confidence, steps

def simulate_pnl(pick: dict, verdict: str) -> Optional[float]:
    """Simulate P&L for GO verdicts only."""
    if verdict != "GO":
        return None
    # Probabilistic outcome: 60% win rate, avg win 35%, avg loss 15%
    if random.random() < 0.60:
        pnl = pick["premium"] * 100 * random.uniform(0.25, 0.50)
    else:
        pnl = -pick["premium"] * 100 * random.uniform(0.10, 0.20)
    return round(pnl, 2)

def run_mock_session() -> SessionReport:
    now = datetime.now(ET)
    report = SessionReport(
        date=now.strftime("%Y-%m-%d"),
        started_at=now.isoformat(),
        finished_at="",
        duration_s=0
    )
    
    # Check Railway health
    health = check_railway_health()
    report.railway_healthy = "status" in health and health.get("status") == "healthy"
    report.railway_version = str(health.get("version", "?"))
    scanner = health.get("omni_scanner", {})
    report.scanner_active = scanner.get("loop_active", False)
    last = scanner.get("last_cycle_summary", {})
    report.last_real_verdict = last.get("verdict", "N/A") if last else "N/A"
    
    t0 = time.time()
    open_positions = 0
    
    for i, pick in enumerate(MOCK_PICKS, 1):
        t_start = time.time()
        trade = MockTradeResult(
            trade_id=i,
            ticker=pick["ticker"],
            direction=pick["direction"],
            strike=pick["strike"],
            expiry_dte=pick["expiry_dte"],
            ivr=pick["ivr"],
            premium=pick["premium"],
            agent=pick["agent"],
            axiom_passed=False,
            omni_verdict="PENDING",
            confidence=0,
        )
        
        # Pipeline
        trade.pipeline_steps.append(f"[{pick['agent']}] Screen → {pick['ticker']} {pick['direction']} ${pick['strike']}strike DTE={pick['expiry_dte']}")
        
        # Axiom gates
        passed, reason = axiom_risk_gates(pick, open_positions)
        trade.axiom_passed = passed
        trade.block_reason = reason
        
        if passed:
            trade.pipeline_steps.append("Axiom: PASS (all 11 layers)")
        else:
            trade.pipeline_steps.append(f"Axiom: BLOCK — {reason}")
        
        # OMNI synthesis
        verdict, confidence, synth_steps = omni_synthesis(pick, passed)
        trade.omni_verdict = verdict
        trade.confidence = confidence
        trade.pipeline_steps.extend(synth_steps)
        
        # Simulate outcome
        trade.simulated_pnl = simulate_pnl(pick, verdict)
        trade.elapsed_ms = (time.time() - t_start) * 1000
        
        if verdict == "GO":
            report.go_count += 1
            open_positions += 1
            if trade.simulated_pnl:
                report.total_simulated_pnl += trade.simulated_pnl
        elif verdict == "BLOCKED":
            report.blocked_count += 1
        else:
            report.no_go_count += 1
        
        report.trades.append(trade)
        time.sleep(0.1)  # brief pause between trades
    
    report.finished_at = datetime.now(ET).isoformat()
    report.duration_s = time.time() - t0
    report.total_simulated_pnl = round(report.total_simulated_pnl, 2)
    return report

# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg(chat_id: str, msg: str):
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"  📤 Telegram sent to {chat_id}")
    except Exception as e:
        print(f"  ❌ Telegram error: {e}")

def build_report(r: SessionReport) -> str:
    pnl_str = f"+${r.total_simulated_pnl:.2f}" if r.total_simulated_pnl >= 0 else f"-${abs(r.total_simulated_pnl):.2f}"
    railway_icon = "✅" if r.railway_healthy else "❌"
    scanner_icon = "🟢" if r.scanner_active else "🔴"
    
    header = (
        f"🧪 <b>MOCK TRADING SESSION — 6:30 PM ET</b>\n"
        f"📅 {r.date} | {datetime.fromisoformat(r.started_at).strftime('%I:%M %p ET')}\n\n"
        f"<b>RAILWAY STATUS:</b>\n"
        f"{railway_icon} nexus-alpha v{r.railway_version} — {'HEALTHY' if r.railway_healthy else 'DOWN'}\n"
        f"{scanner_icon} OMNI scanner: {'ACTIVE' if r.scanner_active else 'INACTIVE'}\n"
        f"📌 Last real verdict: {r.last_real_verdict}\n\n"
        f"<b>MOCK RESULTS (10 trades):</b>\n"
        f"✅ GO:       {r.go_count}/10\n"
        f"❌ NO-GO:    {r.no_go_count}/10\n"
        f"🛡️ BLOCKED:  {r.blocked_count}/10\n"
        f"💰 Sim P&L:  {pnl_str}\n\n"
    )
    
    lines = "<b>TRADE LOG:</b>\n"
    for t in r.trades:
        icon = "✅" if t.omni_verdict == "GO" else ("🛡️" if t.omni_verdict == "BLOCKED" else "❌")
        pnl_part = f" | P&L: ${t.simulated_pnl:+.2f}" if t.simulated_pnl is not None else ""
        block_part = f"\n    🚫 {t.block_reason}" if t.block_reason else ""
        lines += (
            f"{icon} T{t.trade_id}: {t.ticker} {t.direction} "
            f"${t.strike}strike DTE={t.expiry_dte} IVR={t.ivr} "
            f"[{t.agent}]{pnl_part}{block_part}\n"
            f"    → {t.omni_verdict}"
            f"{f' ({t.confidence}% confidence)' if t.omni_verdict in ('GO','NO-GO') else ''}\n"
        )
    
    footer = (
        f"\n⚠️ /mock_session endpoint NOT on Railway (v{r.railway_version}) — ran locally\n"
        f"⏱️ Duration: {r.duration_s:.1f}s\n"
        f"<i>🧪 MOCK — NEXUS_AUTO_EXECUTE=false | No real trades executed</i>"
    )
    
    return header + lines + footer

def build_health_report(r: SessionReport) -> str:
    pnl_str = f"+${r.total_simulated_pnl:.2f}" if r.total_simulated_pnl >= 0 else f"-${abs(r.total_simulated_pnl):.2f}"
    return (
        f"🧪 <b>MOCK SESSION COMPLETE — {r.date}</b>\n"
        f"GO: {r.go_count} | NO-GO: {r.no_go_count} | BLOCKED: {r.blocked_count}\n"
        f"Sim P&L: {pnl_str} | Duration: {r.duration_s:.1f}s\n"
        f"Railway v{r.railway_version}: {'✅ HEALTHY' if r.railway_healthy else '❌ DOWN'}\n"
        f"⚠️ /mock_session endpoint missing from Railway — local fallback used\n"
        f"<i>No real trades executed.</i>"
    )

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    random.seed(42)  # reproducible for same day runs
    print(f"\n{'='*60}")
    print(f"🧪 NEXUS MOCK TRADING ENGINE — {NOW.strftime('%Y-%m-%d %I:%M %p ET')}")
    print(f"{'='*60}")
    
    report = run_mock_session()
    
    # Print summary
    print(f"\nRESULTS: GO={report.go_count} | NO-GO={report.no_go_count} | BLOCKED={report.blocked_count}")
    print(f"Sim P&L: ${report.total_simulated_pnl:+.2f} | Duration: {report.duration_s:.1f}s")
    print(f"Railway: v{report.railway_version} {'HEALTHY' if report.railway_healthy else 'DOWN'}")
    
    for t in report.trades:
        icon = "✅" if t.omni_verdict == "GO" else ("🛡️" if t.omni_verdict == "BLOCKED" else "❌")
        print(f"  {icon} T{t.trade_id}: {t.ticker} {t.direction} → {t.omni_verdict}" + 
              (f" ({t.confidence}%)" if t.confidence else "") +
              (f" | P&L: ${t.simulated_pnl:+.2f}" if t.simulated_pnl else "") +
              (f" | 🚫 {t.block_reason}" if t.block_reason else ""))
    
    # Send Telegram reports
    print("\n📤 Sending Telegram reports...")
    full_report = build_report(report)
    health_report = build_health_report(report)
    
    _tg(AHMED_CHAT_ID, full_report)
    _tg(HEALTH_GROUP_ID, health_report)
    
    print("✅ Done.")
    
    # Save JSON report
    import os
    os.makedirs(os.path.join(os.path.dirname(__file__), "mock_reports"), exist_ok=True)
    report_path = os.path.join(
        os.path.dirname(__file__),
        f"mock_reports/mock_session_{report.date}.json"
    )
    with open(report_path, "w") as f:
        json.dump({
            "date": report.date,
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "duration_s": report.duration_s,
            "go_count": report.go_count,
            "no_go_count": report.no_go_count,
            "blocked_count": report.blocked_count,
            "railway_healthy": report.railway_healthy,
            "railway_version": report.railway_version,
            "scanner_active": report.scanner_active,
            "total_simulated_pnl": report.total_simulated_pnl,
            "trades": [
                {
                    "id": t.trade_id,
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "strike": t.strike,
                    "dte": t.expiry_dte,
                    "ivr": t.ivr,
                    "agent": t.agent,
                    "axiom_passed": t.axiom_passed,
                    "verdict": t.omni_verdict,
                    "confidence": t.confidence,
                    "block_reason": t.block_reason,
                    "simulated_pnl": t.simulated_pnl,
                    "elapsed_ms": t.elapsed_ms,
                    "pipeline_steps": t.pipeline_steps,
                }
                for t in report.trades
            ],
        }, f, indent=2)
    print(f"Report saved: {report_path}")
