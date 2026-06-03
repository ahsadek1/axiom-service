# PROTOCOLS.md — CONTRACT Review Protocol

Same flow as PROBE: POST /review → 202 → async → CHRONICLE → SOVEREIGN.

## CONTRACT-Specific Prompt Construction
When reviewing, explicitly ask the brain to:
1. List every external HTTP call in the code
2. For each call: what auth header, what payload fields, what timeout, what retry behavior
3. Cross-reference with the spec's documented contracts
4. Flag any mismatch as a finding with the appropriate severity
