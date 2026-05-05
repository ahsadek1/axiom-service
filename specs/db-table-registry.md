# DB Table Registry — Nexus V2
**Last updated:** April 19, 2026 | **Governance:** S4 DB Schema Governance (GENESIS)

All SQLite tables across all Nexus V2 services. Update this file whenever a new table is added.
PR descriptions must include a DB Table Registry diff for any schema changes.

## Naming Policy (LOCKED April 19 2026)
- **New tables MUST use service-scoped prefix:** `{service}_{table_name}` (e.g. `alpha_buffer_submissions`)
- **Existing tables are grandfathered** — they live in separate DB files (no active collision)
- The `db_guard.py` startup assertion prevents accidental DB path sharing
- Any `CREATE TABLE IF NOT EXISTS` in new/modified files is checked by pre-commit lint

## Table Inventory

| Service | Table Name | DB File | Prefixed? | Notes |
|---|---|---|---|---|
| alpha-buffer | submissions | alpha_buffer.db | No (grandfathered) | |
| alpha-buffer | concordance_results | alpha_buffer.db | No (grandfathered) | |
| alpha-buffer | circuit_breaker_state | alpha_buffer.db | No (grandfathered) | |
| alpha-buffer | trade_outcomes | alpha_buffer.db | No (grandfathered) | |
| prime-buffer | submissions | prime_buffer.db | No (grandfathered) | |
| prime-buffer | concordance_results | prime_buffer.db | No (grandfathered) | |
| prime-buffer | circuit_breaker_state | prime_buffer.db | No (grandfathered) | |
| prime-buffer | trade_outcomes | prime_buffer.db | No (grandfathered) | |
| alpha-execution | positions | alpha_execution.db | No (grandfathered) | |
| alpha-execution | daily_entries | alpha_execution.db | No (grandfathered) | |
| prime-execution | positions | prime_execution.db | No (grandfathered) | |
| prime-execution | daily_entries | prime_execution.db | No (grandfathered) | |
| cipher | picks | cipher.db | No (grandfathered) | |
| cipher | windows | cipher.db | No (grandfathered) | |
| atlas | picks | atlas.db | No (grandfathered) | |
| atlas | windows | atlas.db | No (grandfathered) | |
| sage | picks | sage.db | No (grandfathered) | |
| sage | windows | sage.db | No (grandfathered) | |
| ails | live_outcomes | ails.db | No (grandfathered) | |
| ails | agent_calibration | ails.db | No (grandfathered) | |
| ails | pattern_library | ails.db | No (grandfathered) | |
| guardian-angel | anomalies_v3 | guardian_angel.db | Partial | v3 suffix added |
| guardian-angel | healing_actions | guardian_angel.db | No (grandfathered) | |
| CHRONICLE | chronicle_failures | chronicle.db | Yes ✅ | |
| CHRONICLE | chronicle_investigations | chronicle.db | Yes ✅ | |
| CHRONICLE | chronicle_fixes | chronicle.db | Yes ✅ | |
| CHRONICLE | chronicle_trades | chronicle.db | Yes ✅ | |
| CHRONICLE | chronicle_adaptive_events | chronicle.db | Yes ✅ | |
| VECTOR | vector_investigations | vector.db | Yes ✅ | |
| VECTOR | vector_dispatches | vector.db | Yes ✅ | |
