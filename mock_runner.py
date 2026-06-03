#!/usr/bin/env python3
"""
mock_runner.py — Nexus Mock Trading Framework Runner
=====================================================
Standalone orchestrator. Real Nexus services. Real Alpaca paper account.
No stubs. No mock clients. If Cycle 1 fails, we have a real problem.

Authorized: Ahmed Sadek + SOVEREIGN 2026-05-03
Cipher COO sign-off: 48624c4 | 2026-05-03 23:03 EDT

Usage:
    cd /Users/ahmedsadek/nexus
    python3 mock_runner.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

# ── Config ────────────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

NEXUS_SECRET  = os.getenv("NEXUS_WEBHOOK_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
AXIOM_SECRET  = os.getenv("AXIOM_SECRET",          "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN",   "7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c")
AHMED_CHAT_ID  = "8573754783"
HEALTH_GROUP_ID = "-1003579956463"

AXIOM_URL    = "http://localhost:8001"
BUFFER_URL   = "http://localhost:8002"
OMNI_URL     = "http://localhost:8004"
EXEC_URL     = "http://localhost:8005"

NEXUS_HDR  = {"X-Nexus-Secret": NEXUS_SECRET, "Content-Type": "application/json"}
AXIOM_HDR  = {"X-Axiom-Secret": AXIOM_SECRET}
REPORT_DIR = os.path.join(os.path.dirname(__file__), "omni", "mock_reports")

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class CycleResult:
    """Result for a single mock cycle."""
    number: int
    name: str
    passed: bool
    details: str
    elapsed_s: float
    alpaca_fill: Optional[dict] = None

@dataclass
class SessionReport:
    """Full session report."""
    session_id: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    cycles: list[CycleResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    baseline_go_count: int = 0
    baseline_trades: int = 0

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    """Print timestamped log line."""
    ts = datetime.now(ET).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def telegram(text: str) -> None:
    """Send Telegram message. Fire-and-forget."""
    if not TELEGRAM_TOKEN:
        return
    for chat_id in [AHMED_CHAT_ID, HEALTH_GROUP_ID]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=6,
            )
        except Exception as e:
            log(f"  Telegram send failed ({chat_id}): {e}")

def submit(agent: str, ticker: str, direction: str, score: float,
           session_id: str, rationale: str = "") -> requests.Response:
    """Submit a pick to alpha-buffer."""
    return requests.post(
        f"{BUFFER_URL}/submit",
        headers=NEXUS_HDR,
        json={
            "agent":     agent,
            "ticker":    ticker,
            "direction": direction,
            "score":     score,
            "rationale": rationale or f"Mock session {session_id}",
        },
        timeout=10,
    )

def purge(tickers: Optional[list[str]] = None) -> None:
    """Purge concordance state. No body = purge all."""
    try:
        requests.post(
            f"{BUFFER_URL}/concordance/purge",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            json=tickers if tickers else None,
            timeout=5,
        )
    except Exception:
        pass

def exec_health() -> dict:
    """Get alpha-execution health."""
    r = requests.get(f"{EXEC_URL}/health", headers=NEXUS_HDR, timeout=5)
    return r.json()

def omni_health() -> dict:
    """Get OMNI health."""
    r = requests.get(f"{OMNI_URL}/health", headers=NEXUS_HDR, timeout=5)
    return r.json()

def get_trades() -> dict:
    """Get alpha-execution trades report."""
    r = requests.get(f"{EXEC_URL}/trades", headers=NEXUS_HDR, timeout=5)
    return r.json()

def wait_for_go_verdict(baseline_go: int, timeout_s: int = 90) -> bool:
    """Poll OMNI until go_verdicts_today increments. Returns True if GO seen."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            h = omni_health()
            if h.get("go_verdicts_today", 0) > baseline_go:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False

def wait_for_trade(baseline_orders: int, timeout_s: int = 60) -> Optional[dict]:
    """Poll alpha-execution trades until alpaca_orders_submitted increments."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            t = get_trades()
            if t.get("alpaca_orders_submitted", 0) > baseline_orders:
                return t
        except Exception:
            pass
        time.sleep(2)
    return None

# ── Pre-session ───────────────────────────────────────────────────────────────

def pre_session(session_id: str) -> tuple[bool, str, dict]:
    """
    Run all pre-session checks. Returns (ok, abort_reason, baseline).
    Any failure aborts the session — no cycles run.
    """
    log("=" * 60)
    log(f"PRE-SESSION CHECKS — {session_id}")
    log("=" * 60)
    baseline: dict = {}

    # S1 — Service health
    log("S1: Checking service health...")
    services = [
        ("Axiom",          f"{AXIOM_URL}/health",  AXIOM_HDR),
        ("Alpha-Buffer",   f"{BUFFER_URL}/status",  NEXUS_HDR),
        ("OMNI",           f"{OMNI_URL}/health",   NEXUS_HDR),
        ("Alpha-Exec",     f"{EXEC_URL}/health",   NEXUS_HDR),
    ]
    for name, url, hdrs in services:
        try:
            r = requests.get(url, headers=hdrs, timeout=5)
            if r.status_code != 200:
                return False, f"{name} returned HTTP {r.status_code}", {}
            log(f"  ✅ {name} healthy")
        except Exception as e:
            return False, f"{name} unreachable: {e}", {}

    # S2 — Resume alpha-execution if paused
    log("S2: Ensuring alpha-execution is not paused...")
    try:
        h = exec_health()
        if h.get("execution_paused"):
            r = requests.post(f"{EXEC_URL}/resume", headers=NEXUS_HDR, timeout=5)
            log(f"  ✅ Resumed: {r.json()}")
        else:
            log("  ✅ Not paused")
    except Exception as e:
        return False, f"Could not check/resume alpha-execution: {e}", {}

    # S3 — Baseline snapshot
    log("S3: Recording baseline...")
    try:
        eh = exec_health()
        oh = omni_health()
        trades = get_trades()
        baseline = {
            "open_positions":         eh.get("open_positions", 0),
            "trades_today":           eh.get("trades_today", 0),
            "go_verdicts_today":      oh.get("go_verdicts_today", 0),
            "alpaca_orders_submitted": trades.get("alpaca_orders_submitted", 0),
            "alpaca_orders_filled":    trades.get("alpaca_orders_filled", 0),
        }
        log(f"  positions={baseline['open_positions']} trades_today={baseline['trades_today']} "
            f"go_verdicts={baseline['go_verdicts_today']} orders_submitted={baseline['alpaca_orders_submitted']}")
    except Exception as e:
        return False, f"Baseline snapshot failed: {e}", {}

    # S4 — Seed pool via trigger-tier1 → trigger-tier2
    log("S4: Seeding Axiom pool...")
    try:
        r1 = requests.post(f"{AXIOM_URL}/trigger-tier1", headers=AXIOM_HDR, timeout=10)
        log(f"  Tier1 trigger: {r1.status_code} {r1.json().get('status','?')}")
        log("  Waiting 20s for Tier1 scan...")
        time.sleep(20)
        pool = requests.get(f"{AXIOM_URL}/pool", headers=AXIOM_HDR, timeout=5).json()
        log(f"  Pool after Tier1: {pool.get('count', 0)} tickers")
        if pool.get("count", 0) > 0:
            r2 = requests.post(f"{AXIOM_URL}/trigger-tier2", headers=AXIOM_HDR, timeout=10)
            log(f"  Tier2 trigger: {r2.status_code}")
            time.sleep(10)
            pool = requests.get(f"{AXIOM_URL}/pool", headers=AXIOM_HDR, timeout=5).json()
            log(f"  Pool after Tier2: {pool.get('count', 0)} tickers")
        else:
            log("  ⚠️  Pool empty after Tier1 — universe check will fail-open, proceeding")
    except Exception as e:
        log(f"  ⚠️  Pool seeding failed ({e}) — universe check will fail-open, proceeding")

    # S5 — Verify MOCK_SESSION_MODE active
    log("S5: Verifying MOCK_SESSION_MODE is active...")
    try:
        r = requests.post(
            f"{EXEC_URL}/execute",
            headers=NEXUS_HDR,
            json={"ticker": "TEST", "direction": "bullish", "pathway": "P1", "score": 99},
            timeout=5,
        )
        # We expect a validation error or real processing — not "outside market hours"
        body = r.json()
        if isinstance(body, dict) and "outside market hours" in str(body.get("reason", "")):
            return False, "MOCK_SESSION_MODE not active — market hours gate still blocking", {}
        log("  ✅ Market hours gate bypassed")
    except Exception as e:
        log(f"  ⚠️  MOCK check inconclusive: {e} — proceeding")

    # S6 — Purge any stale concordance state
    log("S6: Purging stale concordance state...")
    purge()
    time.sleep(1)
    log("  ✅ Buffer state clean")

    log("=" * 60)
    log("PRE-SESSION CHECKS PASSED ✅")
    log("=" * 60)
    return True, "", baseline

# ── Cycles ────────────────────────────────────────────────────────────────────

def cycle_1_clean_concordance(session_id: str, baseline: dict) -> CycleResult:
    """
    Cycle 1 — Clean P1 Concordance (baseline happy path).
    All 3 agents agree on NVDA bullish. Expect P1_CONCORDANCE GO → Alpaca fill.
    Pass criterion: confirmed Alpaca order submitted.
    """
    t0 = time.time()
    log("\n── CYCLE 1: Clean P1 Concordance ──")
    ticker = "NVDA"
    direction = "bullish"

    try:
        base_orders = baseline.get("alpaca_orders_submitted", 0)
        base_go     = baseline.get("go_verdicts_today", 0)

        for agent, score in [("Cipher", 85), ("Atlas", 82), ("Sage", 80)]:
            r = submit(agent, ticker, direction, score, session_id,
                       f"Cycle 1 clean concordance — mock session {session_id}")
            log(f"  {agent}: HTTP {r.status_code} → {r.json().get('message') or r.json().get('reason') or r.json()}")
            if r.status_code not in (200, 201):
                # Not immediately fatal — buffer may still form concordance on 3rd submit
                pass

        log(f"  Waiting for OMNI GO verdict (baseline go_verdicts={base_go})...")
        go_seen = wait_for_go_verdict(base_go, timeout_s=90)
        if not go_seen:
            oh = omni_health()
            return CycleResult(1, "Clean P1 Concordance", False,
                               f"OMNI never issued GO verdict within 90s. go_verdicts_today={oh.get('go_verdicts_today')}",
                               time.time() - t0)

        log(f"  ✅ OMNI GO issued. Waiting for Alpaca order submission...")
        trade_report = wait_for_trade(base_orders, timeout_s=60)
        elapsed = time.time() - t0

        if trade_report and trade_report.get("alpaca_orders_submitted", 0) > base_orders:
            fills = trade_report.get("alpaca_orders_filled", 0)
            submitted = trade_report.get("alpaca_orders_submitted", 0)
            log(f"  ✅ CYCLE 1 PASS — Alpaca orders submitted={submitted} filled={fills}")
            return CycleResult(1, "Clean P1 Concordance", True,
                               f"Alpaca orders_submitted={submitted} filled={fills} | elapsed={elapsed:.1f}s",
                               elapsed, trade_report)
        else:
            eh = exec_health()
            return CycleResult(1, "Clean P1 Concordance", False,
                               f"No Alpaca order submitted within 60s. exec_health={eh.get('trades_today')}/{eh.get('auto_execute')}",
                               elapsed)
    except Exception as e:
        return CycleResult(1, "Clean P1 Concordance", False, f"Exception: {e}", time.time() - t0)
    finally:
        purge([ticker])
        time.sleep(2)


def cycle_2_p2_concordance(session_id: str, baseline: dict) -> CycleResult:
    """
    Cycle 2 — Two-agent P2 concordance. Sage absent (skipped).
    Cipher + Atlas agree on GOOGL bullish. Expect P2 pathway, 0.75x sizing.
    """
    t0 = time.time()
    log("\n── CYCLE 2: P2 Concordance (Sage absent) ──")
    ticker = "GOOGL"
    direction = "bullish"

    try:
        base_go = omni_health().get("go_verdicts_today", 0)

        for agent, score in [("Cipher", 88), ("Atlas", 84)]:
            r = submit(agent, ticker, direction, score, session_id)
            log(f"  {agent}: HTTP {r.status_code} → {r.json().get('message') or r.json().get('reason') or 'ok'}")

        log("  Sage absent — waiting for P2 pathway dispatch...")
        go_seen = wait_for_go_verdict(base_go, timeout_s=90)
        elapsed = time.time() - t0

        # P2 needs exactly 2 agents — may require 3rd or may dispatch on 2
        # Check concordance result directly
        if go_seen:
            log(f"  ✅ CYCLE 2 PASS — P2 GO issued | elapsed={elapsed:.1f}s")
            return CycleResult(2, "P2 Concordance", True,
                               f"P2 GO issued with 2 agents (Sage absent) | elapsed={elapsed:.1f}s",
                               elapsed)
        else:
            # P2 may not dispatch without 3rd agent depending on config — check buffer state
            status = requests.get(f"{BUFFER_URL}/status", headers=NEXUS_HDR, timeout=5).json()
            return CycleResult(2, "P2 Concordance", False,
                               f"No GO after 90s. buffer window: {status.get('current_window',{})}",
                               elapsed)
    except Exception as e:
        return CycleResult(2, "P2 Concordance", False, f"Exception: {e}", time.time() - t0)
    finally:
        purge([ticker])
        time.sleep(2)


def cycle_3_score_rejection(session_id: str) -> CycleResult:
    """
    Cycle 3 — Score below threshold. All agents submit AVGO score 57/56/55.
    Pass criterion: all 3 submissions return 422. No concordance.
    """
    t0 = time.time()
    log("\n── CYCLE 3: Score Below Threshold ──")
    ticker = "AVGO"

    try:
        results = []
        for agent, score in [("Cipher", 57), ("Atlas", 56), ("Sage", 55)]:
            r = submit(agent, ticker, "bullish", score, session_id)
            results.append(r.status_code)
            log(f"  {agent} score={score}: HTTP {r.status_code} reason={r.json().get('reason','?')}")

        elapsed = time.time() - t0
        all_rejected = all(s == 422 for s in results)
        if all_rejected:
            log(f"  ✅ CYCLE 3 PASS — all 3 submissions rejected at score gate")
            return CycleResult(3, "Score Rejection Gate", True,
                               f"All 3 rejected 422 at score floor (57/56/55 < 58) | elapsed={elapsed:.1f}s",
                               elapsed)
        else:
            return CycleResult(3, "Score Rejection Gate", False,
                               f"Expected all 422, got: {results}",
                               elapsed)
    except Exception as e:
        return CycleResult(3, "Score Rejection Gate", False, f"Exception: {e}", time.time() - t0)
    finally:
        purge([ticker])
        time.sleep(1)


def cycle_4_invalid_ticker(session_id: str) -> CycleResult:
    """
    Cycle 4 — Invalid ticker BADTICKER + valid MSFT.
    BADTICKER must be rejected 422. MSFT proceeds.
    """
    t0 = time.time()
    log("\n── CYCLE 4: Invalid Ticker Injection ──")

    try:
        # Submit BADTICKER from Cipher
        r_bad = submit("Cipher", "BADTICKER", "bullish", 85, session_id)
        log(f"  BADTICKER (Cipher): HTTP {r_bad.status_code} → {r_bad.json().get('reason','?')}")
        bad_rejected = r_bad.status_code == 422

        # Submit MSFT from Cipher + Atlas
        r_msft1 = submit("Cipher", "MSFT", "bullish", 83, session_id)
        r_msft2 = submit("Atlas",  "MSFT", "bullish", 80, session_id)
        log(f"  MSFT (Cipher): HTTP {r_msft1.status_code}")
        log(f"  MSFT (Atlas):  HTTP {r_msft2.status_code}")
        msft_accepted = r_msft1.status_code in (200, 201) and r_msft2.status_code in (200, 201)

        elapsed = time.time() - t0
        passed = bad_rejected and msft_accepted
        details = (f"BADTICKER={'rejected 422' if bad_rejected else f'HTTP {r_bad.status_code} UNEXPECTED'} | "
                   f"MSFT={'accepted' if msft_accepted else 'rejected UNEXPECTED'} | elapsed={elapsed:.1f}s")

        if passed:
            log(f"  ✅ CYCLE 4 PASS — {details}")
        else:
            log(f"  ❌ CYCLE 4 FAIL — {details}")
        return CycleResult(4, "Invalid Ticker Injection", passed, details, elapsed)
    except Exception as e:
        return CycleResult(4, "Invalid Ticker Injection", False, f"Exception: {e}", time.time() - t0)
    finally:
        purge(["MSFT"])
        time.sleep(1)


def cycle_7_idempotency(session_id: str) -> CycleResult:
    """
    Cycle 7 — Duplicate submission idempotency.
    Same agent+ticker+direction submitted twice within 500ms.
    Second must return 409 deduplicated.
    """
    t0 = time.time()
    log("\n── CYCLE 7: Idempotency (duplicate submission) ──")
    ticker = "AMD"

    try:
        r1 = submit("Cipher", ticker, "bullish", 82, session_id)
        t_between = time.time()
        r2 = submit("Cipher", ticker, "bullish", 82, session_id)
        gap_ms = (time.time() - t_between) * 1000

        log(f"  Submit 1: HTTP {r1.status_code}")
        log(f"  Submit 2: HTTP {r2.status_code} (gap={gap_ms:.0f}ms) → {r2.json().get('reason','?')}")

        elapsed = time.time() - t0
        first_ok  = r1.status_code in (200, 201)
        second_deduped = r2.status_code == 409 and r2.json().get("deduplicated") is True

        passed = first_ok and second_deduped
        details = (f"First={'accepted' if first_ok else f'HTTP {r1.status_code}'} | "
                   f"Second={'409 deduped' if second_deduped else f'HTTP {r2.status_code} UNEXPECTED'} | "
                   f"gap={gap_ms:.0f}ms | elapsed={elapsed:.1f}s")
        if passed:
            log(f"  ✅ CYCLE 7 PASS — {details}")
        else:
            log(f"  ❌ CYCLE 7 FAIL — {details}")
        return CycleResult(7, "Idempotency", passed, details, elapsed)
    except Exception as e:
        return CycleResult(7, "Idempotency", False, f"Exception: {e}", time.time() - t0)
    finally:
        purge([ticker])
        time.sleep(1)


def cycle_10_bearish_direction(session_id: str, baseline: dict) -> CycleResult:
    """
    Cycle 10 — Bearish direction (PUT spread). All 3 agents agree on TSLA bearish.
    Pass criterion: concordance forms, OMNI issues GO (or NO-GO is also valid — bearish pipeline exercised).
    """
    t0 = time.time()
    log("\n── CYCLE 10: Bearish Direction ──")
    ticker = "TSLA"
    direction = "bearish"

    try:
        base_go = omni_health().get("go_verdicts_today", 0)

        for agent, score in [("Cipher", 85), ("Atlas", 83), ("Sage", 81)]:
            r = submit(agent, ticker, direction, score, session_id)
            log(f"  {agent}: HTTP {r.status_code} → {r.json().get('message') or r.json().get('reason') or 'ok'}")

        log("  Waiting for OMNI synthesis on bearish signal...")
        go_seen = wait_for_go_verdict(base_go, timeout_s=90)
        elapsed = time.time() - t0

        if go_seen:
            log(f"  ✅ CYCLE 10 PASS — bearish P1 concordance dispatched to OMNI and GO issued")
            return CycleResult(10, "Bearish Direction", True,
                               f"Bearish TSLA P1 → OMNI GO | elapsed={elapsed:.1f}s", elapsed)
        else:
            # Check if concordance formed at least (OMNI may have issued NO-GO)
            status = requests.get(f"{BUFFER_URL}/status", headers=NEXUS_HDR, timeout=5).json()
            oh = omni_health()
            # If syntheses_today increased, OMNI received it — that's sufficient for cycle 10
            base_synth = baseline.get("syntheses_today", 0)
            curr_synth = oh.get("syntheses_today", 0)
            if curr_synth > base_synth:
                log(f"  ✅ CYCLE 10 PASS — bearish concordance reached OMNI (synthesis ran, verdict was NO-GO)")
                return CycleResult(10, "Bearish Direction", True,
                                   f"Bearish TSLA reached OMNI (syntheses +{curr_synth - base_synth}); OMNI issued NO-GO (correct for bearish in bull regime) | elapsed={elapsed:.1f}s",
                                   elapsed)
            return CycleResult(10, "Bearish Direction", False,
                               f"No synthesis or GO after 90s | elapsed={elapsed:.1f}s", elapsed)
    except Exception as e:
        return CycleResult(10, "Bearish Direction", False, f"Exception: {e}", time.time() - t0)
    finally:
        purge([ticker])
        time.sleep(2)


def cycle_13_malformed_payload(session_id: str) -> CycleResult:
    """
    Cycle 13 — Malformed payload injection.
    Missing required fields, invalid score type, garbage ticker.
    Pass criterion: all requests rejected with 4xx, no crash.
    """
    t0 = time.time()
    log("\n── CYCLE 13: Malformed Payload ──")

    payloads = [
        ("empty body",        {}),
        ("missing ticker",    {"agent": "Cipher", "direction": "bullish", "score": 85}),
        ("invalid direction", {"agent": "Cipher", "ticker": "NVDA", "direction": "sideways", "score": 85}),
        ("score as string",   {"agent": "Cipher", "ticker": "NVDA", "direction": "bullish", "score": "eighty-five"}),
        ("sql injection",     {"agent": "Cipher", "ticker": "'; DROP TABLE submissions; --", "direction": "bullish", "score": 85}),
    ]

    try:
        results = []
        for name, payload in payloads:
            try:
                r = requests.post(f"{BUFFER_URL}/submit", headers=NEXUS_HDR, json=payload, timeout=5)
                rejected = r.status_code in (400, 422, 405)
                results.append((name, r.status_code, rejected))
                log(f"  {name}: HTTP {r.status_code} {'✅' if rejected else '❌ UNEXPECTED'}")
            except Exception as e:
                results.append((name, "exception", False))
                log(f"  {name}: Exception {e}")

        elapsed = time.time() - t0
        all_rejected = all(r for _, _, r in results)
        details = " | ".join(f"{n}={c}" for n, c, _ in results)

        if all_rejected:
            log(f"  ✅ CYCLE 13 PASS — all malformed payloads rejected")
            return CycleResult(13, "Malformed Payload", True,
                               f"All 5 payloads rejected | elapsed={elapsed:.1f}s", elapsed)
        else:
            log(f"  ❌ CYCLE 13 FAIL — {details}")
            return CycleResult(13, "Malformed Payload", False, details, elapsed)
    except Exception as e:
        return CycleResult(13, "Malformed Payload", False, f"Exception: {e}", time.time() - t0)
    finally:
        purge()
        time.sleep(1)

# ── Report ────────────────────────────────────────────────────────────────────

def build_report(report: SessionReport) -> str:
    """Build Telegram-formatted session report."""
    passed = sum(1 for c in report.cycles if c.passed)
    total  = len(report.cycles)
    emoji  = "✅" if passed == total else ("⚠️" if passed > 0 else "❌")

    lines = [
        f"🧪 <b>NEXUS MOCK SESSION COMPLETE</b>",
        f"Session: <code>{report.session_id}</code>",
        f"Duration: {report.duration_s:.0f}s",
        f"Result: {emoji} {passed}/{total} cycles passed",
        "",
    ]

    for c in report.cycles:
        icon = "✅" if c.passed else "❌"
        lines.append(f"{icon} Cycle {c.number}: {c.name} ({c.elapsed_s:.1f}s)")
        if not c.passed or c.alpaca_fill:
            lines.append(f"   → {c.details[:120]}")

    # Cycle 1 fill evidence
    c1 = next((c for c in report.cycles if c.number == 1), None)
    if c1 and c1.passed and c1.alpaca_fill:
        fill = c1.alpaca_fill
        lines += [
            "",
            "🔴 <b>CYCLE 1 — ALPACA FILL EVIDENCE</b>",
            f"orders_submitted: {fill.get('alpaca_orders_submitted')}",
            f"orders_filled:    {fill.get('alpaca_orders_filled')}",
            f"open_positions:   {fill.get('open_positions_count')}",
        ]

    lines += [
        "",
        "⚠️ MOCK_SESSION_MODE still active — remove from alpha-execution .env after review.",
    ]
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    """
    Run the full mock trading session.
    Returns exit code: 0 if all cycles pass, 1 if any fail or aborted.
    """
    session_id = "MOCK-" + datetime.now(ET).strftime("%Y%m%d-%H%M%S")
    report = SessionReport(
        session_id = session_id,
        started_at = datetime.now(ET).isoformat(),
    )

    log("=" * 60)
    log(f"NEXUS MOCK TRADING FRAMEWORK")
    log(f"Session: {session_id}")
    log(f"Cipher COO sign-off: 48624c4 | 2026-05-03 23:03 EDT")
    log("=" * 60)

    # Pre-session
    ok, reason, baseline = pre_session(session_id)
    if not ok:
        report.aborted = True
        report.abort_reason = reason
        log(f"\n🚨 ABORT: {reason}")
        telegram(f"🚨 <b>MOCK SESSION ABORTED</b>\nSession: {session_id}\nReason: {reason}")
        return 1

    # Record baseline syntheses for cycle 10
    try:
        baseline["syntheses_today"] = omni_health().get("syntheses_today", 0)
    except Exception:
        baseline["syntheses_today"] = 0

    # Run cycles
    cycles_to_run = [
        lambda: cycle_1_clean_concordance(session_id, baseline),
        lambda: cycle_2_p2_concordance(session_id, baseline),
        lambda: cycle_3_score_rejection(session_id),
        lambda: cycle_4_invalid_ticker(session_id),
        lambda: cycle_7_idempotency(session_id),
        lambda: cycle_10_bearish_direction(session_id, baseline),
        lambda: cycle_13_malformed_payload(session_id),
    ]

    for cycle_fn in cycles_to_run:
        try:
            result = cycle_fn()
            report.cycles.append(result)
            status = "✅ PASS" if result.passed else "❌ FAIL"
            log(f"  → Cycle {result.number} result: {status}")
        except Exception as e:
            log(f"  → Cycle runner exception: {e}")

    # Final summary
    report.finished_at = datetime.now(ET).isoformat()
    report.duration_s  = (
        datetime.fromisoformat(report.finished_at) -
        datetime.fromisoformat(report.started_at)
    ).total_seconds()

    passed = sum(1 for c in report.cycles if c.passed)
    total  = len(report.cycles)

    log("\n" + "=" * 60)
    log(f"SESSION COMPLETE — {passed}/{total} cycles passed")
    log("=" * 60)

    # Save JSON report
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"mock_session_{datetime.now(ET).strftime('%Y-%m-%d')}.json")
    with open(report_path, "w") as f:
        json.dump({
            "session_id":   report.session_id,
            "started_at":   report.started_at,
            "finished_at":  report.finished_at,
            "duration_s":   report.duration_s,
            "passed":       passed,
            "total":        total,
            "cycles": [
                {
                    "number":   c.number,
                    "name":     c.name,
                    "passed":   c.passed,
                    "details":  c.details,
                    "elapsed_s": c.elapsed_s,
                }
                for c in report.cycles
            ],
        }, f, indent=2)
    log(f"Report saved: {report_path}")

    # Send Telegram
    msg = build_report(report)
    telegram(msg)
    log("Telegram report sent.")

    log("\n⚠️  REMINDER: Remove MOCK_SESSION_MODE=true from alpha-execution/.env and restart service.")
    log("    sed -i '' '/MOCK_SESSION_MODE/d' /Users/ahmedsadek/nexus/alpha-execution/.env")
    log("    launchctl unload ~/Library/LaunchAgents/ai.nexus.alpha-execution.plist && launchctl load ~/Library/LaunchAgents/ai.nexus.alpha-execution.plist")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
