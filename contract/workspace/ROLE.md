# ROLE.md — CONTRACT Specialization

## Finding Taxonomy

### P0 — Contract Broken
The integration will fail at runtime. Service A calls B with a payload B will reject.
Examples:
- Wrong auth header name (X-Nexus-Secret vs X-Oracle-Secret)
- Required field missing from submitted payload
- Wrong HTTP method (GET vs POST)
- Endpoint URL hardcoded to wrong port

### P1 — Contract Drift
The integration works today but will break when either service is updated.
Examples:
- Caller assumes optional field is always present
- Callee's response schema undocumented — caller uses dict access without .get()
- Timeout on caller is shorter than callee's processing time under load

### P2 — Undocumented Assumption
One service assumes something about another without a documented contract.
Examples:
- Caller assumes callee will retry on 503 — no retry documented in callee spec
- Callee assumes caller will re-submit on 408 — not in caller spec

### P3 — Missing Contract Test
A cross-service interaction that has no integration test.
