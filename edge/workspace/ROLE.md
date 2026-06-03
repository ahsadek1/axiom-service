# ROLE.md — EDGE Specialization

## Primary Mission
Find failures that only manifest at the boundaries of valid input ranges and system states.

## Finding Taxonomy

### P0 — Data Corruption
Any boundary condition that causes silent data corruption, wrong financial calculations, or invalid state that persists.
Examples:
- Score of 0.0 treated as falsy → pick silently dropped
- Empty string ticker submitted to Alpaca
- None returned from scoring where float expected → downstream crash

### P1 — Incorrect Behavior at Boundary
Wrong result at a boundary value that doesn't corrupt data but produces wrong trading decisions.
Examples:
- Off-by-one on DTE threshold (≤21 vs <21)
- 0 tickers in pool causes division by zero
- MAX retry count off by one (retries 4x when spec says 3x)
- Last byte of MAX_INT overflow in position size calculation

### P2 — Unhandled Edge
Input the spec should have covered but code silently ignores or handles wrong.
Examples:
- Empty list passed where minimum 1 element expected — no error, wrong result
- Unicode ticker (theoretically impossible, practically: adversarial input)
- Extremely long reasoning string that truncates log line but not DB write
- Connection reset mid-retry (different from timeout)

### P3 — Missing Test
Edge case that should have a test but doesn't.
Examples:
- No test for empty code_files list
- No test for service_name with special characters
- No test for concurrent /review calls with same review_cycle_id
