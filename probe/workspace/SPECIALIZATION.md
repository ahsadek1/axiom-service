# SPECIALIZATION.md — PROBE Heuristics

Ten specific heuristics applied to every review:

1. **Auth Header Check**: Every endpoint except /health must check X-Nexus-Secret (or service-specific secret). Any endpoint missing this check is P0.

2. **Exception Specificity**: Every `except` clause must catch specific exception types. `except Exception as e: pass` without logging is P0. `except Exception as e: logger.error(...)` is P1.

3. **Secret in Logs**: If any log statement could print a secret (from headers, env vars, or request bodies), that is P0. Check `logger.info/debug/error` calls near auth checks.

4. **Race Condition on Shared State**: Any global variable mutated by multiple threads without a lock is P1. Check `_sovereign_halted`, counters, lists.

5. **SQLite Injection**: Any SQL query using f-strings or .format() instead of parameterized `?` placeholders is P0.

6. **Silent Failure Returns**: Functions that return `None` on error without logging the error or notifying SOVEREIGN are P1. Dead-end error paths hide bugs.

7. **Timeout on External Calls**: Every `requests.post/get` must have explicit `timeout=` parameter. Missing timeouts are P1 (hangs = cascading failure).

8. **Type Annotation Coverage**: Functions missing type annotations on parameters or return values are P3. Trading system functions with `Any` return type without documentation are P2.

9. **Magic Numbers**: Numeric literals in conditional logic without named constants are P2. Example: `if score > 65:` where 65 is a threshold.

10. **Docstring Completeness**: Every public function must have a docstring describing what it does, its parameters, and what it returns. Missing = P3. Inaccurate = P2.
