# SPECIALIZATION.md — ARCHITECT Heuristics

1. **Single Responsibility Check**: Every function — does it do exactly one thing? If the function name contains "and", "or", or "then", it probably does two things. Flag it for review.

2. **Config Layer Validation**: Thresholds, timeouts, secrets, URLs — are they in the right layer? Secrets: env var only. Thresholds: named constant in config.py. URLs: env var with localhost default. Any deviation = P1.

3. **DRY (Don't Repeat Yourself)**: Is the same logic duplicated in more than one file? Scoring logic that appears twice, auth checking that's reimplemented instead of shared, datetime formatting repeated in 3 places.

4. **Shared Module Usage**: Does the code use shared/ modules where they exist? (sovereign_comms, log_setup, chronicle_reader, scorer, etc.) Reimplementing what shared/ already provides = P1.

5. **Error Response Format Consistency**: Every service uses the same error response format: `{"error": "human-readable message"}`. Service using `{"message": ...}` or `{"detail": ...}` = P2.

6. **Type Annotation Completeness**: Every public function (not prefixed with _) must have full type annotations on all parameters and return value. Missing = P3. Wrong type = P1.

7. **Docstring Accuracy**: Is the docstring accurate? A docstring that says "returns None" when the function returns a dict is P1 (documentation is a contract).

8. **Test Coverage Patterns**: Does test coverage match CODE_MANDATE Article V requirements? Core business logic: 100%. API endpoints: 100%. Utility: 80%. Error paths: 100%.

9. **Import Organization**: Is the import structure clean? sys.path.insert for shared/ modules at top. No circular imports. Standard lib → third-party → local. Messy imports = P3.

10. **Naming Conventions**: Function names: snake_case verbs (compute_, validate_, get_, submit_). Constants: UPPER_SNAKE. Classes: PascalCase. Deviation from this = P3.
