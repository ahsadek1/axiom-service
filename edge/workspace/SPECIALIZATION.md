# SPECIALIZATION.md — EDGE Heuristics

Ten specific heuristics applied to every review:

1. **Zero Value Check**: Every numeric parameter — does code handle 0 correctly? Zero tickers in pool, zero score, zero position size. Is there a guard or does it silently proceed?

2. **None/Null Propagation**: When a function can return None, is every caller checking for None before using the result? Unchecked None access in financial code is P0 (silent bad result) or P1 (crash).

3. **Empty Collection**: Every loop over a list — what happens if the list is empty? Does the function return correctly, or does it access index 0 on an empty list?

4. **String Length Boundaries**: Long strings passed to DB or log — are they truncated at safe lengths? Extremely short strings (1 char, 0 chars) — are they rejected or silently causing issues?

5. **Retry Exhaustion State**: After max retries, what is the exact state? Is the DB row marked correctly? Is SOVEREIGN notified? Does the service keep running? Partial retry exhaustion (2/3 retries) — is state consistent?

6. **Timeout Cascade**: If service A times out calling service B, does A correctly propagate the timeout to its own caller? Does it hang? Does it retry infinitely?

7. **Partial Success**: When sending to 3 agents (Cipher, Atlas, Sage), what happens if 2 succeed and 1 fails? Is the partial result handled, or does the whole window fail?

8. **Concurrent Same-ID**: Two requests with the same review_cycle_id hitting /review simultaneously. Does the DB handle ON CONFLICT correctly? Is the second request rejected or silently overwrites?

9. **MAX_INT Boundary**: Score calculations, position sizes, P-counts — what happens at very large values? Are integer overflow paths possible in Python? (Usually no, but check float precision near MAX_FLOAT.)

10. **Network Partial Write**: HTTP connection drops mid-response. Does the client handle truncated JSON? Does json.loads() raise an exception that gets caught, or does it crash the background thread?
