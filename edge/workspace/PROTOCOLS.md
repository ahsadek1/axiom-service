# PROTOCOLS.md — EDGE Review Protocol

Same flow as PROBE: POST /review → 202 → async → CHRONICLE → SOVEREIGN.

## EDGE-Specific Prompt Construction
When building the user message, append this after the code files:
"Focus on: (1) every place a 0, None, or empty collection could be passed — trace what happens. (2) every retry loop — what is the exact state after exhaustion? (3) every partial success path — is state consistent when 1 of N external calls fails?"

## Severity Rules
- FAIL: p0_count > 0 (any data corruption finding)
- CONDITIONAL: p1_count > 3
- PASS: otherwise
