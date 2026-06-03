# AGENT_SILENCE Root Cause Fix — 2026-04-23

## Root Cause
All agent pipeline_client.py defaulted PIPELINE_SENTINEL_URL to
http://localhost:8008 (AILS) instead of http://localhost:8010 (pipeline-sentinel).
Trace hops were silently misfired for 3+ days. Pipeline-sentinel saw zero agent
activity and fired AGENT_SILENCE every market open.

## Fix Applied
Added PIPELINE_SENTINEL_URL=http://localhost:8010 to .env files for:
cipher, atlas, sage, alpha-buffer, prime-buffer, alpha-execution, prime-execution

## Verified
Test trace hop confirmed in pipeline_sentinel.db at 08:10 ET 2026-04-23.
All 7 services restarted and healthy.

## Prevention
Any new service must explicitly set PIPELINE_SENTINEL_URL=http://localhost:8010
in its .env. Never rely on the default in pipeline_client.py.
