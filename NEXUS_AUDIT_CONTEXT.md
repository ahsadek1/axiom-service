# NEXUS_AUDIT_CONTEXT.md
### Mandatory Pre-Read for All Code Auditors — New Nexus Architecture

> **This document is a precision instrument, not a summary.**
> Every auditor — human or AI — must read every section before reviewing any Nexus component.
> Vagueness is a defect. Omission is a failure.

---

## CHANGELOG

| Version | Date | Author | Change |
|---------|------|--------|--------|
| v1.0 | 2026-04-12 | GENESIS | Initial document — new architecture (9-service local monorepo) |
| v1.1 | 2026-04-12 | GENESIS | Resolved 10 VERIFY items from code; fixed 2 new CRITICAL bugs (Axiom fail-open, OMNI state race); added Incidents 9–10; removed 3 false-positive VERIFY tags; added MIN_DTE entry gate; clarified ORACLE/OMNI data flow; scrubbed live bot token (INV-8 violation) |

---

════════════════════════════════════════════════════════════════
## SECTION 1 — SYSTEM IDENTITY
════════════════════════════════════════════════════════════════

| Field | Value |
|-------|-------|
| **System Name** | Nexus Trading System — comprising Nexus Alpha (options) and Nexus Prime (swing equity) |
| **Subsystems** | Axiom, Alpha Buffer, Prime Buffer, OMNI, Alpha Execution, Prime Execution, ORACLE, AILS, Guardian Angel |
| **System Owner & Operator** | Ahmed Sadek |
| **Classification** | Live capital trading system — financially consequential. Zero tolerance for silent failures. Every wrong order, missed brake, or phantom position has direct monetary consequence. |
| **Current Deployment Phase** | Paper trading (Alpaca paper account). Architecture is production-grade; capital is real-equivalent paper. |
| **Deployment Environment** | Fully local — Mac mini (Apple Silicon, Darwin 25.3.0, arm64). No Railway. No cloud services. All 9 microservices run as macOS LaunchAgent daemons. |
| **Last Architecture Review** | 2026-04-12 (adversarial audit complete — Pass A: o3-mini, Pass B: DeepSeek R1) |
| **Document Version** | v1.1 |
| **Total Test Coverage** | 384/384 tests passing across all 9 services |
| **Codebase Root** | `/Users/ahmedsadek/nexus/` |

---

════════════════════════════════════════════════════════════════
## SECTION 2 — SYSTEM PURPOSE & MISSION CRITICALITY
════════════════════════════════════════════════════════════════

### 2A. Nexus Alpha — Options System

| Field | Detail |
|-------|--------|
| **What it does** | Identifies high-probability options spread opportunities (bull put spreads, bear call spreads) using a 3-agent concordance process followed by a 4-brain AI synthesis |
| **Asset class** | US equity options — credit spreads (defined-risk) |
| **Timeframe** | Intraday signal generation; trade duration typically 14–45 DTE |
| **Exchange/Broker** | Alpaca Paper Trading (`https://paper-api.alpaca.markets`) |
| **Base position size** | $2,000 USD |
| **Max concurrent positions** | 10 |
| **Capital pool** | $25,000 USD (paper) |
| **Silent failure consequence** | Wrong spread legs closed, wrong contracts opened, phantom position created before Alpaca confirmation, missed DTE roll resulting in assignment risk, duplicate order on retry |
| **Why correctness is non-negotiable** | A missed VIX brake in ELEVATED/STRESS regime could result in opening options positions just as volatility spikes — maximum theoretical loss on a 10-position book |

**Critical exit rules:**
- At +35% gain: close 50% of position; activate 15% trailing stop on remainder
- At +100% gain: close full position immediately
- DTE ≤ 21: flag for roll consideration
- DTE ≤ 7: force close
- DTE ≤ 5: emergency close regardless of P&L

---

### 2B. Nexus Prime — Swing Equity System

| Field | Detail |
|-------|--------|
| **What it does** | Identifies multi-day swing equity trades using fundamental, technical, and macro concordance, synthesized by OMNI |
| **Asset class** | US equities — long/short swing positions |
| **Timeframe** | Signal on intraday; holding period 2–14 days |
| **Exchange/Broker** | Alpaca Paper Trading |
| **Base position size** | $2,000 USD |
| **Max concurrent positions** | 6 |
| **Capital pool** | $25,000 USD (paper) |
| **Silent failure consequence** | Position opened without stop set, phantom equity position in DB with no Alpaca counterpart, trailing stop never activated on +35% gain, technical invalidation missed |
| **Why correctness is non-negotiable** | Swing positions are held overnight and across weekends — a failure to register an exit trigger means an unprotected position during pre/after market gap |

**Critical exit rules:**
- At +35% gain: close 50%; activate 12% trailing stop on remainder
- At +50% gain: tighten trailing stop to 8%
- Technical invalidation: primary stop trigger (setup breakdown = immediate exit regardless of P&L)
- At -18% loss: backstop hard close

---

════════════════════════════════════════════════════════════════
## SECTION 3 — FULL ARCHITECTURE MAP
════════════════════════════════════════════════════════════════

### 3A. Agent Roster

| Agent | Model | Role | Weight | Runs On | Communication |
|-------|-------|------|--------|---------|---------------|
| **Cipher** | Claude Sonnet 4.6 (`anthropic/claude-sonnet-4-6`) | Lead Alpha options analyst + Team architect + Root agent | 45% | Mac mini (OpenClaw main session) | Direct API via OpenClaw |
| **Atlas** | Gemini 2.5 Pro (`google/gemini-2.5-pro`) | Technical analysis — chart patterns, momentum, sector rotation; Technical Invalidation Watcher for Prime swing positions | 30% | Mac mini (OpenClaw atlas session) | Direct API via OpenClaw |
| **Sage** | Manus (`manus`) | Fundamental + sentiment; Macro Sentinel — watches economic calendar and flags high-risk days to Axiom | 25% | Mac mini (OpenClaw sage session) | Direct API via OpenClaw |

**Agent weight integrity rule:** Cipher (45%) + Atlas (30%) + Sage (25%) = 100%. Any change to weights must revalidate this sum. Valid agent names: `cipher`, `sage`, `atlas` only. Agent `trident` is permanently retired — any submission from `trident` must be rejected.

**Known agent fragilities:**
- Sage (Manus) currently has no live news feed — Benzinga/Seeking Alpha subscriptions pending. Fundamental analysis relies on SEC EDGAR + Alpha Vantage + macro calendar only.
- Atlas needs explicit prompt structure for technical invalidation calls on Prime positions.
- All agents have a `[AHMED TO CONFIRM: exact invocation flow — does Axiom call agents directly or do agents poll Axiom?]`

---

### 3B. Concordance & Decision Pipeline

**Concordance pathways — 4 defined:**

| Pathway | Requirement | Position Size Multiplier | GO Threshold |
|---------|------------|--------------------------|--------------|
| **P1** | All 3 agents submit; total score ≥ threshold | 100% ($2,000) | Alpha: 65 / Prime: 70 |
| **P2** | 2 of 3 agents submit; each agent score ≥ 78 | 75% ($1,500) | Alpha: 65 / Prime: 70 |
| **P3** | Solo agent submission; agent score ≥ 90 | 50% ($1,000) | Minimum 3/4 OMNI brains must vote GO |
| **P4** | OMNI independent scan (no agent submission required) | 25% ($500) | Minimum 3/4 OMNI brains must vote GO |

**Alpha Buffer thresholds:**
- `MIN_SUBMISSION_SCORE` = 58 (minimum to even enter the buffer)
- `GO_THRESHOLD_P1` = 65
- `MIN_SCORE_P2` = 78
- `MIN_SCORE_SOLO_P3` = 90

**Prime Buffer thresholds:**
- `MIN_SUBMISSION_SCORE` = 63
- `GO_THRESHOLD_P1` = 70
- `MIN_SCORE_P2` = 78
- `MIN_SCORE_SOLO_P3` = 90

**OMNI Quad Intelligence verdict rules:**
- 4/4 brains vote GO → `STRONG_GO`
- 3/4 brains vote GO → `GO`
- 2/4 brains vote GO → `CONDITIONAL` (Ahmed decides via Telegram)
- 0–1 brains vote GO → `NO_GO`
- P3/P4 pathways: minimum 3/4 brains required for execution

**VIX regime thresholds — exact values (confirmed from `axiom/regime.py`, approved Ahmed Sadek 2026-04-10):**

| Classification | VIX Range | Alpha Credit | Alpha Debit | Prime | Notes |
|----------------|-----------|-------------|-------------|-------|-------|
| `LOW_VOL` | VIX < 12 | ✅ | ✅ | ✅ | Size 75% — snap-back risk |
| `NORMAL` | 12 ≤ VIX < 20 | ✅ | ✅ | ✅ | Full size |
| `ELEVATED` | 20 ≤ VIX < 25 | ✅ | ✅ | ✅ | Full size, monitor |
| `STRESS` | 25 ≤ VIX < 30 | ✅ | ❌ PAUSED | ✅ cap 3/day | Alpha debit halted |
| `HIGH_STRESS` | 30 ≤ VIX < 35 | ✅ cap 2/day | ❌ HALTED | ❌ PAUSED | Alpha size 75% |
| `CRISIS` | VIX ≥ 35 | ❌ HALTED | ❌ HALTED | ❌ HALTED | No new entries |

**v4 Composite regime** (primary when ORACLE macro data available): 4-factor composite score (0–100)
- VIX (0–25 pts) + HY credit spread (0–25 pts) + yield curve 10Y–2Y (0–25 pts) + put/call ratio (0–25 pts)
- Composite ≤15 = LOW_VOL | ≤30 = NORMAL | ≤45 = ELEVATED | ≤60 = STRESS | ≤75 = HIGH_STRESS | >75 = CRISIS
- Falls back to VIX-only (`classify_regime()`) when ORACLE macro data is unavailable

**Axiom hard-stop gates (checked by OMNI before routing to execution):**
- `hard_stops` list in Axiom `/assess` response — any entry blocks execution regardless of verdict
- **CRITICAL — FAIL-SAFE**: If Axiom is unreachable (timeout or error), OMNI now returns `hard_stops=["AXIOM_UNREACHABLE"]` → execution blocked. Fixed in Pass B (was previously permissive — Incident 9).
- `sizing_mult` from Axiom adjusts position size (0.0 on unreachable = zero size)

**Buffer window model:**
- Windows are 15-minute time buckets: `window_id = YYYY-MM-DD-HHMM` rounded down to 15-minute boundary
- Window "expires" naturally when the clock advances to a new 15-minute slot
- Submissions after window ID changes are counted in the NEW window (no grace period)
- P1 falls through to P2 if 3 agents submitted but weighted score < threshold (not treated as timeout)

---

### 3C. Execution Pipeline

**Alpha Execution sequence (port 8005):**
1. OMNI POSTs to `/execute` with verdict + concordance payload
2. Validate auth (`X-Nexus-Secret`) and position limit (max 10)
3. Build OCC contract symbols for short and long legs using `_build_contract_symbol()`
4. Validate expiry via `_third_friday()` helper
5. Alpaca order placement (short leg first, then long leg)
6. **DB write ONLY after both Alpaca orders confirmed** — no phantom positions
7. Trailing stop parameters set in DB before any Alpaca close call
8. Register position in `positions` table with `short_contract_symbol`, `long_contract_symbol`
9. Send Telegram entry notification to Ahmed
10. Post outcome to AILS after close (via `ails_reporter.py`)

**Prime Execution sequence (port 8006):**
1. OMNI POSTs to `/execute` with verdict + concordance payload
2. Validate auth (`X-Nexus-Prime-Secret`) and position limit (max 6)
3. Validate `position_size_usd` (1–50,000), `sizing_mult` (0.1–5.0), `weighted_score` (0–100)
4. Calculate shares: `position_size_usd / current_price`, rounded to 2dp minimum 1.0
5. Alpaca order placement (market order)
6. **DB write ONLY after Alpaca confirms** — on Alpaca failure: return 503, no DB record
7. Set stop-loss parameters before monitoring activates
8. Post outcome to AILS after close

**Position sizing formula:**
```
base_size = 2000 USD
final_size = base_size * sizing_multiplier * pathway_multiplier
pathway_multipliers: P1=1.0, P2=0.75, P3=0.5, P4=0.25
```

**Telegram notification trigger points:**
- Entry confirmed (position opened)
- Exit triggered (position closed with P&L)
- CONDITIONAL verdict (Ahmed must decide)
- System halt (VIX brake, regime override)
- Guardian Angel healing event
- Bot: `@GENESIS15BOT` | Token: `[STORED IN .env — NEVER DOCUMENT]` | Chat ID: `[STORED IN .env — NEVER DOCUMENT]`
- **NOTE:** Only OUTBOUND notifications — no service exposes inbound Telegram webhook or command endpoints. There is no Telegram command attack surface in the current architecture.

---

### 3D. Data Ingestion Stack — ORACLE (port 8007)

ORACLE aggregates 9 active data sources. All accessed via Engine modules within ORACLE:

| Engine | Source | Data Type | Auth Method | Fallback |
|--------|--------|-----------|-------------|----------|
| 1 | **Polygon.io** | Real-time price, options chains, historical OHLCV | API key header | Cached last-known |
| 2 | **ORATS** | IV surface, spread pricing, historical vol | API key | IV estimated from Polygon |
| 3 | **SpotGamma** | GEX levels, Call/Put walls, Gamma Flip | **No public API** — `spotgamma_client.py` explicitly states "SpotGamma has no programmatic API (dashboard only)." GEX data is approximated via Polygon math fallback. `SPOTGAMMA_KEY` env var is a placeholder. | Polygon math fallback always active |
| 4 | **Unusual Whales** | Options flow sweeps, dark pool, Congress trades | API key | Skip flow signal |
| 5 | **Market Chameleon** | IV rank/percentile, earnings expected moves | API key (pending — stub active) | Polygon math fallback |
| 6 | **Trading Economics** | Fed, CPI, NFP, macro calendar | API key (pending — stub active) | FRED fallback |
| 7 | **FRED** | VIX history, rate environment, macro time series | API key | None (FRED is itself a fallback) |
| 8 | **Alpha Vantage** | Earnings calendar, financial ratios | API key | Skip fundamental signal |
| 9 | **SEC EDGAR** | Form 4 insider trades, 13F holdings, 8-K events | User-Agent header only (free) | Skip insider signal |

**ORACLE rate limiting:** Per-platform token bucket rate limiter. Unknown platforms get 120 RPM (2 req/s) conservative default. Requests exceeding bucket → 429 returned to caller with retry guidance.

**ORACLE caching:** Two-tier. L1 = in-memory dict. L2 = SQLite. Corrupted L2 entries caught via `json.JSONDecodeError` and treated as cache miss.

**ORACLE→OMNI data flow (important architectural clarification):**
- ORACLE is called by **agents** (Cipher, Atlas, Sage) to enrich their signal analysis before submitting to the buffers.
- OMNI does NOT call ORACLE during synthesis. OMNI's `build_context()` receives only: concordance payload + Axiom risk assessment + Axiom regime.
- Therefore "ORACLE partial failure" affects agent signal quality but does NOT affect OMNI's synthesis pipeline directly.

**Stub behavior for missing API keys:**
- Market Chameleon + Trading Economics: keys pending. Engines return `{"_stub": True}` dict, not None and not an error. ORACLE includes this partial data in the context packet without flagging it as degraded.
- When both Unusual Whales AND Market Chameleon return `_stub=True`, flow_engine returns stub flow data — agents receive it silently. No alert sent.
- **Auditor flag:** Any code reading ORACLE context should check for `_stub=True` fields and treat them as absent data, not real data.

**Missing data sources (gaps):**
- Live news / market narrative: Benzinga Pro (~$99/mo) or Seeking Alpha (~$20/mo) — `[AHMED TO CONFIRM: subscription decision]`
- yfinance (free) as Polygon outage fallback — `[AHMED TO CONFIRM: add as redundancy?]`

---

### 3E. Infrastructure Map

**All services run locally on Mac mini. No Railway. No cloud.**

| Service | Port | Auth Header (inbound) | LaunchAgent Plist | Log Path |
|---------|------|-----------------------|-------------------|----------|
| Axiom | 8001 | `X-Axiom-Secret` | `ai.nexus.axiom.plist` | `/Users/ahmedsadek/nexus/logs/axiom/stderr.log` |
| Alpha Buffer | 8002 | `X-Nexus-Secret` | `ai.nexus.alpha-buffer.plist` | `/Users/ahmedsadek/nexus/logs/alpha-buffer/stderr.log` |
| Prime Buffer | 8003 | `X-Nexus-Prime-Secret` | `ai.nexus.prime-buffer.plist` | `/Users/ahmedsadek/nexus/logs/prime-buffer/stderr.log` |
| OMNI | 8004 | `X-Nexus-Secret` OR `X-Nexus-Prime-Secret` (both accepted) | `ai.nexus.omni.plist` | `/Users/ahmedsadek/nexus/logs/omni/stderr.log` |
| Alpha Execution | 8005 | `X-Nexus-Secret` | `ai.nexus.alpha-execution.plist` | `/Users/ahmedsadek/nexus/logs/alpha-execution/stderr.log` |
| Prime Execution | 8006 | `X-Nexus-Prime-Secret` | `ai.nexus.prime-execution.plist` | `/Users/ahmedsadek/nexus/logs/prime-execution/stderr.log` |
| ORACLE | 8007 | `X-Oracle-Secret` | `ai.nexus.oracle.plist` | `/Users/ahmedsadek/nexus/logs/oracle/stderr.log` |
| AILS | 8008 | `X-Ails-Secret` | `ai.nexus.ails.plist` | `/Users/ahmedsadek/nexus/logs/ails/stderr.log` |
| Guardian Angel | 8009 | `X-Guardian-Secret` | `ai.nexus.guardian-angel.plist` | `/Users/ahmedsadek/nexus/logs/guardian-angel/stderr.log` |

**LaunchAgent plist location:** `~/Library/LaunchAgents/ai.nexus.<service>.plist`
**Restart behavior:** `KeepAlive = true` — launchd restarts any service that exits unexpectedly.
**Management commands:** `launchctl load/unload ~/Library/LaunchAgents/ai.nexus.<service>.plist`

**Inter-service URLs (all localhost):**
All services communicate over `http://localhost:<port>`. No TLS between services (local loopback only).

**Environment variable names — per service:**

*Axiom (8001):*
`AXIOM_SECRET`, `NEXUS_WEBHOOK_SECRET`, `NEXUS_PRIME_SECRET`, `TELEGRAM_BOT_TOKEN`, `AHMED_CHAT_ID`, `AXIOM_DB_PATH`, `PORT`

*Alpha Buffer (8002):*
`NEXUS_WEBHOOK_SECRET`, `OMNI_URL`, `OMNI_SECRET`, `TELEGRAM_BOT_TOKEN`, `AHMED_CHAT_ID`, `ALPHA_BUFFER_DB_PATH`, `PORT`

*Prime Buffer (8003):*
`NEXUS_PRIME_SECRET`, `OMNI_URL`, `OMNI_SECRET`, `TELEGRAM_BOT_TOKEN`, `AHMED_CHAT_ID`, `PRIME_BUFFER_DB_PATH`, `PORT`

*OMNI (8004):*
`NEXUS_WEBHOOK_SECRET`, `NEXUS_PRIME_SECRET`, `OMNI_SECRET`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `AXIOM_URL`, `AXIOM_SECRET`, `ALPHA_EXECUTION_URL`, `PRIME_EXECUTION_URL`, `TELEGRAM_BOT_TOKEN`, `AHMED_CHAT_ID`, `OMNI_DB_PATH`, `PORT`

*Alpha Execution (8005):*
`NEXUS_WEBHOOK_SECRET`, `ALPACA_KEY`, `ALPACA_SECRET`, `ALPACA_BASE_URL`, `ALPHA_BUFFER_URL`, `NEXUS_WEBHOOK_SECRET` (for buffer callback), `ORACLE_URL`, `ORACLE_SECRET`, `AILS_URL`, `AILS_SECRET`, `TELEGRAM_BOT_TOKEN`, `AHMED_CHAT_ID`, `ALPHA_EXEC_DB_PATH`, `PORT`

*Prime Execution (8006):*
`NEXUS_PRIME_SECRET`, `ALPACA_KEY`, `ALPACA_SECRET`, `ALPACA_BASE_URL`, `PRIME_BUFFER_URL`, `NEXUS_PRIME_SECRET` (for buffer callback), `ORACLE_URL`, `ORACLE_SECRET`, `AILS_URL`, `AILS_SECRET`, `TELEGRAM_BOT_TOKEN`, `AHMED_CHAT_ID`, `PRIME_EXEC_DB_PATH`, `PORT`

*ORACLE (8007):*
`ORACLE_SECRET`, `POLYGON_KEY`, `ORATS_KEY`, `SPOTGAMMA_KEY`, `UNUSUAL_WHALES_KEY`, `MARKET_CHAMELEON_KEY`, `TRADING_ECONOMICS_KEY`, `FRED_KEY`, `ALPHA_VANTAGE_KEY`, `AILS_URL`, `AILS_SECRET`, `PORT`

*AILS (8008):*
`AILS_SECRET`, `AILS_DB_PATH`, `PORT`

*Guardian Angel (8009):*
`GUARDIAN_SECRET`, `TELEGRAM_BOT_TOKEN`, `AHMED_CHAT_ID`, `PORT`
`[AHMED TO CONFIRM: full list of service URLs Guardian Angel monitors — confirm it checks all 8]`

---

════════════════════════════════════════════════════════════════
## SECTION 4 — BUSINESS INVARIANTS
════════════════════════════════════════════════════════════════

**These rules must NEVER be violated under any condition.**

---

**INVARIANT 1: DB Write Follows Alpaca Confirmation**
- **Rule:** No position record is written to any SQLite database before the corresponding Alpaca order placement returns a successful HTTP 200. On Alpaca failure, the endpoint returns HTTP 503 and NO database record is created.
- **Scope:** Alpha Execution, Prime Execution
- **Violation Consequence:** Phantom positions — the system believes a position exists that Alpaca never received. Exit monitors try to close nonexistent contracts; Telegram shows fake P&L.
- **How Code Enforces It:** Alpaca call is made first; DB INSERT is in the `else` branch of the success check. Tests must assert that Alpaca failure produces 0 DB records.

---

**INVARIANT 2: Trailing Stop Set Before Alpaca Close Call**
- **Rule:** Trailing stop DB update must execute before any Alpaca order submission for a partial or full close.
- **Scope:** Alpha Execution exit monitor
- **Violation Consequence:** If Alpaca call partially succeeds and then the process crashes, the trailing stop is never active on the remaining position — unprotected exposure.
- **How Code Enforces It:** DB trailing stop UPDATE precedes `alpaca_client.close_position()` call. Order is non-negotiable.

---

**INVARIANT 3: GO Threshold Before Execution**
- **Rule:** OMNI must not route to execution unless verdict is `GO` or `STRONG_GO`. `CONDITIONAL` requires explicit Ahmed approval via Telegram. `NO_GO` is silent.
- **Scope:** OMNI synthesis engine
- **Violation Consequence:** Executions on low-conviction signals — degraded win rate, capital at risk without edge.
- **How Code Enforces It:** `verdict.can_execute()` gate in `main.py` Step 8. Only `GO` and `STRONG_GO` return `True`.

---

**INVARIANT 4: Agent Weight Sum = 100%**
- **Rule:** `CIPHER_WEIGHT (0.45) + ATLAS_WEIGHT (0.30) + SAGE_WEIGHT (0.25) == 1.0` — defined in `alpha-buffer/config.py` and `prime-buffer/config.py`
- **Scope:** Alpha Buffer, Prime Buffer (weighted score calculation)
- **Violation Consequence:** Score normalization result changes — partial submissions (P2, P3) would be scored against wrong denominators. Trades could fire on inflated or deflated scores.
- **How Code Enforces It:** The `_weighted_score()` function normalizes by `total_weight = sum(AGENT_WEIGHTS.get(a, 0.0) for a in agent_scores)` — so math stays in 0–100 range for partial submissions. HOWEVER: **there is no startup assertion that weights sum to 1.0**. A weight change that doesn't sum to 1.0 would cause P1 full-submission scores to drift from expected range. This is a known gap — auditor should verify weights are unchanged and flag any modification that doesn't maintain sum == 1.0.

---

**INVARIANT 5: Valid Agent Names Only**
- **Rule:** Only `cipher`, `sage`, `atlas` are valid agent names. Any submission from any other name (including `trident`) must be rejected with HTTP 400.
- **Scope:** Alpha Buffer, Prime Buffer
- **Violation Consequence:** Rogue or legacy agents could inject scores, distort concordance, and trigger false executions.
- **How Code Enforces It:** Agent name validation in buffer `submit()` endpoint. Normalized to lowercase before comparison.

---

**INVARIANT 6: Maximum Position Limits**
- **Rule:** Alpha system: max 10 concurrent open positions. Prime system: max 6 concurrent open positions. New execution is rejected (HTTP 429) if at capacity.
- **Scope:** Alpha Execution, Prime Execution
- **Violation Consequence:** Capital overexposure — total book value could exceed $25K per system.
- **How Code Enforces It:** `_execute_lock` + count query in DB before any new position INSERT.

---

**INVARIANT 7: Concordance Minimum Before Order Fires**
- **Rule:** P1 requires all 3 agents in same window. P2 requires 2 agents each ≥ 78 score. P3 requires 1 agent ≥ 90. P4 is OMNI-only. No other combinations allowed.
- **Scope:** Alpha Buffer, Prime Buffer, OMNI
- **Violation Consequence:** Trades fire on incomplete or unverified agent consensus.
- **How Code Enforces It:** Pathway classification logic in buffer; OMNI verifies pathway before adjusting sizing multiplier.

---

**INVARIANT 8: No Credential Hardcoding or Logging**
- **Rule:** API keys, secrets, tokens must never appear in source code, log files, error messages, or Telegram notifications. All secrets loaded from environment variables. `.env` files must not be committed to version control.
- **Scope:** All 9 services
- **Violation Consequence:** Credential exposure — Alpaca key, AI brain keys, webhook secrets compromised.
- **How Code Enforces It:** All secrets loaded via `os.getenv()`. Error messages reference env var names, not values. `[VERIFY: .gitignore has *.env entries]`

---

**INVARIANT 9: Telegram Is Outbound-Only — No Command Attack Surface**
- **Rule:** No Nexus service exposes an inbound Telegram webhook, command endpoint, or any mechanism for Telegram messages to modify system state. All Telegram usage is outbound notifications only (service → Ahmed).
- **Scope:** All 9 services
- **Violation Consequence:** If an inbound webhook endpoint were added without sender validation, arbitrary Telegram users could control the system.
- **How Code Enforces It:** No service has a Telegram webhook route. Confirmed by code review — all `telegram.py` modules contain only `send_message()` functions. The only inbound channel is the authenticated service APIs (each protected by its own secret header).
- **Auditor flag:** If any future PR adds a `/telegram/webhook` or `/bot` endpoint, it must include `sender_id == AHMED_CHAT_ID` validation before any state change.

---

**INVARIANT 10: Alpaca Order Deduplication**
- **Rule:** The same ticker+direction+window combination must not result in duplicate order submissions to Alpaca. Position existence check must occur before order placement.
- **Scope:** Alpha Execution, Prime Execution
- **Violation Consequence:** Doubled position size, doubled capital exposure, doubled loss if trade goes wrong.
- **How Code Enforces It:** DB query for existing open position on same ticker before INSERT. `_execute_lock` prevents concurrent duplicate submissions.

---

**INVARIANT 11: OMNI Inbound Auth — Both Headers Accepted**
- **Rule:** OMNI `/concordance` endpoint must accept both `X-Nexus-Secret` (from Alpha Buffer) and `X-Nexus-Prime-Secret` (from Prime Buffer). Both validate against their respective configured secrets.
- **Scope:** OMNI
- **Violation Consequence:** If only one header accepted, the other system's concordances are silently rejected. The Prime execution path was completely blocked by this exact bug prior to 2026-04-12 Pass B fix.
- **How Code Enforces It:** `active_secret = x_nexus_secret or x_nexus_prime_secret` before `verify_secret()`. Test: `test_prime_buffer_header_accepted`.

---

**INVARIANT 12: OMNI Uses System-Appropriate Secret for Execution Routing**
- **Rule:** When OMNI routes to Alpha Execution, it must use `nexus_secret` (NEXUS_WEBHOOK_SECRET) with header `X-Nexus-Secret`. When routing to Prime Execution, it must use `nexus_prime_secret` (NEXUS_PRIME_SECRET) with header `X-Nexus-Prime-Secret`.
- **Scope:** OMNI execution_router.py
- **Violation Consequence:** Wrong secret sent → 403 from execution service → no execution despite valid GO verdict.
- **How Code Enforces It:** `exec_auth = nexus_secret if system == "alpha" else nexus_prime_secret` in Step 8.

---

**INVARIANT 13: Minimum DTE on Options Entry**
- **Rule:** Alpha Execution will not open a new spread position if the target expiry has fewer than `MIN_DTE = 30` days to expiration. Confirmed in `alpha-execution/config.py`.
- **Scope:** Alpha Execution
- **Violation Consequence:** Opening near-expiry options positions creates heightened assignment risk, rapid theta decay, and limited exit flexibility.
- **How Code Enforces It:** `MIN_DTE = 30` constant in config; `_build_contract_symbol()` / expiry validation checks DTE before order submission.

**INVARIANT 14: SQLite Write Atomicity**
- **Rule:** All multi-step DB writes must execute within a single transaction. No partial writes. Failure at any step must roll back the entire operation.
- **Scope:** AILS (post_outcome), Alpha Execution, Prime Execution, OMNI
- **Violation Consequence:** Partial state written — e.g., Bayesian rate updated but window record not committed, or position partially registered.
- **How Code Enforces It:** AILS uses `BEGIN IMMEDIATE` / `commit()` / `rollback()` under `_db_lock`. Other services use `with conn:` context manager.

**INVARIANT 15: P4 Pathway Scope and Accumulation Gate**
- **Rule:** P4 (OMNI independent scan) submits to the same buffer as agents and is subject to all concordance pathway rules. P4 signals go through the full OMNI synthesis pipeline with `sizing_mult = 0.25`. P4 is subject to the same max position limits (Alpha: 10, Prime: 6). There is no separate P4 accumulation gate — the position limit is the only gate.
- **Scope:** OMNI, Alpha Buffer, Prime Buffer
- **Violation Consequence:** Uncapped P4 scans could accumulate 6–10 small positions at 25% size in low-volatility periods, concentrating risk invisibly.
- **How Code Enforces It:** Position limit enforced in execution services (`_execute_lock` + DB count query). Auditors should verify P4 frequency is bounded by the concordance buffer window (max 1 P4 signal per ticker per 15-minute window).

---

════════════════════════════════════════════════════════════════
## SECTION 5 — DATA FLOW TRACES
════════════════════════════════════════════════════════════════

### 5A. Alpha System — Happy Path (P1 Concordance → Execution)

```
1. [AXIOM] Regime classifier runs. Sets trading window. Classifies VIX regime.
   → Outputs: window_id, regime (LOW_VOL/NORMAL/ELEVATED/STRESS/HIGH_STRESS/CRISIS),
     pool of candidate tickers, risk_allowed=True

2. [AGENTS — Cipher, Atlas, Sage] Each agent independently scans candidate pool.
   → Each submits to Alpha Buffer: POST /submit
     Headers: X-Nexus-Secret
     Payload: {ticker, direction, score (0-100), rationale, agent_id, window_id}

3. [ALPHA BUFFER] Receives submission.
   → Validates: score ≥ 58, agent in {cipher, sage, atlas}, window_id current
   → Checks concordance: 3/3 agents submitted for same ticker+direction in window
   → If P1 concordance reached: computes weighted score
     (Cipher×0.45 + Atlas×0.30 + Sage×0.25)
   → If weighted_score ≥ 65: dispatches to OMNI
   → POST to OMNI /concordance
     Headers: X-Nexus-Secret (Alpha Buffer uses X-Nexus-Secret for OMNI)
     Payload: {ticker, direction, system="alpha", pathway="P1", weighted_score,
               agent_submissions, window_id, sizing_mult}

4. [OMNI] Receives concordance.
   → Verifies X-Nexus-Secret header
   → Fetches Axiom regime (GET /regime with X-Axiom-Secret)
   → Fetches ORACLE context for ticker (GET /oracle/context/{ticker} with X-Oracle-Secret)
   → Builds synthesis context for all 4 brains
   → Runs Quad Intelligence in parallel:
       Brain 1: Claude (synthesis + primary vote)
       Brain 2: o3-mini (adversarial challenge)
       Brain 3: Gemini 2.5 Pro (pattern recognition)
       Brain 4: DeepSeek R1 (momentum analysis)
   → Counts GO votes. 3/4 → verdict=GO, 4/4 → verdict=STRONG_GO
   → Checks Axiom hard stop: TRADING_ALLOWED=true, REGIME_OVERRIDE=false
   → If GO/STRONG_GO: POST to Alpha Execution /execute
     Headers: X-Nexus-Secret
     Payload: {ticker, direction, system="alpha", pathway, sizing_mult,
               synthesis_verdict, weighted_score}

5. [ALPHA EXECUTION] Receives execution order.
   → Validates X-Nexus-Secret
   → Checks position count < 10
   → Builds OCC contract symbols for short + long legs
   → Validates expiry against third-Friday calendar
   → Places Alpaca orders (short leg, then long leg)
   → On Alpaca success: writes position to DB
   → On Alpaca failure: returns HTTP 503, NO DB write
   → Activates exit monitor for this position
   → Sends Telegram entry alert to Ahmed
   → Returns HTTP 200 with position_id to OMNI

6. [AILS] Receives outcome after position closes:
   → POST /outcome from Alpha Execution (via ails_reporter.py)
   → Updates Bayesian win rate for (ticker, strategy, regime, direction)
   → Learning loop active from minute one
```

---

### 5B. Failure Paths

**VIX brake fires mid-pipeline (between Step 3 and 4):**
- OMNI fetches Axiom regime in Step 4. If regime is `STRESS` or worse: `TRADING_ALLOWED=false`
- OMNI returns `NO_GO` verdict with `axiom_hard_stop=true`
- No execution call made. Alpha Buffer marks concordance as `omni_dispatched=true, verdict=NO_GO`
- Telegram silent (NO_GO is not notified)
- Position NOT opened

**One agent fails to respond (P1 → P2 fallthrough):**
- Alpha Buffer window expires with only 2/3 agents submitted
- If both submitted agents have score ≥ 78: P2 concordance triggered
- sizing_mult = 0.75 (75% of base position)
- Pipeline continues from Step 3 with pathway="P2"
- If scores < 78: no concordance, no dispatch

**Alpaca rejects order (Step 5 failure):**
- Alpha Execution receives Alpaca error (non-200 response)
- Returns HTTP 503 to OMNI
- NO DB record created
- OMNI logs execution failure, sends Telegram alert to Ahmed
- Position is NOT in system — next window can retry from scratch

**Axiom unreachable (Steps 1–2 failure):**
- `assess_ticker()` times out → returns `hard_stops=["AXIOM_UNREACHABLE"]`
- `compute_verdict()` sees non-empty `hard_stops` → `axiom_blocked=True`
- Verdict is `NO_GO` / `BLOCKED` regardless of brain votes
- Execution call never made. Ahmed notified via Telegram.
- **This is fail-safe behavior** (fixed Pass B — Incident 9). Original was fail-open.

**ORACLE engine fails (agent signal quality impact):**
- ORACLE is called by agents, NOT by OMNI synthesis
- If an ORACLE engine returns `{"_stub": True}` or None, the agent receives degraded context
- Agent may still submit to the buffer with a lower confidence score
- OMNI does not know ORACLE data was partial — it receives only the agent's final score
- **Auditor flag:** No mechanism currently exists to flag reduced-context verdicts at OMNI level. This is a documented gap.

---

════════════════════════════════════════════════════════════════
## SECTION 6 — KNOWN FAILURE HISTORY & FRAGILE ZONES
════════════════════════════════════════════════════════════════

---

**INCIDENT 1: Prime Concordance Path Silently Rejected (Discovered 2026-04-12)**
- **System:** Infrastructure (OMNI ↔ Prime Buffer)
- **What Happened:** Every Prime concordance ever dispatched was rejected by OMNI with HTTP 403. The Prime execution path had never successfully executed a single trade since deployment.
- **Root Cause:** Prime Buffer sends `X-Nexus-Prime-Secret` header. OMNI's `/concordance` endpoint only read `X-Nexus-Secret` via FastAPI's `Header()` parameter. FastAPI received empty string, `verify_secret("")` failed.
- **Resolution Applied:** OMNI endpoint now accepts both `x_nexus_secret` and `x_nexus_prime_secret`. New regression test added.
- **Residual Risk:** None — both headers now validated. Test covers this path.
- **Auditor Flag:** Any new OMNI endpoint that handles both Alpha and Prime traffic must accept both header names.

---

**INCIDENT 2: OMNI Used Wrong Secret for Prime Execution Routing (Discovered 2026-04-12)**
- **System:** OMNI → Prime Execution
- **What Happened:** OMNI passed `nexus_secret` (= NEXUS_WEBHOOK_SECRET) for both Alpha and Prime execution routing. Prime Execution expects `NEXUS_PRIME_SECRET`. Works today because both secrets share the same value — latent failure on any secret rotation.
- **Root Cause:** Architectural error — OMNI config only loaded one webhook secret for both systems.
- **Resolution Applied:** OMNI now loads `NEXUS_PRIME_SECRET` separately; uses system-appropriate secret for execution routing.
- **Residual Risk:** Low — fixed. Risk reactivates if secrets are set to different values without ensuring OMNI env is updated.
- **Auditor Flag:** Any place OMNI calls a downstream service: verify the header name and secret value are system-appropriate.

---

**INCIDENT 3: DB-Before-Alpaca Phantom Positions (Discovered Pass A, 2026-04-12)**
- **System:** Alpha Execution, Prime Execution
- **What Happened:** Original code inserted position into DB before calling Alpaca. On Alpaca failure or timeout, DB had a position record with no corresponding Alpaca position.
- **Root Cause:** Wrong operation ordering — DB write was used as "intent" record, not confirmation record.
- **Resolution Applied:** Alpaca call first; DB write only on success. Tests assert 0 DB records on Alpaca failure.
- **Residual Risk:** None — fixed and tested.
- **Auditor Flag:** In any execution service, search for DB INSERT before Alpaca API call. Should not exist.

---

**INCIDENT 4: OMNI Synthesis Upsert Reset execution_dispatched Flag (Discovered Pass A, 2026-04-12)**
- **System:** OMNI
- **What Happened:** `save_synthesis_result()` used an upsert that reset `execution_dispatched=0` on re-synthesis of the same window/ticker. Could re-trigger execution on a position already opened.
- **Root Cause:** `ON CONFLICT DO UPDATE` included `execution_dispatched=0` in the SET clause.
- **Resolution Applied:** Upsert now uses `excluded.execution_dispatched` to preserve existing flag.
- **Residual Risk:** None — fixed.
- **Auditor Flag:** Check `ON CONFLICT DO UPDATE` clauses in synthesis_results table. `execution_dispatched` must never be overwritten to 0.

---

**INCIDENT 5: AILS Direction Column Missing from Bayesian Table (Discovered Pass A, 2026-04-12)**
- **System:** AILS
- **What Happened:** `bayesian_rates` PRIMARY KEY was `(ticker, strategy, regime)` — no direction. Bullish and bearish outcomes for the same setup were conflated. Win rate for "AAPL bull put spread in NORMAL" was polluted by bearish outcomes.
- **Root Cause:** Initial schema omitted `direction` dimension.
- **Resolution Applied:** `direction TEXT NOT NULL DEFAULT 'bullish'` added as 4th PK component. Migration via `ALTER TABLE ... ADD COLUMN` on startup (catches existing DBs).
- **Residual Risk:** Low. Existing records default to 'bullish' — minor distortion until enough live trades recalibrate.
- **Auditor Flag:** Any query against `bayesian_rates` must include `direction` in the WHERE clause.

---

**INCIDENT 6: OpenClaw Gateway Corruption (2026-04-10)**
- **System:** Infrastructure
- **What Happened:** Primus agent wrote invalid key `_heartbeat_paused` into `openclaw.json` agents array. Config validation failed → gateway went down for ~3.5 hours.
- **Root Cause:** Agent writing directly to the OpenClaw config file without going through the gateway API.
- **Resolution Applied:** Gateway recovered via `openclaw doctor --fix`. Config restored.
- **Residual Risk:** Medium — no enforcement preventing agents from writing to config file directly. `[AHMED TO CONFIRM: is openclaw.json now write-protected?]`
- **Auditor Flag:** Never write to `openclaw.json` programmatically. Use gateway API.

---

**INCIDENT 7: Circuit Breaker Missing Threading Lock (Discovered Pass A, 2026-04-12)**
- **System:** Alpha Buffer, Prime Buffer
- **What Happened:** Circuit breaker state (`failures`, `state`, `last_failure`) was read and written without synchronization in a multi-threaded FastAPI worker context.
- **Root Cause:** Circuit breaker class had no lock protecting compound read-modify-write operations.
- **Resolution Applied:** `threading.Lock()` added; all state changes under lock.
- **Residual Risk:** None.
- **Auditor Flag:** Any shared mutable state in FastAPI services must be protected by a threading lock.

---

**INCIDENT 8: ORACLE Unknown Platform Rate-Limit Bypass (Discovered Pass A, 2026-04-12)**
- **System:** ORACLE
- **What Happened:** Unknown platform names in the rate limiter fell through to an unthrottled path — any code that supplied an unknown platform name bypassed all rate limiting.
- **Root Cause:** Rate limiter only had buckets for known platforms; no default case.
- **Resolution Applied:** Unknown platforms now get conservative `_TokenBucket(requests_per_minute=120)` registered and reused.
- **Residual Risk:** None.
- **Auditor Flag:** Rate limiter must never have an unthrottled code path.

---

════════════════════════════════════════════════════════════════
## SECTION 7 — SECURITY THREAT MODEL
════════════════════════════════════════════════════════════════

### 7A. Credential & Secret Management

| Secret | Storage Location | Rotation Policy |
|--------|-----------------|-----------------|
| Webhook secrets (Nexus, Prime) | `.env` files, loaded at startup | `[AHMED TO CONFIRM: rotation frequency]` |
| Alpaca API key + secret | `.env` files | On compromise: immediate rotation via Alpaca dashboard |
| AI brain API keys | `.env` files | On compromise: immediate rotation via provider dashboards |
| Telegram bot token | `.env` files | On compromise: `/revoke` via @BotFather |
| ORACLE, AILS, OMNI, Guardian secrets | `.env` files | Internal — rotate manually |

**What must NEVER appear in logs, code, or Telegram:**
- Any value from `.env` files
- Alpaca order IDs in conjunction with position sizes (privacy)
- Raw API responses containing embedded keys

**`.env` files location:** `/Users/ahmedsadek/nexus/<service>/.env`
**`.env` files in `.gitignore`:** `[VERIFY: confirm .gitignore has *.env entry before any git push]`

---

### 7B. Authentication Surfaces

| Service | Auth Method | Header Name | Token Storage | Expiry/Refresh |
|---------|------------|-------------|---------------|----------------|
| Alpaca | API Key + Secret | `APCA-API-Key-ID` + `APCA-API-Secret-Key` | `.env` | No expiry; manual rotation |
| Telegram | Bot token in URL | URL path component | `.env` | No expiry; revocable via BotFather |
| Polygon.io | API Key query param | `apiKey` | `.env` | No expiry; manual rotation |
| ORATS | API Key | `[VERIFY: header or query param?]` | `.env` | `[VERIFY: expiry?]` |
| SpotGamma | `[VERIFY: auth method — official or reverse-engineered?]` | `[VERIFY]` | `.env` | `[VERIFY]` |
| Unusual Whales | API Key | `[VERIFY: header name]` | `.env` | `[VERIFY: expiry?]` |
| Market Chameleon | API Key (pending) | `[VERIFY]` | `.env` | `[VERIFY]` |
| Trading Economics | API Key (pending) | `[VERIFY]` | `.env` | `[VERIFY]` |
| FRED | API Key query param | `api_key` | `.env` | No expiry |
| Alpha Vantage | API Key query param | `apikey` | `.env` | No expiry |
| SEC EDGAR | User-Agent header (no key) | `User-Agent: NexusTradingSystem ahmedsadek@nexus.ai` | Hardcoded (safe — not a secret) | No expiry |

---

### 7C. Unauthenticated or Reverse-Engineered Surfaces

| Integration | Risk | Current Mitigation |
|-------------|------|-------------------|
| SpotGamma | If API is reverse-engineered (not official), schema can change without notice. Requests may violate ToS. | `[VERIFY: is SpotGamma access via official API or reverse-engineered endpoint?]` If reverse-engineered: wrap all calls in try/except, treat any non-200 as cache miss, monitor for schema drift |
| SEC EDGAR | Free public API — no rate limit auth. Must send valid User-Agent or requests are blocked. | User-Agent header set. Rate limit: 10 req/s respected via ORACLE rate limiter |

---

### 7D. Internal Trust Boundaries

- **Only Alpha Execution and Prime Execution can submit orders to Alpaca.** No other service has Alpaca credentials.
- **Only OMNI can trigger execution.** Agents submit to Buffers; Buffers route to OMNI; OMNI routes to Execution. No service bypasses this chain.
- **Telegram commands are validated by sender ID.** Only `8573754783` (Ahmed) can issue control commands. `[VERIFY: which services accept Telegram commands and where sender validation lives]`
- **OMNI verifies Axiom before executing.** Hard stop check is synchronous — if Axiom is unreachable, OMNI defaults to no-execution. `[VERIFY: confirm OMNI fails safe on Axiom timeout]`

---

════════════════════════════════════════════════════════════════
## SECTION 8 — ENVIRONMENT SPECIFICATIONS
════════════════════════════════════════════════════════════════

### 8A. Local Environment (Mac mini — production)

| Field | Value |
|-------|-------|
| Hardware | Mac mini (Apple Silicon, arm64) |
| OS | Darwin 25.3.0 (macOS Sequoia or later) |
| Python | 3.9.6 (each service has isolated `.venv`) |
| Node.js | v22.22.0 (OpenClaw runtime) |
| OpenClaw | Installed at `/opt/homebrew/lib/node_modules/openclaw/` |
| Service management | macOS launchd via LaunchAgent plists |
| Codebase root | `/Users/ahmedsadek/nexus/` |
| Virtual envs | `/Users/ahmedsadek/nexus/<service>/.venv/` |

**Key Python libraries (across services):**
- `fastapi` — HTTP server framework
- `uvicorn` — ASGI server
- `pydantic` — request/response validation
- `httpx` / `requests` — HTTP client
- `alpaca-trade-api` — Alpaca broker integration
- `anthropic` — Claude API client
- `openai` — OpenAI API client
- `google-generativeai` — Gemini API client
- `sqlite3` — built-in (no external ORM)
- `[AHMED TO CONFIRM: exact library versions from each service's requirements.txt]`

**LaunchAgent behavior:**
- `KeepAlive = true` on all 9 services
- Crash → launchd restarts within seconds
- Logs to `stderr.log` in service log directory
- Guardian Angel (8009) monitors all 8 other services and can trigger restart + Telegram alert

---

### 8B. No Railway Environment

**This system is fully local.** There is no Railway deployment. All previous Railway services have been superseded by the local Mac mini deployment.

If you encounter Railway configuration in the codebase (old plist files, `railway.json`, Railway-specific env vars), treat it as dead code from the prior architecture.

---

### 8C. Environment Parity Gaps

| Gap | Risk |
|-----|------|
| Mac mini has UPS — Railway would not | Local is MORE resilient than cloud for power events |
| Python 3.9.6 on Mac mini — newer Python on any future cloud deployment | Python 3.9 does NOT support `dict \| list` union syntax in type hints. Use `Union[dict, list]` or `Any`. |
| All services on loopback — no TLS between services | Acceptable for local; must add TLS if any service moves to cloud or external network |
| SQLite files on local disk — no replication | DB loss = trade history loss. `[AHMED TO CONFIRM: backup strategy for SQLite files?]` |

---

════════════════════════════════════════════════════════════════
## SECTION 9 — CONCURRENCY & ASYNC MODEL
════════════════════════════════════════════════════════════════

**Framework:** FastAPI + Uvicorn (ASGI). All services use synchronous (non-async) route handlers with threading for background tasks. FastAPI runs in multi-threaded mode.

**Shared state per service:**

| Service | Shared State | Lock Used |
|---------|-------------|-----------|
| Axiom | `app_state` (pool, regime, window_id) | `_state_lock: threading.Lock()` |
| Alpha Buffer | Circuit breaker state | `threading.Lock()` in circuit breaker class |
| Prime Buffer | Circuit breaker state | `threading.Lock()` in circuit breaker class |
| OMNI | `app_state` (syntheses_today, go_verdicts_today) | `_state_lock: threading.Lock()` — added Pass B fix (V6) |
| Alpha Execution | Position limit check + `app_state` | `_execute_lock` (position limit) + `_state_lock` (dict mutations) |
| Prime Execution | Position limit check + `app_state` | `_execute_lock` + `_state_lock` |
| AILS | SQLite write operations | `_db_lock: threading.Lock()` |

**Exit monitor concurrency:**
- Alpha Execution: background thread polls for exit conditions at 15-minute intervals
- Prime Execution: background thread polls for exit conditions at 15-minute intervals
- **KNOWN CONCERN:** 15-minute interval may be too slow for volatile intraday moves on Prime equity positions. `[AHMED TO CONFIRM: change to 1-minute interval? This was flagged as CRITICAL by o3-mini in Pass A but deferred pending Ahmed's decision.]`

**OMNI Quad Intelligence parallelism:**
All 4 AI brains run concurrently. Results collected and voted. Confirmed brain timeout/error behavior (from `synthesis.py`):
- A brain that returns an error is excluded from `brains_responded` count — does NOT count as a NO_GO vote
- Threshold stays at **3 absolute GO votes** (not 3/remaining) — so a timed-out brain makes GO harder to achieve, not easier
- `MIN_BRAINS_REQUIRED = 3` — if fewer than 3 brains respond, verdict is `CONDITIONAL` (not NO_GO), Ahmed alerted
- `VOTES_REQUIRED_GO = 3`, `VOTES_REQUIRED_STRONG_GO = 4`
- P3/P4 require `P3_P4_MIN_VOTES_GO = 3` of 4 (not of responded)
- `[AHMED TO CONFIRM: exact per-brain HTTP timeout in quad_intelligence.py]`

**Race condition history:**
- Alpha Buffer circuit breaker — missing lock (fixed 2026-04-12)
- Prime Buffer circuit breaker — missing lock (fixed 2026-04-12)
- Axiom `/pool` endpoint — `app_state` snapshot needed under lock (fixed 2026-04-12)

---

════════════════════════════════════════════════════════════════
## SECTION 10 — AUDIT USAGE INSTRUCTIONS
════════════════════════════════════════════════════════════════

**This document is MANDATORY READING before auditing any Nexus component.**

### For All Auditors:

1. **Read Section 4 (Invariants) first.** Every finding must be cross-referenced against the invariant list. If code violates an invariant, that is automatically CRITICAL.

2. **Read Section 6 (Failure History) before reviewing any component.** Prior incidents define known-fragile zones. Code in those zones requires extra scrutiny.

3. **All 12 error categories are mandatory.** For every file reviewed, explicitly report for each:
   - Syntax errors
   - Runtime errors
   - Logic errors
   - Semantic errors
   - Type errors
   - Concurrency errors
   - Resource management errors
   - Config/environment errors
   - Integration errors (cross-service contracts)
   - Off-by-one errors
   - Security errors
   - Invariant violations

4. **"No issues found" must be stated explicitly** for any category that is clean.

5. **Severity definitions:**
   - `CRITICAL` — could cause financial loss, phantom positions, duplicate orders, or system halt
   - `HIGH` — could cause incorrect execution (wrong size, wrong contract, wrong direction)
   - `MEDIUM` — degraded reliability, partial signal loss, error handling gap
   - `LOW` — code quality, maintainability, documentation

6. **Cross-service auditors:** Focus on Section 3 (Architecture) and Section 5 (Data Flow Traces). The most dangerous bugs in this system are at service boundaries — wrong header names, mismatched field names, wrong secret used for wrong system.

7. **Regression focus:** Every fix from Section 6 must be verified as still present in the code being audited. Prior incidents are the most likely classes of bugs to recur.

8. **Unresolved items:** See `VERIFY` and `AHMED TO CONFIRM` tags in this document. These represent known uncertainty — treat any code touching these areas with heightened scrutiny.

---

**INCIDENT 9: Axiom Timeout Was Fail-Open — Trades Executed Without Regime Check (Discovered 2026-04-12, Pass B)**
- **System:** OMNI → Axiom
- **What Happened:** When Axiom `/assess` timed out, `axiom_client.py` returned `{"hard_stops": [], "sizing_mult": 1.0, "error": "timeout"}`. In `synthesis.py`, an empty `hard_stops` list means `axiom_blocked=False`. GO/STRONG_GO verdicts proceeded to execution with no regime validation, no hard stops, and full position sizing.
- **Root Cause:** Error return was designed for resilience (don't block on Axiom flakiness) but produced the opposite of the intended safety behavior.
- **Resolution Applied:** On timeout or any exception, `axiom_client.py` now returns `hard_stops=["AXIOM_UNREACHABLE"]` and `sizing_mult=0.0`. This guarantees `axiom_blocked=True` in synthesis. New test: `test_axiom_unreachable_blocks_execution`.
- **Residual Risk:** None — fail-safe is now enforced and tested. If Axiom is down, no new trades open.
- **Auditor Flag:** In any version of `axiom_client.py`, error/timeout return must include a non-empty `hard_stops` list. An empty `hard_stops=[]` on error is a silent fail-open.

---

**INCIDENT 10: OMNI `app_state` Counters Unprotected in Multi-Threaded Context (Discovered 2026-04-12, Pass B)**
- **System:** OMNI
- **What Happened:** `app_state["syntheses_today"] += 1` and `app_state["go_verdicts_today"] += 1` executed without any threading lock. In multi-threaded Uvicorn, concurrent concordances could race on these increments — classic read-modify-write race condition.
- **Root Cause:** `_state_lock` was added to Axiom in Pass A but not to OMNI. Same class of bug, different service.
- **Resolution Applied:** `import threading; _state_lock = threading.Lock()` added to OMNI `main.py`. Both counter increments now wrapped in `with _state_lock:`.
- **Residual Risk:** None — lock is in place.
- **Auditor Flag:** Any shared mutable state in FastAPI services must be locked. Check every `app_state[key] += 1` or `app_state[key] = value` across all services.

---

**INCIDENT 11: Live Bot Token Published in Audit Document (Discovered 2026-04-12 via Cipher review)**
- **System:** Infrastructure / Security
- **What Happened:** `NEXUS_AUDIT_CONTEXT.md` v1.0 was published containing the live Telegram bot token in plaintext in Section 3C. The document was immediately sent as a PDF to an external LLM auditor (Claude) and to Ahmed's Telegram.
- **Root Cause:** GENESIS violated INV-8 (no credentials in documentation) when populating the Telegram notification section. This was the exact invariant the section was documenting.
- **Resolution Applied:** Token removed from document. PDF containing the token should be considered compromised — Ahmed to revoke bot token via @BotFather and issue new token. All service `.env` files must be updated with new token before next restart.
- **Residual Risk:** HIGH until token is revoked. The token was sent to at least one external LLM context window (Claude). Revocation is urgent.
- **Auditor Flag:** Any document, spec, log, or comment containing a credential value (not name) is an INV-8 violation. `[AHMED TO CONFIRM: has bot token been revoked and reissued?]`

---

*NEXUS_AUDIT_CONTEXT.md — v1.1 — Updated 2026-04-12 by GENESIS*
*Maintain this document as architecture evolves. Update CHANGELOG on every revision.*
