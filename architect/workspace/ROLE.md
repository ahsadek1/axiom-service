# ROLE.md — ARCHITECT Specialization

## Finding Taxonomy

### P0 — Architectural Corruption
The design is fundamentally broken in a way that will cause cascading failures or make the system unmaintainable.
Examples:
- Two services owning the same data with divergent logic
- Business logic in a data access layer
- Config validation spread across 3 files with no single source of truth

### P1 — Mandate Violation
A clear violation of CODE_MANDATE Articles III-V or PRECISION MANDATE v1.
Examples:
- Function doing more than one thing (Article 3.1 violation)
- Hardcoded threshold instead of named constant (Article 3.2 violation)
- `os.getenv("SECRET", "default")` instead of `os.environ["SECRET"]` (Article 3.4 violation)
- Bare `except:` clause (Article 3.7 violation)

### P2 — Pattern Drift
Code that doesn't follow the established Nexus pattern without a documented reason.
Examples:
- New service uses different error response format than all other services
- New DB schema uses different naming convention than existing tables
- New endpoint doesn't follow the RESTful patterns of other endpoints

### P3 — Improvement Opportunity
Code that works and follows mandates, but could be cleaner or more maintainable.
Examples:
- Function could be extracted and reused across 2 services
- Config constant should be in shared/ not duplicated in each service
- Test could be parameterized to cover 5 cases instead of 1
