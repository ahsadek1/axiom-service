# SPEC: Nexus Commercial-Grade Monitoring System — v2
**Version:** 2.0  
**Author:** GENESIS 🌱  
**Date:** 2026-04-29  
**Status:** APPROVED — Ahmed Sadek (2026-04-29, ~18:53 ET)  
**Design Authority:** Vector (v2 rebuttal, accepted in full after adversarial review by Cipher + SOVEREIGN + GENESIS)  
**Service:** `nexus-integrity` — new standalone service  
**Port:** 8011  
**LaunchD label:** `ai.nexus.nexus-integrity`

---

## The Core Problem This Solves

Every monitoring system we had asked: **"Is the service process alive?"**  
Zero of them asked: **"Can the system actually execute a trade right now?"**

This is why 13 health checks returned green while the pipeline was silent for 2+ hours.

**This system asks the right question.**

---

## The Three Pillars

### Pillar 1 — Trading Readiness Score (TRS)
A single authoritative score (0–100) that answers: "Is this system ready to execute a trade?"  
Computed by a dedicated TRS sidecar process. Fail-closed by contract.

### Pillar 2 — Pre-Market Verification Pipeline
Sequenced, retry-budgeted stages run before 9:31 AM that prove execution capability — not just service presence.

### Pillar 3 — Alert Severity Ladder
Four-tier alert model. P0 pages Ahmed immediately. Everything else is filtered.

---

## Architecture

```
CHRONICLE (single state store — no new database)
  ├── trs_snapshots       (TRS sidecar — SOLE writer)
  ├── health_events       (one authoritative writer per check_type)
  ├── preflight_manifests (immutable, signed, per trading day)
  └── monitoring_thresholds (regime-aware, Ahmed controls via CHRONICLE)

TRS SIDECAR (dedicated process, Guardian Angel watchdog)
  ├── Reads health_events from CHRONICLE
  ├── Applies regime-aware threshold (from Axiom)
  ├── Computes TRS with sub-score TTL enforcement
  ├── Pushes score to execution services every 90s
  └── Fails closed on ANY error — no exceptions

EXECUTION SERVICES (alpha-exec, prime-exec)
  ├── Cache TRS with 3-minute TTL
  ├── Hard-block if TRS < threshold OR cache expired
  ├── Schema version gate on startup
  └── No fail-open paths

PRE-MARKET PIPELINE
  06:00 → Snapshot + schema version check
  07:00 → Deep probe (Tier 3, CHRONICLE-cached)
  08:00 → Alpaca options probe (chain + contract + spread)
  08:15 → Concordance dry-run drill
  08:30 → TRS compute + regime threshold set
  09:15 → Final watchlist check
  09:31 → Live canary (CONFIRMATION only — not verification)
  └── Canary cleanup job runs 60s after, every time

ALERT LADDER
  P0 → Immediate, no suppression, human ACK required within 5 min
  P1 → Once per state change, escalates to P0 after 15 min without ACK
  P2 → Hourly digest, never individual page
  P3 → CHRONICLE only, never Telegram
```

---

## The TRS — Fail-Closed by Contract

### Non-negotiable contract

```python
def get_trading_readiness_score() -> TRSResult:
    try:
        score = _read_trs_from_chronicle()
        if score is None:
            return TRSResult(score=0, reason="TRS_NOT_FOUND", blocking=True)
        if score.age_seconds > TRS_TTL_SECONDS:
            return TRSResult(score=0, reason="TRS_STALE", blocking=True)
        return score
    except Exception as e:
        # ANY exception = hard block. No fail-open. Ever.
        return TRSResult(score=0, reason=f"TRS_FETCH_ERROR: {e}", blocking=True)
```

Every failure path returns TRS=0 and blocking=True. There is no code path from TRS failure to trading allowed.

### TRS Sub-Score TTL Enforcement

Each dimension has its own TTL. A stale sub-score FAILS its dimension — it does not average in:

| Dimension | TTL | What it measures |
|---|---|---|
| Contract resolution | 6 min | ORATS chain + OCC symbol valid |
| Alpaca live order | 30 min | Options spread accepted by Alpaca |
| AI brain capacity | 10 min | ≥2 brains responding within timeout |
| Data freshness | 4 min | VIX age, last Axiom pool push |
| DB integrity | 15 min | Schema version match, WAL health |
| Exit monitor active | 3 min | Exit monitor running, last check < 5 min |

### Binary Floor Blockers (override composite score)

If ANY of these fail, TRS=0 regardless of all other sub-scores:

| Blocker | Condition | Action |
|---|---|---|
| Alpaca API key | Returns 401/403 | TRS=0, P0 alert |
| ORATS API key | Returns 401/403 | TRS=0, P0 alert |
| DB schema mismatch | Schema version != declared | TRS=0, P0 alert, service refuses to start |
| Execution service down | alpha-exec or prime-exec /health fails | TRS=0, P0 alert |

### Regime-Aware Thresholds

The TRS sidecar reads market regime from Axiom and applies:

| Regime | TRS Minimum |
|---|---|
| NORMAL (VIX < 20) | 75 |
| ELEVATED (VIX 20–30) | 82 |
| STRESSED (VIX 30–40) | 88 |
| CRISIS (VIX > 40) | 95 |
| FOMC / EVENT DAY | regime threshold + 5 |
| UNKNOWN (Axiom unreachable) | 88 (fail to higher threshold) |

**Thresholds stored in CHRONICLE `monitoring_thresholds` table. Ahmed controls them without code deploy. Reviewed every 30 days.**

### Who Watches the TRS Sidecar?

Guardian Angel V4 monitors TRS sidecar heartbeat every 60s.  
If sidecar misses 2 consecutive 90s pushes → Guardian writes `trs_sidecar_dead=true` to CHRONICLE → all execution services read TRS=0 on next cache check → trading halted.

### Human Override Path (audited emergency only)

```
POST /trs/override
{
  "authorized_by": "ahmed",
  "reason": "...",
  "override_score": 90,
  "valid_for_minutes": 30,  # max 30 — cannot be extended without new invocation
  "token": "<OVERRIDE_TOKEN>"
}
```
Override writes to CHRONICLE with human signature. Every trade under override is flagged in execution DB. Visible in daily manifest. Not a backdoor — an audited emergency path.

---

## Pre-Market Pipeline

### Stage-by-Stage with Retry Budgets

| Stage | Time ET | Max Attempts | Backoff | Hard-Fail On |
|---|---|---|---|---|
| Snapshot + schema gate | 06:00 | 1 | — | Any failure (deterministic) |
| Deep probe (Tier 3) | 07:00 | 3 | 3 min | All 3 fail |
| Alpaca options probe | 08:00 | 3 | 2 min | All 3 fail |
| Concordance drill | 08:15 | 2 | 5 min | Both fail |
| TRS compute + regime set | 08:30 | Continuous | 90s refresh | Sidecar death (→ Guardian) |
| Final watchlist check | 09:15 | 2 | 2 min | Both fail on critical dimension |
| Live canary (confirmation) | 09:31 | 2 | 5 min | Both fail |

### Alpaca Options Probe — Three Stages (08:00)

Proves the actual asset class, not a proxy:

**Stage 1 — Account verification**
```
GET /v2/account → ACTIVE status, equity > $0
GET /v2/account/configurations → options_approved_level >= 2
```

**Stage 2 — Chain availability (sentinel tickers)**
```
GET ORATS chain for SPY, QQQ, AAPL (3 sentinel tickers)
PASS: all 3 → chain exists, bid/ask non-zero, < 5 min old
PARTIAL (1/3 fail): WARNING — degraded, trading allowed, CHRONICLE logged
FAIL (2/3 fail): HARD BLOCK
```

**Stage 3 — Spread order dry-run**
```
Submit 1-contract limit order to paper Alpaca:
  client_order_id: "PROBE_[date]_[uuid]"
  qty=1, limit_price = market * 0.80 (will not fill)
Verify: accepted (not rejected) in < 10s
Cancel immediately, verify cancellation confirmed
PASS: round-trip clean in < 30s
FAIL: rejected for auth/permissions reason (not trading-logic)
```

### Canary Repositioned as Confirmation (09:31)

**Before this change:** Canary at 9:31 was the FIRST test of real Alpaca execution.  
**After this change:** By 9:31 the system has already proven it can trade. The canary CONFIRMS it.

By 9:31 AM the pre-market pipeline has verified:
1. Alpaca accepts spread orders of the correct type
2. Contract resolution works for today's watchlist
3. Concordance pipeline flows end-to-end
4. TRS ≥ regime threshold

The 9:31 live canary ($200, minimum size) proves the full live path under real market conditions. If it fails after all prior checks passed — that is new, specific signal (a condition that only exists at market open). That's useful, not a surprise.

### Canary Isolation and Guaranteed Cleanup

```python
class CanaryExecution:
    CANARY_TAG = "CANARY_"  # All canary client_order_ids prefixed here

    async def run(self) -> CanaryResult:
        order_id = None
        try:
            order_id = await self._place_tagged_order()
            result = await self._verify_pipeline(order_id)
            return result
        finally:
            # GUARANTEED cleanup — runs on success, failure, AND exception
            if order_id:
                await self._force_close_canary(order_id)
                await self._verify_cleanup(order_id)
```

Three enforcement layers:
1. **CANARY_ prefix** on all canary order `client_order_id` — exit monitor, P&L tracker, and reconciler all filter these out
2. **Guaranteed `finally` cleanup** — cleanup runs regardless of outcome
3. **Canary position sentinel** — TRS sidecar checks for open CANARY_* positions every 5 min. Any canary position older than 10 min → P0 alert + force close + canary sub-score = 0

---

## Probe Architecture — Quota-Safe and Idempotent

### Three Probe Tiers

| Tier | Frequency | What it checks | External API calls |
|---|---|---|---|
| Tier 1 — Shallow | Every 30s | Process liveness, port response, CHRONICLE connectivity | Zero |
| Tier 2 — Medium | Every 5 min | Internal pipeline flow, DB integrity, TRS freshness | Zero |
| Tier 3 — Deep | Pre-market only (3× per day) | Contract resolution, AI brain capacity, Alpaca options | External |

Tier 3 probes run maximum 3× per day. Never triggered by retry loops. Results cached in CHRONICLE with TTL.

### Probe Idempotency via CHRONICLE Probe Lock

```python
class ProbeCache:
    def get_or_run(self, probe_fn, cache_key: str, ttl_seconds: int):
        # Check CHRONICLE for existing probe_lock
        lock = chronicle.get_probe_lock(cache_key)
        if lock and lock.age_seconds < ttl_seconds:
            return lock.cached_result  # No API call
        
        # Minimum 4 min between executions of same probe
        if lock and lock.age_seconds < 240:
            return lock.cached_result if lock else ProbeResult(skipped=True)
        
        return probe_fn()  # Only runs if cache expired AND rate limit cleared
```

Race condition eliminated at DB level — two concurrent probe triggers cannot both run.

### Dedicated API Budget

- Probes use a **separate ORATS API key** (`ORATS_PROBE_KEY`) — never the trading key
- Probe quota exhaustion cannot affect live trading quota
- Every probe carries `X-Nexus-Probe: true` header — distinguishable in ORATS logs

---

## CHRONICLE Schema — No New Database

All state in CHRONICLE. No fourth database.

```sql
-- Health events (one authoritative writer per check_type — enforced by CHRONICLE API)
CREATE TABLE health_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    source TEXT NOT NULL,        -- authoritative writer ID only
    service TEXT NOT NULL,
    check_type TEXT NOT NULL,
    result TEXT NOT NULL,        -- "pass" | "fail" | "warn"
    latency_ms REAL,
    detail TEXT,
    trs_impact REAL,
    schema_version TEXT
);

-- TRS snapshots (TRS sidecar ONLY — CHRONICLE API returns 403 to any other writer)
CREATE TABLE trs_snapshots (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    score REAL NOT NULL,
    components TEXT NOT NULL,    -- JSON: each sub-score + freshness timestamp
    regime TEXT NOT NULL,
    threshold REAL NOT NULL,     -- regime-aware minimum used for this snapshot
    blocking INTEGER NOT NULL,   -- 1 if score < threshold
    computed_by TEXT NOT NULL    -- "trs_sidecar_v1"
);

-- Pre-flight manifests (immutable, signed, one per trading day)
CREATE TABLE preflight_manifests (
    id INTEGER PRIMARY KEY,
    trade_date TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    config_hash TEXT NOT NULL,
    schema_versions TEXT NOT NULL,  -- JSON
    api_key_status TEXT NOT NULL,   -- JSON
    canary_result TEXT NOT NULL,
    trs_score REAL NOT NULL,
    signature TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0
);

-- Regime-aware thresholds (Ahmed controls via CHRONICLE, no redeploy needed)
CREATE TABLE monitoring_thresholds (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    regime TEXT DEFAULT 'ALL',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
```

**Writer authority enforced at CHRONICLE API layer** — not by convention. A misconfigured agent writing to a dimension it doesn't own gets a 403. One source per dimension.

---

## Alert Severity Ladder

| Tier | Condition | Response | Suppression |
|---|---|---|---|
| P0 | Trading hard-blocked (TRS=0, execution refusing) | Immediate Telegram to Ahmed + Health Group. Human ACK within 5 min. | Never suppressed |
| P1 | Trading degraded (TRS 60–84, reduced capacity) | Health Group alert. Agent ACK within 15 min or escalates to P0. | Suppressed after first alert until state changes |
| P2 | Anomaly detected (single probe fail, latency spike) | CHRONICLE log + Health Group once per hour | Batched into hourly digest |
| P3 | Informational (successful probes, TRS refreshes, heartbeats) | CHRONICLE only | Always suppressed from Telegram |

---

## Schema Version Gate

Every service declares `SCHEMA_VERSION` in its config. On startup, before accepting any traffic:

```python
def _verify_schema_version():
    actual = _get_db_schema_version()
    expected = SCHEMA_VERSION
    if actual != expected:
        chronicle.write("schema_mismatch", {
            "service": SERVICE_NAME,
            "expected": expected,
            "actual": actual,
        })
        sys.exit(1)  # Hard exit — launchd restarts, CHRONICLE has the record
```

Catches schema drift at service restart — not at 9:35 AM when a trade fails.

---

## Break Tests — Post-Deployment + Recovery Assertions

Break tests run on two triggers:
1. **Sunday night** — full suite
2. **After every production deployment** — targeted 5-minute subset covering changed subsystems

Every break test includes a recovery assertion:

```python
def run_break_test(fault_injection_fn, recovery_assertion_fn, timeout_s=120):
    fault_injection_fn()
    assert wait_for_trs_drop(timeout_s=30), "BREAK TEST FAIL: fault not detected"
    remove_fault()
    assert wait_for_trs_recovery(timeout_s=60), "BREAK TEST FAIL: system did not recover"
    assert recovery_assertion_fn(), "BREAK TEST FAIL: recovery state invalid"
```

**Meta-test runs first** — injects a known-detectable fault, verifies the break test infrastructure itself can detect it. If meta-test fails → P0 alert, full suite aborted. You cannot trust a break test you haven't confirmed is reaching the system under test.

---

## Files to Build

```
nexus/nexus-integrity/
├── main.py               # FastAPI port 8011, schema gate at startup, scheduler
├── config.py             # All config + CHRONICLE threshold loader (regime-aware)
├── trs_sidecar.py        # Dedicated TRS computation process (fail-closed by contract)
├── probe_cache.py        # Probe idempotency + quota management (CHRONICLE probe_lock)
├── alpaca_probe.py       # 3-stage options probe (chain + contract + spread dry-run)
├── canary.py             # Pre-market + intraday canary (confirmation, not verification)
├── pipeline_verifier.py  # 5-stage pre-market pipeline (06:00–09:31)
├── health_aggregator.py  # Composite health + binary floor blockers
├── alert_ladder.py       # P0/P1/P2/P3 routing + suppression logic
├── chronicle_writer.py   # CHRONICLE async writer with writer authority enforcement
├── break_tests.py        # Break test suite + meta-test + recovery assertions
├── notifier.py           # Telegram alerts (P0→Ahmed, P1→Health Group)
├── .env
├── requirements.txt
├── ai.nexus.nexus-integrity.plist
└── tests/
    └── test_nexus_integrity.py   # 15 tests — ALL must pass
```

---

## Required Tests (15)

| # | Test | Vector Amendment Validated |
|---|---|---|
| T1 | TRS returns 0 when CHRONICLE unreachable | Fail-closed by contract |
| T2 | TRS returns 0 when score is stale (TTL expired) | Sub-score TTL enforcement |
| T3 | Binary floor blocker (Alpaca 401) forces TRS=0 regardless of composite | Binary floor blockers |
| T4 | TRS=0 when DB schema mismatch detected | Schema version gate |
| T5 | Alpaca options probe: 401 response = FAIL (permissions check) | Options-specific probe |
| T6 | Alpaca options probe: limit order 20% below market = PASS | Options-specific probe |
| T7 | Probe cache: second call within TTL returns cached result, no API call | Probe idempotency |
| T8 | Probe mutex: concurrent probe returns cached result, doesn't double-execute | Probe quota safety |
| T9 | Canary cleanup: CANARY_ prefixed positions removed in finally block | Guaranteed canary cleanup |
| T10 | Canary sentinel: CANARY_ position >10 min old → TRS canary sub-score = 0 | Canary isolation |
| T11 | Regime-aware threshold: FOMC day applies regime+5 minimum | Regime-aware thresholds |
| T12 | Alert ladder: P1 without ACK in 15 min → escalates to P0 | Alert severity ladder |
| T13 | Alert dedup: same P2 condition within 30 min → only one alert sent | Alert suppression |
| T14 | Break test meta-test: infrastructure self-check fires before suite | Recovery assertions |
| T15 | Pre-flight manifest: written to CHRONICLE at end of pre-market pipeline | CHRONICLE as state store |

---

## Environment Variables

```
NEXUS_SECRET=62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2
PROBE_SECRET=integrity_probe_7x9k2m4n8p1q3r5s6t
ORATS_PROBE_KEY=<dedicated probe key — not the trading key>
CHRONICLE_URL=http://192.168.1.42:8020
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_KEY=PKPGM3BRNYPGCF5Z56IAUZCZJL
ALPACA_SECRET=5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs
TELEGRAM_BOT_TOKEN=7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c
TELEGRAM_AHMED_CHAT_ID=8573754783
TELEGRAM_HEALTH_GROUP_ID=-5241272802
DATA_DIR=/Users/ahmedsadek/nexus/data
PORT=8011
TRS_TTL_SECONDS=180
TRS_PUSH_INTERVAL_S=90
PROBE_MIN_INTERVAL_S=240
LOG_LEVEL=INFO
```

---

## Build Phases

**Phase 1 — Core (this build):**  
TRS sidecar, probe architecture, schema gate, CHRONICLE writer, alert ladder, 15 tests, LaunchAgent live on port 8011.

**Phase 2 — Pre-market pipeline (next session):**  
Full 06:00–09:31 sequenced pipeline, canary repositioned as confirmation, break test suite.

**Phase 3 — OMNI wiring + Guardian integration (session after):**  
OMNI reads TRS → applies size multiplier. Guardian Angel watches TRS sidecar heartbeat.

---

*GENESIS 🌱 — April 29, 2026*  
*Built to Vector's v2 design. All 9 engineering decisions incorporated exactly as specified.*
