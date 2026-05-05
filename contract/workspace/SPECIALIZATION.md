# SPECIALIZATION.md — CONTRACT Heuristics

1. **Auth Header Consistency**: Every HTTP call must use the correct auth header for the target service (X-Nexus-Secret, X-Oracle-Secret, X-Nexus-Prime-Secret, etc.). Wrong header = P0.

2. **Payload Required Fields**: When a service submits to another, every required field in the target's spec must be present in the payload. Missing required field = P0.

3. **Response Parsing Safety**: When parsing a response from another service, every field access must use `.get()` or handle KeyError. Unguarded `response["field"]` = P1.

4. **Timeout Alignment**: Caller's timeout must be >= callee's maximum processing time. If callee can take 30s and caller has 5s timeout, that's a P1 contract mismatch.

5. **Status Code Handling**: Every HTTP call must handle 401, 403, 404, 429, 500, 503 explicitly. Treating all non-200 as equivalent = P1.

6. **Retry Idempotency**: If a caller retries, the callee must handle duplicate submissions gracefully. Check callee's deduplication logic when caller has retry logic.

7. **Schema Version Drift**: If one service has been updated and another hasn't, the schema may have drifted. Look for fields present in old calls but removed from new receiver.

8. **URL Hardcoding vs Env Var**: Every service URL must come from an env var. Hardcoded http://localhost:PORT is P1 (breaks in multi-machine deployment).

9. **Content-Type Header**: Every POST with JSON body must send Content-Type: application/json. Missing header on requests that need it = P2.

10. **Error Propagation**: When service A calls service B and B returns an error, does A propagate the error context or swallow it? Swallowed errors make debugging impossible = P1.
