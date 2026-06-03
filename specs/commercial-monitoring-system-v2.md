# SPEC: Commercial-Grade Monitoring System — v2
**Version:** 2.1  
**Author:** GENESIS 🌱  
**Date:** 2026-04-29  
**Status:** APPROVED — Ahmed Sadek (2026-04-29, 18:53 ET)  
**Design Basis:** Axiom COMMERCIAL_MONITORING_SYSTEM v1 + Vector Rebuttal (8 amendments) + Vector Final Rebuttal (amendments V9–V14, all accepted)

### Vector Final Rebuttal Amendments (V9–V14)
| # | Amendment | Resolution |
|---|---|---|
| V9 | TRS encodes severity — P0–P4 determined by sub-component weights, no manual classification | TRS score → P0 (score=0/dead), P1–P4 by sub-component weight map |
| V10 | Schema Version Gate — hard blocker at startup | Service refuses to start if CHRONICLE schema != REQUIRED_CHRONICLE_SCHEMA |
| V11 | Explicit TRS fail-closed contract — any exception returns TRSResult(score=0, block=True) | Implemented in get_trading_readiness_score() with no default-allow path |
| V12 | Options probe replaces equity smoke test entirely — 6-step OCC roundtrip | SPY ATM 30-DTE, dry_run=False on paper, PROBE_ prefix, cancel+verify |
| V13 | Canary guaranteed cleanup — CANARY_ prefix, 90s hard cancel, TRS=0 on leak | Enforced at order construction, canary runner owns cleanup path |
| V14 | CHRONICLE IS the Health Bus — no 4th database | health_events table in CHRONICLE, source-enforced writer authority |  
**Service:** `nexus-integrity` — new standalone service  
**Port:** 8011  
**LaunchD label:** `ai.nexus.nexus-integrity`  
**DB:** `/Users/ahmedsadek/nexus/data/nexus_integrity.db`

---

## Purpose

A commercial-grade monitoring system that asks **"did the right data flow through the right path in the last 15 minutes, and if not, why not, and is it already fixed?"** — not "are services running?".

Closes the gap that caused 2+ hour pipeline silence while all health checks returned green.

---

## Design Basis: Vector's 8 Amendments (All Accepted)

The Axiom v1 design was solid but had 8 material gaps identified by Vector. This spec incorporates all 8:

| # | Vector Amendment | Resolution |
|---|---|---|
| V1 | Fail-closed TRS (Transaction Result Store) | TRS uses WAL SQLite, fail-closed on crash — probe blocked until TRS responds |
| V2 | Options-specific probe routing | Execution probe uses `/execute` with `asset_class=options`, `strategy=short_put` — not generic HTTP |
| V3 | Probe isolation | Dedicated `PROBE_SECRET` env var + `X-Probe-Mode: true` header + mutex prevents concurrent probes |
| V4 | CHRONICLE as unified state store | All health state written to CHRONICLE `monitoring_state` table, not local-only SQLite |
| V5 | Canary repositioned | Canary = confirmation layer (validates result of real pipeline), not synthetic substitute for it |
| V6 | Recovery assertions | After every auto-fix, a meta-test verifies the fix infrastructure itself works |
| V7 | Regime-aware thresholds | All thresholds stored in CHRONICLE `monitoring_thresholds` — Ahmed can adjust without redeploy |
| V8 | Schema version gate | Hard blocker: if CHRONICLE schema version != expected, service refuses to start |

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                   NEXUS INTEGRITY SERVICE (port 8011)            │
│                                                                  │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────┐  │
│  │  Pipeline Flow  │  │  Composite Health │  │  SPC Engine    │  │
│  │  Verifier       │  │  Aggregator       │  │  (Statistical) │  │
│  │  (Layer 2)      │  │  (Layer 1+2+3)    │  │  (Layer 3)     │  │
│  └────────┬────────┘  └────────┬──────────┘  └───────┬────────┘  │
│           │                    │                     │           │
│           └────────────────────┼─────────────────────┘           │
│                                │                                 │
│                    ┌───────────▼──────────┐                      │
│                    │   TRS (Transaction   │                      │
│                    │   Result Store)      │                      │
│                    │   fail-closed WAL    │                      │
│                    └───────────┬──────────┘                      │
│                                │                                 │
│                    ┌───────────▼──────────┐                      │
│                    │  Self-Healing        │                      │
│                    │  Orchestrator        │                      │
│                    │  + Recovery Asserter │                      │
│                    └───────────┬──────────┘                      │
└────────────────────────────────┼─────────────────────────────────┘
                                 │
              ┌──────────────────┼─────────────────┐
              │                  │                 │
   ┌──────────▼──────┐  ┌────────▼──────┐  ┌──────▼──────┐
   │ CHRONICLE        │  │ Alert Bus     │  │ OMNI Sizing │
   │ (state + thresh) │  │ (Telegram)    │  │ (multiplier)│
   └──────────────────┘  └───────────────┘  └─────────────┘
```

---

## Layer 1: Presence (kept, enhanced)

**What it does:** Standard /health checks on all 9 services. Runs every 60 seconds.  
**Enhancement:** All results written to CHRONICLE `monitoring_state` — not just logged locally.

Services monitored:
- Axiom (8001), Alpha-Buffer (8002), Prime-Buffer (8003), OMNI (8004)
- Alpha-Execution (8005), Prime-Execution (8006), ORACLE (8007), AILS (8008)
- Pipeline Sentinel (8010)

Output: `presence_score` (0–100) = (healthy services / total services) × 100

---

## Layer 2: Pipeline Flow Verifier (V2 Amendment: fail-closed TRS + options probe)

### 2.1 Five-Stage Verification (every 15 minutes, market hours only)

```
STAGE 1 — POOL DELIVERY (Axiom gate)
  Check: axiom.db → pool_snapshots.created_at < 16 min ago
  Fix:   POST /refresh to Axiom → wait 30s → re-check
  
STAGE 2 — AGENT SCAN ACTIVITY
  Check: cipher.db + atlas.db + sage.db → decisions.created_at < 20 min ago
  Note:  0 submissions ≠ broken. 0 decisions = broken.
  Fix:   Push pool manually via /receive-pool → wait 60s → re-check decisions count

STAGE 3 — BUFFER ACCEPTANCE
  Check: GET alpha-buffer/status → circuit_breaker = NORMAL
  Fix:   POST /reset-circuit-breaker → re-check

STAGE 4 — SYNTHESIS READINESS
  Check: OMNI /health → pool_workers > 0, syntheses_today incrementing
  Fix:   Restart OMNI via launchctl → wait 30s → re-check worker count

STAGE 5 — EXECUTION READINESS (V2 Amendment: options-specific probe)
  Check: POST alpha-execution/execute with:
    { "ticker": "SPY", "asset_class": "options", "strategy": "short_put",
      "dry_run": true, "X-Probe-Mode": true }
  Auth: PROBE_SECRET header (separate from NEXUS_SECRET — V3 amendment)
  Mutex: Only one probe in flight at a time
  Fix:   Diagnose specific sub-failure → targeted fix, not service restart
```

### 2.2 TRS — Transaction Result Store (V1 Amendment: fail-closed)

All probe results written to TRS before advancing to next stage.

```
TRS behaviour:
- WAL mode SQLite at /nexus/data/trs.db
- If TRS write fails: probe is BLOCKED (fail-closed) — no result = failure assumed
- If TRS read fails: composite score treats component as UNKNOWN (not GREEN)
- TRS crash → service restarts TRS, blocks new probes until TRS ACKs startup
```

### 2.3 Auto-Remediation Protocol

```
Detect → Diagnose → Fix → Recovery Assert → (alert only if assert fails)
                               ↑
                     (V6 Amendment: meta-test)
                     After fix: assert the fix mechanism itself works
                     e.g. after restart: verify process pid changed
                     After pool push: verify decisions count > pre-fix baseline
```

Timeline:
- Fix attempt: within 2 minutes of stage failure
- Recovery assert: within 3 minutes of fix
- Escalation to SOVEREIGN: if assert fails after 3 minutes
- Ahmed alert: only if SOVEREIGN cannot resolve (never direct)

---

## Layer 3: Statistical Process Control (SPC Engine)

### 3.1 Metrics Tracked

| Metric | Window | Alert Threshold |
|---|---|---|
| GO verdict rate | Rolling 20-day | >3σ deviation (stored in CHRONICLE — V7 amendment) |
| Submission rate | Per market session | >5σ or zero for >45 min |
| Pick score distribution | Rolling 10-day | K-S test p-value < 0.05 |
| Pipeline latency P99 | Per 30-min window | >3× P50 |
| Brain error rate | Per session | >40% (threshold from CHRONICLE) |
| Canary SLA breach rate | Last 5 canaries | >2/5 breaches |

### 3.2 Regime-Aware Thresholds (V7 Amendment)

All SPC thresholds stored in CHRONICLE table `monitoring_thresholds`:

```sql
CREATE TABLE monitoring_thresholds (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    regime TEXT DEFAULT 'ALL',   -- ALL | NORMAL | ELEVATED | HIGH | EXTREME
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
```

Ahmed can adjust thresholds via CHRONICLE without any code redeploy. Service reads thresholds at startup + refreshes every 5 minutes.

### 3.3 EOD Governance Report (4:15 PM ET, weekdays)

Answers 5 questions:
1. Pipeline uptime: % of 15-min windows confirmed LIVE (target ≥ 95%)
2. Signal quality: concordances formed + score distribution
3. Trade execution: GO verdicts → orders → fills (target 100% chain completion)
4. Silent failures: any failure >15 min without auto-fix (target 0)
5. Monitoring blind spots: any failure evaded all detection layers?

---

## Composite Health Score (V2 design, weighted)

```
GREEN:   90-100  → full capacity (size multiplier 1.0)
AMBER:   60-89   → cap size 50% (size multiplier 0.5)
RED:     30-59   → halt new entries (size multiplier 0.0)
BLACK:   0-29    → halt everything + emergency alert
```

| Component | Weight | Source |
|---|---|---|
| Service Availability | 20% | Layer 1 presence checks |
| Canary Success Rate | 25% | Last 5 canary results + SLA |
| Config Correctness | 15% | /limits vs expected values |
| Error Rate | 15% | HTTP 4xx/5xx across services (last 15 min) |
| Pipeline Throughput | 10% | Submission → verdict → execution ratio |
| Data Freshness | 10% | VIX age, last agent scan, last Axiom pool |
| Session Activity | 5% | Any agent session active in last 5 min |

OMNI reads composite score before every synthesis → applies size multiplier.

---

## Canary Repositioned (V5 Amendment)

**Old model:** Synthetic pick injected as proxy for real pipeline health.  
**New model:** Canary confirms result of REAL data flow — it does not substitute for it.

```
Pre-Market Canary Schedule:
  07:45 ET — Stage 1: /health all 9 services
  08:00 ET — Stage 2: push REAL pool to agents, wait for ≥5 decisions
  08:15 ET — Stage 3: inject ONE real concordance, verify synthesis pool completes
  08:30 ET — Stage 4: dry-run execution probe (options-specific, V2 amendment)
  09:00 ET — TRADING CLEARED (all 4 green) or TRADING BLOCKED (any red)

Intraday (every 30 min during market hours):
  Re-run Stages 2-4 live — not "did we pass this morning?"
  Uses probe mutex (V3 amendment) to prevent concurrent probes

Guaranteed cleanup (V5 amendment):
  Every canary pick tagged canary=true
  Cleanup job runs after every canary: DELETE FROM submissions WHERE canary=true
  AND created_at < now - 5min. Prevents canary pollution in real metrics.
```

---

## Schema Version Gate (V8 Amendment)

Hard blocker at service startup:

```python
REQUIRED_CHRONICLE_SCHEMA = "2.1"

def _assert_schema_version():
    version = chronicle_client.get_schema_version()
    if version != REQUIRED_CHRONICLE_SCHEMA:
        raise SystemExit(
            f"FATAL: CHRONICLE schema {version} != required {REQUIRED_CHRONICLE_SCHEMA}. "
            f"Run migration before starting nexus-integrity."
        )
```

Service refuses to start if CHRONICLE schema mismatches. Closes an entire class of silent compatibility bugs.

---

## CHRONICLE Integration (V4 Amendment)

CHRONICLE is the single source of truth for all monitoring state. Local SQLite (`nexus_integrity.db`) is a write-ahead cache only — CHRONICLE is authoritative.

```
Tables used in CHRONICLE:
- monitoring_state       → current composite score + component breakdown
- monitoring_thresholds  → all SPC thresholds (regime-aware)
- canary_log             → every canary result + stage latencies
- intervention_log       → every auto-fix attempt + outcome
- trs_log                → all probe results (TRS audit trail)
- spc_metrics            → daily SPC snapshots for cross-day pattern detection
```

Write pattern: local cache first (non-blocking) → CHRONICLE async (best-effort, never blocks pipeline).

---

## Files to Build

```
nexus/nexus-integrity/
├── main.py                  # FastAPI port 8011, startup schema gate, scheduler
├── config.py                # All config + CHRONICLE threshold loader
├── trs.py                   # Transaction Result Store (WAL SQLite, fail-closed)
├── flow_verifier.py         # 5-stage pipeline flow verifier (Layer 2)
├── canary.py                # Pre-market + intraday canary (V5 repositioned)
├── probe.py                 # Execution probe (V2 options-specific, V3 isolated)
├── composite.py             # Composite health score aggregator
├── spc_engine.py            # Statistical Process Control + regime-aware thresholds
├── self_healer.py           # Auto-remediation + recovery asserter (V6)
├── chronicle_writer.py      # CHRONICLE async writer (V4 unified state)
├── notifier.py              # Telegram alerts (SOVEREIGN routing, Ahmed only on escalation)
├── .env                     # Secrets + config
├── requirements.txt
├── ai.nexus.nexus-integrity.plist   # LaunchAgent
└── tests/
    └── test_nexus_integrity.py      # 15 tests (see below)
```

---

## API Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| GET /health | None | Service liveness |
| GET /composite-health | X-Nexus-Secret | Full composite score + components |
| GET /flow-status | X-Nexus-Secret | Current 5-stage flow state |
| GET /canary/latest | X-Nexus-Secret | Last canary result per type |
| GET /spc/current | X-Nexus-Secret | SPC metrics + anomaly flags |
| POST /canary/run | X-Nexus-Secret | Trigger canary manually |
| GET /thresholds | X-Nexus-Secret | Current regime-aware thresholds |
| POST /thresholds | X-Nexus-Secret | Update threshold (Ahmed/SOVEREIGN only) |

---

## Required Tests (15 minimum)

| # | Test | What It Validates |
|---|---|---|
| T1 | Composite score GREEN when all components healthy | Happy path scoring |
| T2 | Composite score AMBER when canary has 2 SLA breaches | Canary weight applied correctly |
| T3 | Composite score RED when execution probe fails | Size multiplier goes to 0.0 |
| T4 | TRS fail-closed: write failure blocks probe result | V1 amendment enforced |
| T5 | Probe mutex: second probe rejected while first in flight | V3 amendment enforced |
| T6 | Canary cleanup: canary=true picks removed after window | V5 amendment enforced |
| T7 | Recovery asserter fires after every auto-fix | V6 amendment enforced |
| T8 | Regime-aware threshold loaded from CHRONICLE | V7 amendment — live threshold read |
| T9 | Schema gate blocks startup on mismatch | V8 amendment enforced |
| T10 | Flow verifier detects Stage 2 failure (0 agent decisions) | Catches today's silent failure |
| T11 | Auto-remediation: Stage 4 OMNI fix attempt triggered | Self-healer dispatch |
| T12 | SOVEREIGN notified on escalation, Ahmed NOT notified | Routing hierarchy enforced |
| T13 | EOD report generates 5 required sections | Governance layer output |
| T14 | SPC anomaly detected: GO verdict rate >3σ | SPC engine sensitivity |
| T15 | CHRONICLE write failure: service continues, local cache used | Resilience: CHRONICLE down ≠ service down |

---

## Failure Modes + Contracts

| Failure | Behaviour |
|---|---|
| CHRONICLE unreachable at startup | Log P1 warning, continue with local cache only |
| CHRONICLE schema mismatch at startup | HARD STOP — service refuses to start (V8) |
| TRS write failure | Probe blocked (fail-closed) — component marked UNKNOWN in composite |
| Concurrent probe attempt | Second probe returns 429, mutex holder completes |
| Layer 2 stage failure | Auto-remediate → recovery assert → SOVEREIGN if assert fails |
| OMNI sizing feed down | Composite score stale; OMNI uses last known multiplier (max 15 min) |
| Canary cleanup failure | Log P2, retry on next cycle — never block live trading |

---

## Environment Variables

```
NEXUS_SECRET=62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2
PROBE_SECRET=<dedicated probe key — not shared with NEXUS_SECRET>
CHRONICLE_URL=http://192.168.1.42:8020
CHRONICLE_SECRET=<from chronicle .env>
REQUIRED_CHRONICLE_SCHEMA=2.1
TELEGRAM_BOT_TOKEN=7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c
TELEGRAM_SOVEREIGN_CHAT_ID=<sovereign chat>
TELEGRAM_AHMED_CHAT_ID=8573754783
DATA_DIR=/Users/ahmedsadek/nexus/data
TRS_DB_PATH=/Users/ahmedsadek/nexus/data/trs.db
INTEGRITY_DB_PATH=/Users/ahmedsadek/nexus/data/nexus_integrity.db
PORT=8011
LOG_LEVEL=INFO
FLOW_VERIFY_INTERVAL_S=900
CANARY_INTRADAY_INTERVAL_S=1800
SPC_REFRESH_INTERVAL_S=300
THRESHOLD_REFRESH_INTERVAL_S=300
```

---

## Implementation Phases

### Phase 1 — Core (this build)
- `trs.py`, `flow_verifier.py`, `composite.py`, `probe.py`
- `main.py` with schema gate + scheduler
- All 15 tests passing
- LaunchAgent deployed on port 8011

### Phase 2 — Canary + SPC (next session)
- `canary.py` (repositioned per V5)
- `spc_engine.py` with CHRONICLE threshold integration
- EOD governance report

### Phase 3 — Self-Healing + OMNI Wiring (session after)
- `self_healer.py` with recovery asserter (V6)
- OMNI sizing integration (composite → multiplier)
- Cross-day pattern detection in CHRONICLE

---

## Open Questions (resolved in spec)

**Q: Override auth model (Cipher concern)** — Resolved: `PROBE_SECRET` is a separate dedicated env var, never shared with service `NEXUS_SECRET`. Override model: probe uses its own key, mutex enforced at service level.

**Q: Probe path overlap** — Resolved: `X-Probe-Mode: true` header on all probe calls. Execution service routes probe calls to dry-run handler without touching Alpaca. Services must check this header before any real order path.

---

*GENESIS 🌱 — April 29, 2026*  
*This spec implements all 8 Vector amendments. Zero open gaps.*
