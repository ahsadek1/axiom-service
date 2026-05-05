# ROLE.md — DEPLOY Specialization

## Finding Taxonomy

### P0 — Service Won't Start
The service will crash at startup or immediately after.
Examples:
- Required env var not in secrets file or plist
- ImportError from missing pip package not in requirements.txt
- DB path pointing to non-existent directory
- Port already in use (no conflict check)

### P1 — Service Starts Degraded
Service starts but key functionality is broken or unavailable.
Examples:
- Env var present but wrong value (empty string instead of None → not caught)
- Log directory doesn't exist (first log write fails, then silently stops logging)
- Health endpoint always returns 200 regardless of actual state

### P2 — Deploy Friction
Service works but deployment requires manual intervention or is fragile.
Examples:
- start.sh doesn't source secrets file before exec
- Plist missing KeepAlive → service doesn't restart on crash
- requirements.txt has ==version but package renamed in newer pip

### P3 — Missing Runbook
Operational documentation gap.
Examples:
- No comment in plist explaining what the service does
- No documented procedure for rotating the API key
- Health endpoint doesn't report version number
