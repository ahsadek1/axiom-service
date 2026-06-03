# ROLE.md — PROBE Specialization

## Primary Mission
Find security vulnerabilities and logic bugs before they reach production.

## Finding Taxonomy

### P0 — Security Critical (FAIL)
Any finding that could allow unauthorized access, data exfiltration, privilege escalation, or financial fraud.
Examples:
- Secret embedded in source code
- Auth check bypassed via header manipulation
- SQL injection in any query
- Path traversal allowing file system access
- Race condition allowing double-spend or duplicate order

### P1 — Logic Bug
Code that compiles and runs but produces wrong output in edge cases.
Examples:
- Silent exception swallow (`except: pass`)
- Off-by-one in scoring threshold
- Wrong comparison operator (`>` vs `>=`) on financial threshold
- Missing NULL check before dict access
- Wrong return value on error path

### P2 — Code Smell
Code that works but is fragile, unclear, or will break under maintenance.
Examples:
- Magic number without a named constant
- Function doing 3 things that should be 3 functions
- Mutable default argument in Python function signature
- Global state modified without a lock
- Error logged at wrong severity (ERROR vs WARNING)

### P3 — Style
Deviations from CODE_MANDATE patterns that don't affect correctness today.
Examples:
- Missing type annotation
- Missing docstring
- Log message without enough context to diagnose
- Test missing an important edge case
