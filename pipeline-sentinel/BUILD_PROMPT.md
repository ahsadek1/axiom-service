Read the full spec at /Users/ahmedsadek/nexus/specs/pipeline-sentinel.md first.

Build the complete pipeline-sentinel microservice for the Nexus trading system.

## Files to create:

**models.py** — Pydantic v1 models (Python 3.9, use Union[X,Y] not X|Y):
- HopEnum (axiom_push, agent_received, omni_started, omni_completed, execution_received, alpaca_submitted, alpaca_confirmed) [buffer_accepted is deprecated]
- ServiceEnum (axiom, cipher, atlas, sage, alpha-buffer, prime-buffer, omni, alpha-execution, prime-execution)
- TraceRequest(trace_id, hop, service, ticker, pathway, status, metadata)
- HealthScoreComponents(pipeline_completion_rate, inter_service_latency_p95_ms, omni_brain_latency_p95_ms, oracle_cache_freshness, active_anomaly_count, stalled_picks_count)
- SystemHealthResponse(health_score, score_components, status, recommended_size_multiplier, active_failures, context_block, submissions_open, computed_at, stale=False)
- AnomalyReport(source, anomaly_type, service, detail, ts)

**database.py** — SQLite with WAL mode. On init: PRAGMA integrity_check, if fail delete+recreate. Tables: traces, health_scores, failure_events, anomaly_reports. Functions: init_db, insert_trace, get_traces_for_id, insert_health_score, get_latest_health_score, insert_failure_event, get_active_failures, get_stalled_picks(stall_window_s), resolve_failure, prune_old_scores(keep_days=30), insert_anomaly_report, get_recent_traces(window_s=900). All operations wrapped in try/except — never propagate to caller.

**notifier.py** — TelegramNotifier: send_alert(message, failure_class). Dedup: same failure_class within 300s → skip. Retry 3x (1s/2s/4s backoff). Never raises.

**scorer.py** — HealthScorer.compute_score(db_path, notifier) -> SystemHealthResponse.
Score starts at 100. Deductions:
- pipeline_completion_rate (trailing 20 picks): 0pts at 100%, -30pts at 60% (linear)
- inter_service_latency_p95_ms (from trace timestamps): 0pts at 500ms, -20pts at 3000ms (linear)  
- omni_brain_latency_p95_ms (omni_started→omni_completed): 0pts at 2000ms, -15pts at 8000ms (linear)
- active_stalled_picks: -5 each, max -15
- active_failure_classes: -5 each, max -15
- ga_anomaly_flags: -5 if any anomaly in last 5 min
Bands: 85-100=NOMINAL(1.0,open), 70-84=DEGRADED(0.95,open), 55-69=IMPAIRED(0.85,open,alert), 30-54=CRITICAL(0.0,halted,alert), 0-29=EMERGENCY(0.0,halted,alert).

**classifier.py** — FailureClassifier. detect_all(db_path, stall_window_s, notifier) -> List[str].
Detects: PIPELINE_STALL (no hop for >stall_window_s, not at alpaca_confirmed), EXECUTOR_SLOW (alpaca latency >3000ms in 3+ of last 10), BRAIN_TIMEOUT (omni_started without omni_completed after 6min), NETWORK_DEGRADED (P99 inter-hop >2000ms in 3+ consecutive), AGENT_SILENT (0 hops from any agent in last 15min window), COMPLETION_RATE_LOW (<80% of last 10 reach alpaca_confirmed), DATA_STALE (>15min gap in traces).
Insert failure_events if not already active. Return active failure class names. Runs in daemon thread every 30s.

**main.py** — FastAPI on port 8008.
- Startup: init_db, start scorer loop (30s daemon thread), start classifier loop (30s daemon thread), cache health score in app.state
- Auth: X-Nexus-Secret header check for protected endpoints
- POST /trace — ALWAYS return {"status":"ok"} 200, async DB write, never block
- GET /health — no auth, {"status":"healthy","service":"pipeline-sentinel"}
- GET /system-health — auth, return cached SystemHealthResponse
- GET /pipeline/{trace_id} — auth, return trace hops
- GET /stalls — auth, return stalled picks
- POST /report — auth, store AnomalyReport

**requirements.txt:**
fastapi==0.104.1
uvicorn==0.24.0
pydantic==1.10.13
python-dotenv==1.0.0
requests==2.31.0
pytest==7.4.3
pytest-asyncio==0.21.1
httpx==0.25.2

**.env:**
NEXUS_SECRET=62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2
TELEGRAM_BOT_TOKEN=7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c
TELEGRAM_AHMED_CHAT_ID=8573754783
DATA_DIR=/Users/ahmedsadek/nexus/data
PIPELINE_SENTINEL_DB_PATH=/Users/ahmedsadek/nexus/data/pipeline_sentinel.db
STALL_WINDOW_SECONDS=300
SCORE_RECOMPUTE_INTERVAL_S=30
HEALTH_HALT_THRESHOLD=55
HEALTH_IMPAIR_THRESHOLD=70
LOG_LEVEL=INFO
PORT=8008

**MANDATE.md** — Pipeline Sentinel observes only. Never writes to other services' DBs. Never modifies picks. Never calls Alpaca.

**tests/test_pipeline_sentinel.py** — 12 tests, pytest + httpx TestClient, mock Telegram, tmp_path DB:
1. Happy path — 7 hops for one pick (current pipeline architecture), GET /pipeline/{id} returns all 7
2. Stall detection — axiom_push trace at T-360s, confirm PIPELINE_STALL in failure_events
3. Score computation — 12 picks / 7 complete → score deduction ~12pts from completion
4. Health halt — score=40 → submissions_open=False
5. Score recovery — 40→60→80, submissions_open flips correctly
6. EXECUTOR_SLOW — 5 alpaca traces with latency >3000ms metadata, confirm class created
7. AGENT_SILENT — no cipher hops in last 20min, confirm event
8. DB integrity — corrupt db file, init_db detects and recreates clean
9. Concurrent writes — 20 simultaneous POST /trace, confirm all 20 in DB
10. Context block — /system-health has context_block with all 5 required keys
11. Telegram dedup — same failure_class twice in <300s → sent only once
12. Score pruning — score at T-31days deleted, recent score survives

**../shared/pipeline_client.py** — Fire-and-forget trace helper. trace_hop(trace_id, hop, service, ticker, pathway, status="ok", metadata=None). Daemon thread, 2s timeout, silent on failure. Uses PIPELINE_SENTINEL_URL env (default http://localhost:8008).

**/Users/ahmedsadek/Library/LaunchAgents/ai.nexus.pipeline-sentinel.plist** — Standard Nexus launchd plist, port 8008, WorkingDirectory /Users/ahmedsadek/nexus/pipeline-sentinel, stdout/stderr to /Users/ahmedsadek/nexus/logs/pipeline-sentinel/.

## After writing all files:
1. python3.9 -m venv .venv
2. source .venv/bin/activate && pip install -r requirements.txt
3. python -m pytest tests/ -v
4. If ALL 12 tests pass: launchctl load /Users/ahmedsadek/Library/LaunchAgents/ai.nexus.pipeline-sentinel.plist && sleep 5 && curl http://localhost:8008/health

## Rules:
- Python 3.9: Union[X,Y] not X|Y, no walrus issues
- POST /trace ALWAYS 200 — never block the pipeline
- Daemon threads only
- Fix code if tests fail, never change tests
- When fully done: openclaw system event --text "pipeline-sentinel build complete — all tests pass, service live on port 8008" --mode now
