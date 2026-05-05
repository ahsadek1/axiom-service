# Resilience Framework Build Task
# Authority: Ahmed Sadek | Date: 2026-04-14 | Priority: IMMEDIATE

Read the full spec at: /Users/ahmedsadek/.openclaw/workspace-genesis/RESILIENCE_FRAMEWORK.md

## WHAT TO BUILD

Build the complete Nexus V2 Operational Resilience Framework.
All code goes in /Users/ahmedsadek/nexus/resilience/

## FILE STRUCTURE

```
/Users/ahmedsadek/nexus/resilience/
  pre_market_snapshot.py      # Approach 1: 6 AM snapshot, 8:30 AM validation, 60s drift detection
  redundant_data_client.py    # Approach 2: Primary/fallback data source wrapper (async)
  trade_state_machine.py      # Approach 3: Universal trade state machine + crash recovery
  predictive_failure_model.py # Approach 4: 10 failure class predictors, 30s cycle
  canary_protocol.py          # Approach 5: 9:31 AM canary trades, SPY/AAPL/QQQ
  graceful_degradation.py     # Approach 6: 5-level degradation manager
  resilience_manager.py       # Orchestrator — wires all 6 approaches together
  requirements.txt
  .env
  README.md
  tests/
    test_snapshot.py
    test_state_machine.py
    test_predictive_model.py
    test_canary.py
    test_degradation.py
```

## IMPLEMENTATION NOTES

### pre_market_snapshot.py
- `ServiceSnapshot` and `TradingDaySnapshot` dataclasses (use exact ones from spec)
- `capture_snapshot()` — hits /health on all 12 ports (8001-8009, 9001-9003), hashes configs, captures LaunchAgent plist mtimes
- `validate_snapshot()` — compares current state vs captured snapshot
- `drift_detection_loop()` — runs every 60 seconds during trading hours
- Persists to SQLite at /Users/ahmedsadek/nexus/data/snapshots.db
- SHA256 signature on full snapshot
- SAFE_AUTO_CORRECT actions: service_restart, cache_clear, rate_limiter_reset
- REQUIRES_AHMED actions: api_key_change, db_schema_change, agent_weight_change

### redundant_data_client.py
- `DataSourceResult` dataclass: data, source_used, is_primary, is_fallback, quality_score, latency_ms
- `RedundantDataClient` class — use exact implementation from spec
- Python 3.9 compatible: replace list[X] with List[X], dict[X,Y] with Dict[X,Y], use Union not |
- Wire up ORACLE fallback chain: ORACLE primary → Polygon → Alpha Vantage → historical vol × 1.15
- Wire up price fallback: Polygon → Alpha Vantage → yfinance
- Wire up macro/regime: FRED+composite → VIX-only

### trade_state_machine.py  
- Full `TradeState` enum with all states from spec
- `TRANSITIONS` dict
- `TradeSM` class with transition(), on_failure(), _handle_partial_fill_emergency()
- `recover_from_crash()` classmethod
- Python 3.9: no dict|union syntax
- SQLite logging of every state transition to /Users/ahmedsadek/nexus/data/trade_states.db

### predictive_failure_model.py
- All 10 failure class evaluators (use exact ones from spec)
- `PredictiveFailureModel` with `run_cycle()` async method
- INTERVENTION_THRESHOLD = 0.65, ALERT_THRESHOLD = 0.85
- `record_metric()` and `get_eod_prediction_report()`
- Intervention implementations: stub them with real logic where possible, pass for system-specific ones
- Python 3.9 compatible

### canary_protocol.py
- `CanaryResult` and `CanaryAssessment` dataclasses
- `CanaryProtocol` class
- CANARY_SIZE_USD = 200.0
- CANARY_TICKERS: NEXUS_ALPHA → ["SPY", "AAPL"], NEXUS_PRIME → ["SPY", "QQQ"]
- `run()` executes canaries, `_assess_results()` decides full-size approval
- Sends Telegram report via bot token 7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c, chat 8573754783

### graceful_degradation.py
```python
# 5 degradation levels:
# LEVEL 0: Full operation — all sources primary, all brains, full size
# LEVEL 1: Minor — 1 source on fallback OR 1 brain down → EOD note only, full size
# LEVEL 2: Moderate — multiple fallbacks OR 2 brains down → 50% size, alert Ahmed
# LEVEL 3: Severe — primary sources unavailable OR all brains down → no new entries
# LEVEL 4: Emergency Halt — execution bridge down OR VIX breach → full halt

# ABSOLUTE NON-NEGOTIABLES (never degraded under any condition):
# vix_brake_enforcement, position_limit_enforcement, invariant_enforcement,
# existing_position_monitoring, exit_execution, stop_loss_enforcement,
# circuit_breaker_enforcement

# QI degradation rules:
# QI_ONE_BRAIN_TIMEOUT → Level 1, full size
# QI_TWO_BRAINS_TIMEOUT → Level 2, 25% size, alert Ahmed
# QI_ALL_BRAINS_TIMEOUT → Level 3, no new entries
# CAPITAL_ROUTER_UNREACHABLE → Level 2, 50% size
# VIX_BRAKE_FULL → Level 4, full halt
```

### resilience_manager.py
- Orchestrates all 6 approaches
- `startup()` — runs at service start, loads/validates snapshot
- `pre_market_sequence()` — runs the unified pre-market timeline:
  - 6:00 AM: capture snapshot
  - 6:15 AM: validate + auto-correct safe drifts
  - 6:30 AM: start predictive model warmup
  - 7:45 AM: send diagnostic report to Ahmed
  - 8:55 AM: final clearance report
  - 9:00 AM: pre-market preflight
  - 9:31 AM: fire canaries
  - 9:45 AM: approve/deny full-size trading
- `trading_loop_cycle()` — called every 30s during trading:
  - Run predictive model cycle
  - Check drift detection
  - Update degradation level
- `eod_sequence()` — 4:15 PM EOD summary to Ahmed

## SECRETS (load from .env)
```
NEXUS_SECRET=62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2
NEXUS_PRIME_SECRET=62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2
ORACLE_SECRET=dba775f5d12e63730927f8b66af2778f3208aacc682baf6720a58aa1dc24a9f3
TELEGRAM_BOT_TOKEN=7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c
TELEGRAM_CHAT_ID=8573754783
POLYGON_API_KEY=7LkpjSEBTFsr3HCodQNppdphNU0Qmr7f
ALPHA_VANTAGE_KEY=5LPKGHYMW9ZK24KL
FRED_API_KEY=5749ecf7ebd18e7f77e30ef2357f55b7
```

## PYTHON 3.9 COMPATIBILITY RULES
- No `list[X]` — use `List[X]` from typing
- No `dict[X, Y]` — use `Dict[X, Y]` from typing
- No `X | Y` union syntax — use `Union[X, Y]`
- No match/case statements
- No walrus operator in complex expressions

## REQUIREMENTS
- requests, aiohttp, python-dotenv, pytz
- No AI/LLM calls in this module — pure infrastructure
- All async methods use asyncio
- Every function has docstring + type hints
- Comprehensive tests — minimum 5 per module

## AFTER BUILDING
1. Create .venv, install requirements
2. Run tests: .venv/bin/python -m pytest tests/ -v
3. All tests must pass

When completely finished, run:
openclaw system event --text "Done: Resilience framework built at nexus/resilience/ — tests passing" --mode now
