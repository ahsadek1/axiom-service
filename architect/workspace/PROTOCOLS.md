# PROTOCOLS.md — ARCHITECT Review Protocol

Same flow as PROBE: POST /review → 202 → async → CHRONICLE → SOVEREIGN.

## ARCHITECT-Specific Prompt Construction
When reviewing, explicitly ask the brain to:
1. List every function and assess: does it do exactly one thing?
2. List every config value and assess: is it in the correct layer?
3. Check each module against CODE_MANDATE Articles III-V
4. Identify any patterns that diverge from the established Nexus codebase style
