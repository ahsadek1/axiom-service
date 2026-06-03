
## [2026-04-23] guardian-angel SQLite deadlock + buffer retry fixes — GENESIS

### Guardian Angel SQLite Deadlock (hourly DB recreation)
- **File:** `guardian-angel/guardian_angel_v4.py`
- **Bug:** `_backup_dbs()` acquired `_BACKUP_LOCK` (pausing TelemetryWriter) then released it on exit. `prune_old()` ran immediately after — TelemetryWriter resumed and grabbed the SQLite connection first — `prune_old()` hit "database table is locked" every hour, recreating `telemetry.db` and losing all telemetry history.
- **Fix:** `prune_old()` now runs inside a fresh `_BACKUP_LOCK` acquisition, forcing TelemetryWriter to yield during the prune. One lock release per cycle.
- **Rollback:** Remove `with _BACKUP_LOCK:` wrapper around `self._collector.prune_old()`

### Concordance window fix — verified deployed
- Cipher reported "not deployed" but the GENESIS concordance-window-fix IS live in alpha-buffer + cipher/atlas/sage. Agents pass `window_id` from Axiom pool push; buffer uses agent's window as DB key. Verified: AAPL P1 concordance (3 agents, same window `2026-04-23-0815`) formed correctly today.

### Watchdog bash compatibility — verified deployed
- Cipher fix already deployed: `declare -A` replaced with `get_url()` function. Watchdog running PID confirmed.

### ATM win rate + QI gate (ATG Intraday) — SQS domain
- Out of GENESIS scope. These are ATM/ATG services in `/Users/ahmedsadek/sqs/`. GENESIS owns Nexus V2 only.

---

## [2026-04-23] alpha-buffer + pipeline-sentinel — GENESIS (two critical fixes)

### Root Cause 1: Concordance 422 Unprocessable Entity (Execution Failure)
- **File:** `alpha-buffer/main.py`, `prime-buffer/main.py`
- **Bug 1:** Retry loop built concordance dict without `system`, `echo_chamber`, `notes` fields. OMNI's `ConcordancePayload` requires `system` (no default) → every retry returned 422. Concordances timed out on first attempt were never retried successfully.
- **Bug 2:** `_dispatch_background()` always called `mark_omni_dispatched()` regardless of ok/fail — silently marking failed dispatches as `omni_dispatched=1`, permanently bypassing the retry loop.
- **Fix:** Retry dict now includes all required fields (`system="alpha"`, `echo_chamber=False`, `notes=[]`). `_dispatch_background()` only marks dispatched on confirmed success; failures stay `omni_dispatched=0` for retry.
- **Impact:** Every concordance that timed out (Claude AI latency) was permanently dropped. With fix, they will retry successfully.

### Root Cause 2: AGENT_SILENCE false positive at market open
- **File:** `pipeline-sentinel/classifier.py`
- **Bug:** `_detect_agent_silent()` fired every day at exactly 9:30 AM ET — agents haven't processed any Axiom pool picks in the first 15 minutes of market open, so the 15-min window is empty. This was a guaranteed daily false positive.
- **Fix:** Added 30-minute grace period after market open (09:30–10:00 ET). AGENT_SILENT suppressed during this window with debug log.
- **Impact:** Eliminates the recurring false-positive alert and removes the daily noise from the integrity dashboard.

### Tests
- All 11 services verified healthy after restart
- Alpha Buffer / Prime Buffer restarted clean
- Pipeline Sentinel restarted clean

### Rollback
- alpha-buffer: `git revert` or restore `system`/`echo_chamber`/`notes` field block
- pipeline-sentinel: remove grace period block in `_detect_agent_silent()`

---

## [2026-04-13] pipeline-sentinel v1.0 — GENESIS

### What changed
- New service: `pipeline-sentinel` on port 8010
- 4 components: Pipeline Trace Ledger, System Health Scorer, Failure Mode Classifier, Agent Context Injector
- 12/12 tests passing
- launchd registered: ai.nexus.pipeline-sentinel
- shared/pipeline_client.py — fire-and-forget trace helper ready for adoption in all 9 services

### Why it changed
Axiom and Nexus Alpha identified the core architectural gap: HTTP 200 from an agent ≠ pick delivered.
System had no end-to-end pipeline visibility, no failure classification, no health scoring.

### Tests added
12 test cases: happy path, stall detection, score computation, health halt, score recovery,
EXECUTOR_SLOW, AGENT_SILENT, DB integrity, concurrent writes, context block format,
Telegram dedup, score history pruning.

### Next: Service adoption
Add trace_hop() calls to all 9 services via shared/pipeline_client.py.
Start with Axiom (axiom_push) then agent services.

### Rollback
launchctl unload ~/Library/LaunchAgents/ai.nexus.pipeline-sentinel.plist

## [2026-04-30] nexus-integrity — v1.0.0
**What:** Commercial-grade monitoring service (nexus-integrity) built and deployed
**Design basis:** Vector COMMERCIAL_MONITORING_SYSTEM v1 + Vector Rebuttal (8 amendments) + Vector Final Rebuttal (6 amendments V9-V14)
**Key capabilities:**
- TRS (Trading Readiness Score) — fail-closed, WAL SQLite, score=0 on any failure
- 5-stage pipeline flow verifier — tests actual data flow, not process liveness
- Options-specific execution probe — 6-step SPY ATM 30-DTE OCC roundtrip (V12)
- Composite health score — 7 components, weighted, regime-aware thresholds from CHRONICLE
- P0-P4 alert tiering — derived from TRS sub-component weights, no manual classification
- CHRONICLE as unified state store — no 4th database created
- Schema version gate — hard stop on CHRONICLE schema mismatch (V8/V10)
- Canary with guaranteed CANARY_ prefix cleanup (V13)
**Tests:** 18/18 passing (15 spec-required + 3 bonus)
**Port:** 8012 (8011 occupied by discord-bridge — resolve before loading LaunchAgent)
**Files:** /Users/ahmedsadek/nexus/nexus-integrity/
**LaunchAgent:** ai.nexus.nexus-integrity.plist (ready to load after port resolved)
**Rollback:** Remove /Users/ahmedsadek/nexus/nexus-integrity/, unload plist
