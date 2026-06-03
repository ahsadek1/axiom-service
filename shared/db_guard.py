"""
db_guard.py — Startup DB path collision guard.

Asserts at service startup that no two Nexus services share a SQLite DB path.
If a misconfiguration causes two services to open the same file, this guard
aborts startup with a clear error before any data corruption can occur.

Usage in each service's startup (FastAPI lifespan or __init__):
    from shared.db_guard import assert_unique_db_path
    assert_unique_db_path("alpha-buffer", settings.db_path)
"""

import os
from typing import Optional

# Registry: absolute DB path → canonical service name that owns it.
# Update this map whenever a new Nexus service with a SQLite DB is added.
KNOWN_DB_REGISTRY: dict[str, str] = {
    "/Users/ahmedsadek/nexus/data/alpha_buffer.db":     "alpha-buffer",
    "/Users/ahmedsadek/nexus/data/prime_buffer.db":     "prime-buffer",
    "/Users/ahmedsadek/nexus/data/alpha_execution.db":  "alpha-execution",
    "/Users/ahmedsadek/nexus/data/prime_execution.db":  "prime-execution",
    "/Users/ahmedsadek/nexus/data/cipher.db":           "cipher",
    "/Users/ahmedsadek/nexus/data/atlas.db":            "atlas",
    "/Users/ahmedsadek/nexus/data/sage.db":             "sage",
    "/Users/ahmedsadek/nexus/data/omni.db":             "omni",
    "/Users/ahmedsadek/nexus/data/ails.db":             "ails",
    "/Users/ahmedsadek/nexus/data/backtest.db":         "ails",
    "/Users/ahmedsadek/nexus/data/oracle_cache.db":     "oracle",
    "/Users/ahmedsadek/nexus/data/guardian_angel.db":   "guardian-angel",
    "/Users/ahmedsadek/nexus/data/telemetry.db":        "guardian-angel",
    "/Users/ahmedsadek/nexus/data/healing.db":          "guardian-angel",
    "/Users/ahmedsadek/nexus/data/sentinel.db":         "pipeline-sentinel",
    "/Users/ahmedsadek/nexus/data/window_mismatches.db": "omni",
}


def assert_unique_db_path(service_name: str, db_path: str) -> None:
    """
    Assert that the given db_path is registered exclusively to this service.

    Aborts with RuntimeError if another registered service owns this path,
    preventing silent table-level data corruption from path misconfiguration.

    Paths not in the registry are allowed through (new services, test DBs).
    Call this during service startup before init_db().

    Args:
        service_name: The calling service's canonical name (e.g. 'alpha-buffer').
        db_path: Absolute or relative path to the SQLite database file.

    Raises:
        RuntimeError: If the path is registered to a different service.
    """
    resolved = os.path.abspath(db_path)
    expected_owner = KNOWN_DB_REGISTRY.get(resolved)

    if expected_owner is not None and expected_owner != service_name:
        raise RuntimeError(
            f"[db_guard] DB PATH COLLISION DETECTED\n"
            f"  Service '{service_name}' attempted to open: {resolved}\n"
            f"  That path is registered to: '{expected_owner}'\n"
            f"  Aborting startup to prevent data corruption.\n"
            f"  Fix: check {service_name} DB_PATH env var or config."
        )


def get_registry() -> dict[str, str]:
    """
    Return a copy of the DB path registry.

    Used by tests and diagnostics to inspect the current registry state.

    Returns:
        Dict mapping absolute DB path → owning service name.
    """
    return dict(KNOWN_DB_REGISTRY)
