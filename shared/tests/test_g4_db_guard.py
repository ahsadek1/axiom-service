"""
test_g4_db_guard.py — G4: DB schema governance / db_guard tests.

Verifies: collision detection, self-registration passes, unknown paths allowed,
registry integrity. 6 test cases.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db_guard import assert_unique_db_path, get_registry, KNOWN_DB_REGISTRY


class TestDbGuardCollisionDetection(unittest.TestCase):

    def test_t1_correct_owner_passes(self):
        """T1: Service opening its own registered DB path — no error raised."""
        try:
            assert_unique_db_path(
                "alpha-buffer",
                "/Users/ahmedsadek/nexus/data/alpha_buffer.db",
            )
        except RuntimeError:
            self.fail("assert_unique_db_path raised RuntimeError for correct owner")

    def test_t2_wrong_owner_raises_runtime_error(self):
        """T2: alpha-buffer opening prime_buffer.db → RuntimeError with clear message."""
        with self.assertRaises(RuntimeError) as ctx:
            assert_unique_db_path(
                "alpha-buffer",
                "/Users/ahmedsadek/nexus/data/prime_buffer.db",
            )
        msg = str(ctx.exception)
        self.assertIn("DB PATH COLLISION", msg)
        self.assertIn("alpha-buffer", msg)
        self.assertIn("prime-buffer", msg)

    def test_t3_unknown_path_allowed(self):
        """T3: Path not in registry (e.g. test DB, new service) — passes without error."""
        try:
            assert_unique_db_path("new-service", "/tmp/some_new_service.db")
        except RuntimeError:
            self.fail("Unknown path should be allowed through (not in registry)")

    def test_t4_relative_path_resolved_correctly(self):
        """T4: Relative path that resolves to a registered absolute path is caught."""
        # Build a relative path that resolves to prime_buffer.db
        prime_abs = "/Users/ahmedsadek/nexus/data/prime_buffer.db"
        rel = os.path.relpath(prime_abs, os.getcwd())
        # alpha-buffer trying to open prime path via relative form
        with self.assertRaises(RuntimeError):
            assert_unique_db_path("alpha-buffer", rel)

    def test_t5_all_registered_services_can_open_own_db(self):
        """T5: Every service in the registry can open its own DB without error."""
        # Group paths by service — a service may own multiple DBs (e.g. guardian-angel)
        from collections import defaultdict
        service_paths = defaultdict(list)
        for path, service in KNOWN_DB_REGISTRY.items():
            service_paths[service].append(path)

        for service, paths in service_paths.items():
            for path in paths:
                try:
                    assert_unique_db_path(service, path)
                except RuntimeError as e:
                    self.fail(
                        f"Service '{service}' could not open its own DB '{path}': {e}"
                    )

    def test_t6_registry_returns_copy(self):
        """T6: get_registry() returns an independent copy — mutations don't affect original."""
        registry = get_registry()
        original_len = len(KNOWN_DB_REGISTRY)
        registry["fake_path"] = "fake-service"
        self.assertEqual(
            len(KNOWN_DB_REGISTRY),
            original_len,
            "get_registry() must return a copy, not a reference to the original",
        )


if __name__ == "__main__":
    unittest.main()
