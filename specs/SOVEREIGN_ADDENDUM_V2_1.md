# SOVEREIGN ADDENDUM — nexus-integrity v2.1
**Author:** SOVEREIGN 👁️
**Date:** 2026-04-30
**Base Spec:** `commercial-monitoring-system-v2.md` (v2.1, approved 2026-04-29)
**Status:** REQUIRED — incorporate into build before implementation begins
**Authority:** Ahmed Sadek

---

## Why This Addendum Exists

The v2.1 spec is architecturally sound. Every amendment from V1–V14 is correct and should be
built as written. This addendum covers failure modes that v2.1 still cannot detect — derived
directly from failures observed on 2026-04-29 that *every existing and proposed monitor would
have missed*. Each item here is either a gap in detection logic, an edge case in the recovery
path, or a hidden error class that v2.1 leaves unguarded.

These are not polish. They are **the difference between a system that catches silent failures
and one that doesn't.**

---

## Amendment S1 — Oracle Scoring Quality Probe (Tier 2)

### The Gap
v2.1's Layer 2 Stage 2 checks: *"agent decisions created_at < 20 min ago."*
This does not detect the failure that killed today's pipeline for 2+ hours.

**What actually happened:** Oracle engines were executing correctly — no crash, no HTTP
failure, /health returned 200, cache_warm_tickers=105. The engines were calling nonexistent
function names, catching AttributeError silently, and returning `null` for flow and gamma
data. The scorer received these nulls, applied hardcoded fallbacks (D2=7, D6=4), computed
mathematically valid scores of 43–53, and produced records in the decisions table —
*exactly what Stage 2 checks for*. Stage 2 showed green. The pipeline was dead.

**The invisible failure mode:** Service healthy + data flowing + decisions created + scores
below threshold. No detection in any layer of v2.1.

### Required: Scoring Quality Probe (every 30 min, market hours — Tier 2)

```python
class ScoringQualityProbe:
    """
    Detects Oracle returning fallback/null data for key scoring dimensions.
    Runs every 30 min. No external API calls. Uses cached Oracle data only.
    """

    # Fallback values defined in shared/scorer.py — null-return sentinels
    FALLBACK_SENTINELS = {
        "D2_options_flow":        7,   # returned when flow.put_call_ratio is None
        "D3_technical_setup":     7,   # returned when all price deriveds are None
        "D5_fundamental_quality": 7,   # returned when all fundamental data is None/NEUTRAL
        "D6_gamma_safety":        4,   # returned when gamma.net_gex is None/NEUTRAL
    }
    SENTINEL_THRESHOLD = 2  # If ≥2 dimensions hit sentinel on same ticker → degraded

    PROBE_TICKERS = ["SPY", "QQQ", "AAPL"]  # Liquid — always available

    def run(self) -> ScoringQualityResult:
        degraded_count = 0
        findings = []

        for ticker in self.PROBE_TICKERS:
            context = oracle_client.get_context(ticker, auth=PROBE_SECRET)
            if context is None:
                findings.append(f"{ticker}: Oracle unreachable")
                degraded_count += 3  # Counts as full degradation
                continue

            direction = determine_direction(context)
            result = compute_score(context, direction)

            sentinel_hits = sum(
                1 for dim, sentinel in self.FALLBACK_SENTINELS.items()
                if result.breakdown.get(dim) == sentinel
            )

            if sentinel_hits >= self.SENTINEL_THRESHOLD:
                degraded_count += 1
                findings.append(
                    f"{ticker}: {sentinel_hits}/4 dimensions at fallback sentinel "
                    f"(D2={result.breakdown['D2_options_flow']} "
                    f"D3={result.breakdown['D3_technical_setup']} "
                    f"D5={result.breakdown['D5_fundamental_quality']} "
                    f"D6={result.breakdown['D6_gamma_safety']})"
                )

        quality = "DEGRADED" if degraded_count >= 2 else "PARTIAL" if degraded_count == 1 else "GOOD"

        # Write to CHRONICLE
        chronicle.write("scoring_quality_probe", {
            "quality": quality,
            "degraded_tickers": degraded_count,
            "findings": findings,
            "ts": utcnow(),
        })

        if quality == "DEGRADED":
            # TRS impact: scoring_quality sub-score → 0
            # Blocks new submissions until quality recovers
            return ScoringQualityResult(quality=quality, trs_score=0, findings=findings)

        return ScoringQualityResult(quality=quality, trs_score=100, findings=findings)
```

**TRS Integration:** `scoring_quality` becomes a new TRS sub-score component (weight 10%,
TTL 35 min). If quality=DEGRADED → sub-score=0 → TRS drops by ≥10 points → AMBER or RED
depending on other components. Zero-submission pipelines become impossible to miss.

**Would have caught today's failure:** SPY context at 9:00 AM would have returned D2=7,
D6=4 (fallback sentinels) → quality=DEGRADED → TRS drop → P1 alert within 30 seconds of
Oracle restart — not 2 hours later.

---

## Amendment S2 — Service Restart Cluster Detector

### The Gap
v2.1 monitors individual service health and logs individual restarts. It does not detect
*clusters of restarts* — multiple services restarting in a short window — which is a
qualitatively different failure mode from any single restart.

**What actually happened today:**
- Axiom: restarted 08:55 AM
- Prime: restarted 08:55 AM
- Alpha: restarted 09:50 AM, 10:58 AM, 12:22 PM
- OMNI: restarted 09:54 AM, 10:22 AM, 13:21 PM, 16:06 PM (5 total)
- Sage: cycling every ~5 minutes all day

No existing monitor — v2.1 included — raised a cluster flag. Each restart was evaluated
in isolation. The pattern that every experienced operations engineer would notice immediately
("something is thrashing the process manager") was completely invisible.

### Required: Restart Cluster Detector

```python
class RestartClusterDetector:
    CLUSTER_WINDOW_MINUTES = 20
    CLUSTER_THRESHOLD = 3       # ≥3 distinct services restarting = cluster event
    SINGLE_SERVICE_THRESHOLD = 3  # same service restarting ≥3 times in window = P1

    def check(self) -> Optional[ClusterAlert]:
        window_start = utcnow() - timedelta(minutes=self.CLUSTER_WINDOW_MINUTES)

        # Query restart events from CHRONICLE health_events
        recent_restarts = chronicle.query(
            "SELECT service, COUNT(*) as count, MIN(ts) as first, MAX(ts) as last "
            "FROM health_events "
            "WHERE check_type='service_restart' AND ts >= ? "
            "GROUP BY service",
            (window_start.timestamp(),)
        )

        distinct_services_restarted = len(recent_restarts)
        total_restart_events = sum(r["count"] for r in recent_restarts)

        # Single service thrashing
        for row in recent_restarts:
            if row["count"] >= self.SINGLE_SERVICE_THRESHOLD:
                return ClusterAlert(
                    kind="SINGLE_SERVICE_THRASH",
                    severity="P1",
                    message=f"{row['service']} restarted {row['count']}x in "
                            f"{self.CLUSTER_WINDOW_MINUTES} min. Root cause unknown. "
                            f"First={row['first']}, Last={row['last']}",
                    services=[row["service"]],
                    restart_count=row["count"],
                )

        # Multi-service cluster
        if distinct_services_restarted >= self.CLUSTER_THRESHOLD:
            names = [r["service"] for r in recent_restarts]
            return ClusterAlert(
                kind="MULTI_SERVICE_CLUSTER",
                severity="P0",
                message=f"CLUSTER: {distinct_services_restarted} services restarted "
                        f"({total_restart_events} total events) in "
                        f"{self.CLUSTER_WINDOW_MINUTES} min. "
                        f"Services: {', '.join(names)}. "
                        f"Likely: OS memory pressure, shared dependency failure, "
                        f"or process manager issue.",
                services=names,
                restart_count=total_restart_events,
            )

        return None
```

**TRS Integration:** Cluster event → `service_stability` sub-score drops proportionally.
Multi-service cluster (P0 kind) → binary floor blocker → TRS=0.

**Auto-diagnosis on cluster:** When cluster detected, immediately check:
1. `vm_stat` — is the system under memory pressure?
2. `launchctl list | grep nexus` — any services in restart backoff?
3. Shared dependency health (CHRONICLE DB, message bus, network interface)
4. Write diagnosis to CHRONICLE with evidence, then escalate

---

## Amendment S3 — Post-Restart Cache Quality Gate (Oracle)

### The Gap
When Oracle restarts, it rebuilds its in-memory cache by serving the first requests live —
which is correct. But v2.1 has no gate that prevents Oracle from serving *previously-cached
null values* in the first minutes after a restart when the cache is repopulated from a
prior corrupted state.

**The failure mode:** Oracle restarts → cache warms from fresh engine calls → engines are
broken → cache now contains null flow + null gamma for 105 tickers → cache_hit_rate climbs
to 63% → Oracle reports `status: healthy` with high hit rate — but every cached entry
carries the broken data. The cache hit rate is the *rate at which broken data is being
served quickly*.

### Required: Cache Quality Assertion (on Oracle startup + every 15 min)

```python
class OracleCacheQualityGate:
    """
    Runs once after Oracle startup (after 60s warmup period) and every 15 min.
    Checks whether cached data is populated vs null across sentinel dimensions.
    Requires Oracle context endpoint access (uses PROBE_SECRET).
    """

    SENTINEL_TICKERS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
    NULL_FLOW_THRESHOLD = 0.6   # If >60% of probed tickers have null flow → dirty cache
    NULL_GAMMA_THRESHOLD = 0.6  # Same for gamma

    def run_post_restart_check(self, wait_seconds: int = 60) -> CacheQualityResult:
        """Called after Oracle startup. Waits for cache to warm, then checks quality."""
        time.sleep(wait_seconds)  # Allow minimum warm-up

        null_flow_count = 0
        null_gamma_count = 0
        checked = 0

        for ticker in self.SENTINEL_TICKERS:
            ctx = oracle_client.get_context(ticker, auth=PROBE_SECRET, timeout=10)
            if ctx is None:
                continue
            checked += 1
            if ctx.get("flow") is None or ctx["flow"].get("put_call_ratio") is None:
                null_flow_count += 1
            if ctx.get("gamma") is None or ctx["gamma"].get("net_gex") is None:
                null_gamma_count += 1

        if checked == 0:
            return CacheQualityResult(quality="UNKNOWN", trs_score=0)

        flow_null_rate = null_flow_count / checked
        gamma_null_rate = null_gamma_count / checked

        if flow_null_rate > self.NULL_FLOW_THRESHOLD or gamma_null_rate > self.NULL_GAMMA_THRESHOLD:
            # Cache is dirty — force eviction of null entries
            self._evict_null_cache_entries()
            chronicle.write("cache_quality_gate", {
                "action": "EVICTED_NULL_ENTRIES",
                "flow_null_rate": flow_null_rate,
                "gamma_null_rate": gamma_null_rate,
                "checked_tickers": checked,
            })
            # Re-check after eviction
            return self.run_post_restart_check(wait_seconds=30)

        return CacheQualityResult(
            quality="GOOD",
            trs_score=100,
            flow_null_rate=flow_null_rate,
            gamma_null_rate=gamma_null_rate,
        )

    def _evict_null_cache_entries(self):
        """Remove cache entries where flow=null AND gamma=null simultaneously.
        These are certain engine failures, not legitimate null data."""
        # Oracle exposes DELETE /cache/evict-nulls endpoint (add this endpoint)
        oracle_client.post("/oracle/cache/evict-nulls", auth=PROBE_SECRET)
```

**Required Oracle endpoint (add to Oracle's main.py):**
```python
@app.delete("/oracle/cache/evict-nulls")
async def evict_null_cache_entries(x_oracle_secret: str = Header(...)):
    _verify_secret(x_oracle_secret)
    evicted = cache.evict_where(lambda entry: entry.flow is None and entry.gamma is None)
    return {"evicted": evicted, "reason": "null_flow_and_gamma"}
```

---

## Amendment S4 — Scanner Window Completion Rate

### The Gap
v2.1 Stage 2 checks: *"agent decisions created_at < 20 min ago."*
This is a recency check. It tells you whether the last decision was recent.
It does not tell you whether the agent is *completing its windows reliably*.

**What happened today:** Sage restarted every ~5 minutes. A 15-minute window would arrive
while Sage was restarting, get recorded as received (window_id written to DB), but
`analyzed=0` and `completed_at=null` — because Sage was down during the analysis phase.
The next window arrives 15 minutes later, Sage happens to be up, `analyzed=20, submitted=3`.
The Stage 2 check sees a recent decision and reports green — even though 50% of windows
were silently missed.

### Required: Window Completion Rate Metric

```python
class WindowCompletionRateMonitor:
    LOOKBACK_WINDOWS = 4   # Check last 4 windows per agent
    MIN_COMPLETION_RATE = 0.75  # ≥3/4 windows must complete with analyzed > 0

    AGENTS = [
        ("cipher", "/Users/ahmedsadek/nexus/data/cipher.db"),
        ("atlas",  "/Users/ahmedsadek/nexus/data/atlas.db"),
        ("sage",   "/Users/ahmedsadek/nexus/data/sage.db"),
    ]

    def check(self) -> List[WindowCompletionAlert]:
        alerts = []

        for agent_name, db_path in self.AGENTS:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()

            # Get last N windows
            cur.execute(
                "SELECT window_id, tickers_analyzed, tickers_submitted, completed_at "
                "FROM windows ORDER BY rowid DESC LIMIT ?",
                (self.LOOKBACK_WINDOWS,)
            )
            windows = cur.fetchall()
            conn.close()

            if len(windows) < 2:
                continue  # Not enough history yet

            completed = sum(
                1 for w in windows
                if w[1] > 0  # tickers_analyzed > 0
                and w[3] is not None  # completed_at is not null
            )
            rate = completed / len(windows)

            # Also check for persistent incomplete windows (open > 10 min)
            stale_open = [
                w for w in windows
                if w[3] is None and w[1] == 0  # Never analyzed, never completed
            ]

            if rate < self.MIN_COMPLETION_RATE:
                alerts.append(WindowCompletionAlert(
                    agent=agent_name,
                    severity="P1",
                    completion_rate=rate,
                    windows_checked=len(windows),
                    windows_completed=completed,
                    message=f"{agent_name} window completion rate {rate:.0%} "
                            f"({completed}/{len(windows)} last windows). "
                            f"Service may be cycling during analysis."
                ))

            if len(stale_open) >= 2:
                alerts.append(WindowCompletionAlert(
                    agent=agent_name,
                    severity="P1",
                    message=f"{agent_name} has {len(stale_open)} windows with "
                            f"analyzed=0 and no completion. Cycling confirmed."
                ))

        return alerts
```

**Run frequency:** Every 20 minutes during market hours (aligned with window cadence).
**TRS impact:** If any agent window completion rate < 75% → `agent_reliability` sub-score
drops to 50. If < 50% → drops to 0.

**Would have caught today's pattern:** By the third window showing analyzed=0, this
monitor would have fired P1 — identifying Sage cycling before the fourth hour of missed
windows.

---

## Amendment S5 — Brain API Pre-Market Connectivity Test (Stage 0)

### The Gap
v2.1's Alpaca options probe (08:00) tests execution broker connectivity.
No stage tests the AI brain APIs (Claude, Gemini, DeepSeek, o3mini) before market open.

**What happened today:** Gemini was returning 503 on every synthesis all afternoon.
Claude was intermittently timing out. The OMNI health endpoint said `status: healthy`.
Syntheses ran but completed with only 2/4 brains — systematically producing CONDITIONAL
verdicts (counted as NO_GO) instead of clean evaluations. Every synthesis today was
operating at half capacity. No pre-market check caught this.

### Required: Brain API Connectivity Stage (add as Stage 0.5 in pre-market pipeline)

```python
class BrainConnectivityProbe:
    """
    Pre-market only. Tests all four synthesis brains with a minimal API call.
    Minimum call: single-token completion to verify auth + connectivity.
    Does NOT use trading API quota — uses PROBE_ scoped keys.
    Run at 08:45 ET (after Alpaca probe, before TRS compute).
    """

    BRAINS = {
        "claude": {
            "client_fn": lambda: anthropic.Anthropic(api_key=CLAUDE_PROBE_KEY),
            "test_fn":   lambda c: c.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1,
                messages=[{"role": "user", "content": "1"}]
            ),
        },
        "gemini": {
            "client_fn": lambda: genai.GenerativeModel("gemini-2.5-flash"),
            "test_fn":   lambda c: c.generate_content("1", generation_config={"max_output_tokens": 1}),
        },
        "deepseek": {
            "client_fn": lambda: openai.OpenAI(api_key=DEEPSEEK_PROBE_KEY,
                                                base_url="https://api.deepseek.com/v1"),
            "test_fn":   lambda c: c.chat.completions.create(
                model="deepseek-chat", max_tokens=1,
                messages=[{"role": "user", "content": "1"}]
            ),
        },
        "o3mini": {
            "client_fn": lambda: openai.OpenAI(api_key=OPENAI_PROBE_KEY),
            "test_fn":   lambda c: c.chat.completions.create(
                model="o3-mini", max_tokens=1,
                messages=[{"role": "user", "content": "1"}]
            ),
        },
    }

    TIMEOUT_SECONDS = 10
    MIN_BRAINS_REQUIRED = 3  # <3 available → TRS degraded; <2 → TRS=0

    def run(self) -> BrainConnectivityResult:
        available = []
        failed = {}

        for brain_name, brain_config in self.BRAINS.items():
            try:
                client = brain_config["client_fn"]()
                with timeout(self.TIMEOUT_SECONDS):
                    brain_config["test_fn"](client)
                available.append(brain_name)
            except Exception as e:
                failed[brain_name] = str(e)[:120]

        result = BrainConnectivityResult(
            available=available,
            failed=failed,
            available_count=len(available),
            ts=utcnow(),
        )

        chronicle.write("brain_connectivity_probe", result.to_dict())

        if len(available) < 2:
            # Cannot synthesize with <2 brains — hard block
            return result._replace(trs_score=0)
        if len(available) < self.MIN_BRAINS_REQUIRED:
            # Degraded synthesis quality — AMBER
            return result._replace(trs_score=50)

        return result._replace(trs_score=100)
```

**Pre-market pipeline change:** Insert between Stage 08:15 (concordance drill) and
Stage 08:30 (TRS compute). Failures write to CHRONICLE and feed into TRS.

**Intraday recheck:** Run every 30 min as Tier 2 probe (no external API calls for
already-available brains — use a cached TTL=25min result; only re-probe if TTL expired
or a synthesis recently returned a brain error).

**TRS sub-score:** `brain_capacity` (TTL 35 min). 4/4 brains → 100. 3/4 → 75.
2/4 → 50. <2 → 0 (binary floor blocker).

---

## Amendment S6 — Clearance Certificate Invalidation on Post-Clearance Restart

### The Gap
The 8:30 AM clearance produces a pass/fail result for that moment in time. Once cleared,
a service is considered cleared for the trading day — even if it restarts afterward.

**What happened today:** The 8:30 clearance passed (Oracle was the only failure). Axiom
restarted at 08:55 AM — 25 minutes after clearance. The clearance certificate was already
written. The CANARY fired at 09:00 AM into an Axiom that had been up for 4 minutes and
hadn't finished warming up. AXIOM_UNREACHABLE was the result.

The clearance was valid at 08:30. It was invalid at 09:00. Nothing knew the difference.

### Required: Clearance Certificate Invalidation

```python
class ClearanceCertificateManager:
    """
    The clearance certificate is a live claim, not a historical fact.
    Any service restart after clearance invalidates it for that service.
    The certificate must be re-validated before it can be re-used.
    """

    REVALIDATION_STABILITY_WINDOW_MINUTES = 5
    # A restarted service must be stable for 5 min before it re-clears

    def watch_post_clearance_restarts(self):
        """Background daemon — runs from clearance time until market close."""
        while market_is_open():
            for service in CLEARED_SERVICES:
                current_uptime = get_service_uptime_minutes(service)

                if current_uptime < self.REVALIDATION_STABILITY_WINDOW_MINUTES:
                    # This service restarted after clearance
                    self._invalidate_clearance(service)
                    chronicle.write("clearance_invalidated", {
                        "service": service,
                        "uptime_minutes": current_uptime,
                        "reason": "post_clearance_restart",
                    })

                    # Block CANARY and new trade entries until re-validated
                    trs_client.set_floor_blocker(
                        f"clearance_invalid_{service}",
                        reason=f"{service} restarted post-clearance, "
                               f"stability window not met ({current_uptime:.1f}min < "
                               f"{self.REVALIDATION_STABILITY_WINDOW_MINUTES}min)",
                        expires_in_minutes=self.REVALIDATION_STABILITY_WINDOW_MINUTES,
                    )

            time.sleep(30)

    def _invalidate_clearance(self, service: str):
        # Write to CHRONICLE preflight_manifests as amendment
        chronicle.write("clearance_amendment", {
            "service": service,
            "status": "INVALIDATED",
            "reason": "post_clearance_restart",
            "ts": utcnow(),
        })
```

**CANARY timing fix:** The CANARY runner must check `clearance_certificate.is_valid()`
before firing. If any critical service restarted in the last 5 minutes, the CANARY
defers 3 minutes and retries once. This eliminates the race condition observed today.

---

## Amendment S7 — Cross-Agent Ticker Overlap Rate

### The Gap
v2.1's pipeline flow verifier checks that submissions reach the buffer and that the buffer
is healthy. It does not check whether the *submissions from different agents actually
overlap on the same tickers* — which is required for concordance formation.

**Why it matters:** If Sage submits [GE, PKG, PEP] and Atlas submits [MSFT, AMZN, NVDA]
and Cipher submits [JPM, BAC, GS], the alpha-buffer has nine submissions and zero
concordances — because no two agents submitted the same ticker. The flow verifier shows
green at every stage. The TRS shows green. GO verdicts: zero.

This is not a system failure — it's a signal quality condition that looks like one.
But it needs to be visible and distinguishable from a real pipeline failure.

### Required: Concordance Formation Rate Tracker

```python
class ConcordanceFormationMonitor:
    WINDOW_MINUTES = 30
    MIN_FORMATION_RATE_WITH_SUBMISSIONS = 0.15
    # At least 15% of submitted tickers should form concordances when >3 agents active.
    # 0% for extended period = either legitimately bad signals OR overlap failure.

    def check(self) -> ConcordanceFormationResult:
        window_start = utcnow() - timedelta(minutes=self.WINDOW_MINUTES)

        # Count submissions per ticker (alpha-buffer DB)
        submissions_by_ticker = alpha_buffer_db.query(
            "SELECT ticker, COUNT(DISTINCT agent) as agent_count "
            "FROM submissions WHERE created_at >= ? GROUP BY ticker",
            (window_start.isoformat(),)
        )

        # Count concordances formed
        concordances = alpha_buffer_db.query(
            "SELECT COUNT(*) as count FROM concordance_results WHERE created_at >= ?",
            (window_start.isoformat(),)
        )

        total_submitted_tickers = len(submissions_by_ticker)
        multi_agent_tickers = sum(1 for r in submissions_by_ticker if r["agent_count"] >= 2)
        concordances_formed = concordances[0]["count"] if concordances else 0

        if total_submitted_tickers == 0:
            # No submissions at all — handled by Stage 2
            return ConcordanceFormationResult(status="NO_SUBMISSIONS")

        overlap_rate = multi_agent_tickers / total_submitted_tickers
        formation_rate = concordances_formed / max(multi_agent_tickers, 1)

        result = ConcordanceFormationResult(
            submitted_tickers=total_submitted_tickers,
            multi_agent_tickers=multi_agent_tickers,
            concordances_formed=concordances_formed,
            overlap_rate=overlap_rate,
            formation_rate=formation_rate,
        )

        # Flag if submissions are arriving but no overlap is forming
        if total_submitted_tickers >= 5 and overlap_rate < 0.10:
            result.flag = "LOW_OVERLAP"
            result.message = (
                f"Agents submitting {total_submitted_tickers} tickers but only "
                f"{multi_agent_tickers} overlap ({overlap_rate:.0%}). "
                f"Concordances impossible without overlap. "
                f"Check scanner pool alignment — all agents may be scanning different tickers."
            )

        chronicle.write("concordance_formation_rate", result.to_dict())
        return result
```

**This is informational, not a TRS blocker.** Zero overlap is not a system failure — it
may mean the scanner algorithms are returning divergent signals (which is valid). Surface
it in the health dashboard and EOD report so Ahmed can see it. Do NOT block trading on
low overlap alone.

---

## Amendment S8 — OMNI Synthesis Counter Continuity Across Restarts

### The Gap
OMNI's `syntheses_today` counter lives in application memory. Every restart resets it
to 0. The v2.1 flow verifier (Stage 4) checks `syntheses_today incrementing` — but after
a restart, `syntheses_today=0` looks identical to "no syntheses ever ran today" vs
"restarted 4 minutes ago, counting from zero."

**Impact today:** OMNI restarted 5 times. Each restart reset the counter. The flow
verifier couldn't distinguish between "OMNI just restarted" and "OMNI has been silent
for hours." This contributed to false-calm readings after each restart.

### Required: Durable Synthesis Counter in CHRONICLE

```python
# In OMNI's synthesis completion handler (add to existing code):
def _record_synthesis_completion(ticker, verdict, synthesis_id):
    # Existing: update in-memory counter
    app_state["syntheses_today"] += 1

    # New: write to CHRONICLE (durable across restarts)
    chronicle.write("synthesis_completed", {
        "synthesis_id": synthesis_id,
        "ticker": ticker,
        "verdict": verdict,
        "ts": utcnow(),
        "trade_date": today_et(),
    })
```

**In nexus-integrity flow verifier Stage 4 (replace in-memory check):**
```python
# Instead of: OMNI /health → syntheses_today incrementing
# Use: CHRONICLE count for today

syntheses_today_durable = chronicle.count(
    "SELECT COUNT(*) FROM synthesis_completed WHERE trade_date = ?",
    (today_et(),)
)
# Now survives restarts. 0 at 10:00 AM ET actually means 0 syntheses today.
```

**OMNI /health endpoint enhancement:** Add `syntheses_today_durable` to the response,
sourced from CHRONICLE — so external monitors don't need DB access.

---

## Amendment S9 — TRS Null-State Handling Post-Restart

### The Gap
The TRS sidecar fail-closed contract (V1 amendment) correctly returns TRS=0 when
CHRONICLE is unreachable or the TRS entry is stale. But there is an unguarded window
*during the sidecar's own startup* — the first 90 seconds after the sidecar starts,
before it has computed and written its first TRS snapshot. During this window, execution
services cache a stale TRS and will eventually hit TTL expiry and hard-block — but the
timing is non-deterministic.

### Required: Explicit Sidecar Startup State

```python
class TRSSidecar:

    def __init__(self):
        self.startup_complete = False

    def startup(self):
        # Write STARTING state immediately — before first computation
        chronicle.write_trs_snapshot(TRSResult(
            score=0,
            reason="TRS_SIDECAR_STARTING",
            blocking=True,   # fail-closed during startup
            computed_by="trs_sidecar_startup",
        ))

        # Run first computation
        first_score = self.compute_trs()
        chronicle.write_trs_snapshot(first_score)
        self.startup_complete = True

        # Now begin normal 90s refresh cycle
        self.run_refresh_loop()
```

**Contract addition (add to v2.1 spec Section on TRS):**
> On sidecar startup, the first write to CHRONICLE MUST be a blocking TRS=0 record
> with reason="TRS_SIDECAR_STARTING". The sidecar MUST NOT write a non-zero score
> until its first full computation completes successfully. This eliminates the
> startup ambiguity window.

---

## Amendment S10 — EOD Manifest: "Hidden Failure" Section

### The Gap
v2.1's EOD governance report answers 5 questions — all good. But it doesn't explicitly
ask: *"Did any failure today evade all detection layers and only became visible through
manual investigation or user report?"*

This is the most important operational question. A failure that slips through the
monitoring system is categorically more dangerous than one that is detected immediately.
It means the monitoring system has a blind spot. Blind spots must be tracked explicitly.

**Today's evidence:** The Oracle engine function mismatch ran undetected for hours.
No existing monitor (v2.1 included) would have caught it. It was found manually
during a sweep. This should be recorded as a monitoring system failure, not just
a pipeline failure.

### Required: EOD Manifest Section 6 — Monitoring Evasion Log

Add to the EOD report (4:15 PM ET, weekdays):

```
SECTION 6 — MONITORING BLIND SPOTS

Q: Did any failure today become visible only through:
   (a) Manual investigation   (b) User report   (c) Downstream symptom only

If yes for any failure:
  - Record the failure
  - Record how it was eventually discovered
  - Record the earliest point a monitoring check COULD have caught it
  - Record the gap in coverage
  - Create P2 CHRONICLE entry: "ADD PROBE FOR: [failure mode]"

Target: 0 blind spots per day.
Any day with a blind spot = monitoring system regression.
```

**This section is mandatory.** An EOD report without Section 6 is incomplete.
If there are no blind spots, it says: "Section 6: No monitoring evasions today."
It never says nothing.

---

## TRS Sub-Score Summary (v2.1 + Addendum)

The complete TRS sub-score registry after incorporating this addendum:

| Sub-Score | Source | Weight | TTL | Zero = Block? |
|---|---|---|---|---|
| Contract resolution | ORATS chain probe | V2.1 defined | 6 min | Via floor blocker |
| Alpaca live order | Options probe Stage 3 | V2.1 defined | 30 min | Via floor blocker |
| AI brain capacity | S5 brain connectivity | 8% | 35 min | <2 brains = yes |
| Data freshness | VIX age, pool age | V2.1 defined | 4 min | No |
| DB integrity | Schema version check | V2.1 defined | 15 min | Via floor blocker |
| Exit monitor active | Guardian check | V2.1 defined | 3 min | No |
| **Scoring quality** | **S1 Oracle probe** | **10%** | **35 min** | **No (AMBER)** |
| **Service stability** | **S2 restart cluster** | **8%** | **20 min** | **Multi-cluster = yes** |
| **Agent reliability** | **S4 window completion** | **7%** | **25 min** | **No (AMBER)** |

**Remaining weight distribution** (V2.1 weights adjusted proportionally for new sub-scores):
The three new sub-scores (scoring quality 10%, service stability 8%, agent reliability 7%)
absorb weight from the existing `pipeline_throughput` component (was 10%, reduce to 0% —
now fully covered by the new granular sub-scores) and `session_activity` (was 5%, reduce
to 3%).

---

## Implementation Notes for Genesis

**Build order:** S1 → S5 → S2 → S4 → S8 → S6 → S3 → S9 → S7 → S10

- **S1 and S5** first — they catch the two most costly failure classes from today
  (silent scoring degradation and brain unavailability). Both are Tier 2 probes with
  no external API calls in steady state.
- **S2 and S4** second — they catch the restart cycling pattern that also cost hours today.
- **S8** is a 20-line OMNI change — ship it with the first OMNI deploy.
- **S6** is a background daemon in nexus-integrity — straightforward to build.
- **S3** requires a new Oracle endpoint (`DELETE /oracle/cache/evict-nulls`) — coordinate
  with the Oracle service owner.
- **S9** is a one-time startup contract addition to the TRS sidecar — add at build time.
- **S7** is informational only — low priority, high value for Ahmed visibility.
- **S10** is a report template change — trivial, but mandatory.

**Dependency on v2.1:** All amendments assume the CHRONICLE schema from v2.1 exists.
S1, S2, S4, S5, S8 require the `health_events` and `monitoring_state` tables.
S6 requires `preflight_manifests`. S9 requires `trs_snapshots`. Build v2.1 schema first.

**Tests required (per amendment):**

| Amendment | Minimum Tests |
|---|---|
| S1 | T-S1a: probe returns DEGRADED when Oracle returns fallback values; T-S1b: probe returns GOOD when Oracle returns real data; T-S1c: TRS sub-score correctly set to 0 on DEGRADED |
| S2 | T-S2a: cluster alert fires on 3 distinct restarts in 20 min; T-S2b: single-service thrash alert fires on 3 restarts from same service; T-S2c: no alert when restarts are spread across 2+ hours |
| S3 | T-S3a: gate fires when >60% of sentinel tickers have null flow; T-S3b: eviction call fires; T-S3c: gate passes after eviction restores data |
| S4 | T-S4a: P1 alert fires when 2/4 windows have analyzed=0; T-S4b: no alert when ≥3/4 windows complete |
| S5 | T-S5a: returns trs_score=0 when <2 brains respond; T-S5b: returns trs_score=50 when 2/4; T-S5c: timeout respected per brain |
| S6 | T-S6a: TRS blocker set when service uptime < 5 min post-clearance; T-S6b: CANARY defers when blocker active; T-S6c: blocker expires after stability window |
| S7 | T-S7a: LOW_OVERLAP flag set when overlap_rate < 10%; T-S7b: no TRS impact from flag alone |
| S8 | T-S8a: synthesis count survives OMNI restart; T-S8b: /health returns durable count |
| S9 | T-S9a: TRS=0 blocking=True written on sidecar startup before first computation; T-S9b: non-zero score only written after successful first computation |
| S10 | T-S10a: EOD report fails validation if Section 6 missing; T-S10b: blind spot entry created for any failure discovered outside monitoring |

---

## Closing Statement

Every amendment in this addendum maps to a failure observed on 2026-04-29 that v2.1
would not have caught. None are theoretical. None are edge cases. They are the exact
failure modes that cost hours of trading time today.

The v2.1 spec answers the question "is the pipeline connected?" This addendum answers
the question "is the data flowing through it correctly, and is the pipeline stable over
time?" Together they leave no room for a hidden error that presents as healthy.

**Target state after full implementation:**
No failure on 2026-04-29 would have lasted more than 15 minutes undetected.
The current record was 2+ hours on the Oracle failure and all day on the cycling issue.

— SOVEREIGN 👁️
