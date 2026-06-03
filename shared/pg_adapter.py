"""
pg_adapter.py — Unified SQLite / Postgres database adapter

Provides a drop-in `get_conn` replacement that transparently switches between
SQLite (local dev / Mac Mini) and Postgres (Railway cloud) based on DATABASE_URL.

Usage:
    from shared.pg_adapter import get_conn, P
    with get_conn(db_path) as conn:
        conn.execute("SELECT * FROM foo WHERE id = " + P, (42,))

- When DATABASE_URL is unset or empty → SQLite at db_path
- When DATABASE_URL is set (postgres:// or postgresql://) → Postgres

Key differences handled:
- Parameter placeholder: SQLite uses ?, Postgres uses %s  → use P constant
- Row factory: SQLite uses sqlite3.Row, Postgres uses RealDictCursor
- executescript: only available on SQLite; on Postgres each statement is executed separately
- PRAGMA: silently ignored on Postgres
- AUTOINCREMENT: handled by schema at init time (SQLite keeps AUTOINCREMENT,
  Postgres schemas use SERIAL — managed by init_db in each service)
- WAL mode: SQLite-only, no-op on Postgres path
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

logger = logging.getLogger("shared.pg_adapter")

# ── Detect backend ────────────────────────────────────────────────────────────

def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _is_postgres() -> bool:
    url = _database_url()
    return url.startswith(("postgres://", "postgresql://"))


# ── Parameter placeholder ─────────────────────────────────────────────────────

#: Use this constant for parameterised queries so they work on both backends.
#: Example: conn.execute("SELECT * FROM foo WHERE id = " + P, (42,))
P: str = "%s" if _is_postgres() else "?"


# ── Compat row wrapper ────────────────────────────────────────────────────────

class _PgRow(dict):
    """
    Thin wrapper around a psycopg2 RealDictRow / dict that mimics sqlite3.Row.
    Supports both key-based and index-based access so existing callers work.
    """

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def keys(self):
        return super().keys()


class _PgCursor:
    """
    Wraps a psycopg2 cursor to provide an sqlite3-compatible interface:
    - .execute() with ? placeholders auto-converted to %s
    - .executescript() split on semicolons
    - .fetchall() / .fetchone() return _PgRow instances
    - .rowcount mirrored
    - .lastrowid via RETURNING id clause (if present in query)
    """

    def __init__(self, cursor):
        self._cur = cursor
        self._lastrowid: Optional[int] = None

    def execute(self, sql: str, params=None):
        pg_sql = sql.replace("?", "%s")
        # Strip SQLite-specific PRAGMA statements silently
        if pg_sql.strip().upper().startswith("PRAGMA"):
            return self
        if params is not None:
            self._cur.execute(pg_sql, params)
        else:
            self._cur.execute(pg_sql)
        # Capture lastrowid if available (INSERT ... RETURNING id)
        if self._cur.description and self._cur.description[0][0] == "id":
            try:
                row = self._cur.fetchone()
                if row:
                    self._lastrowid = row["id"] if isinstance(row, dict) else row[0]
            except Exception:
                pass
        return self

    def executescript(self, sql: str):
        """Split on semicolons and execute each statement."""
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            pg_stmt = stmt.replace("?", "%s")
            if pg_stmt.strip().upper().startswith("PRAGMA"):
                continue
            try:
                self._cur.execute(pg_stmt)
            except Exception as exc:
                logger.debug("executescript stmt skipped: %s — %s", pg_stmt[:80], exc)
        return self

    def fetchall(self) -> list:
        rows = self._cur.fetchall()
        return [_PgRow(r) for r in rows] if rows else []

    def fetchone(self) -> Optional[_PgRow]:
        row = self._cur.fetchone()
        return _PgRow(row) if row else None

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> Optional[int]:
        return self._lastrowid

    @property
    def description(self):
        return self._cur.description


class _PgConnWrapper:
    """
    Wraps a psycopg2 connection to present an sqlite3-like interface with
    .execute(), .executescript(), and row factory support.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=None) -> _PgCursor:
        from psycopg2.extras import RealDictCursor
        cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        wrapped = _PgCursor(cursor)
        return wrapped.execute(sql, params)

    def executescript(self, sql: str) -> _PgCursor:
        from psycopg2.extras import RealDictCursor
        cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        wrapped = _PgCursor(cursor)
        return wrapped.executescript(sql)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


# ── Unified get_conn ──────────────────────────────────────────────────────────

@contextmanager
def get_conn(db_path: str) -> Generator[Any, None, None]:
    """
    Context manager yielding a database connection.

    Transparently picks SQLite or Postgres based on DATABASE_URL env var.
    The yielded object has an sqlite3-like interface regardless of backend.

    Args:
        db_path: Path to SQLite file (ignored when DATABASE_URL is set).

    Yields:
        Connection wrapper with .execute(), .executescript(), row-dict results.
    """
    if _is_postgres():
        import psycopg2
        conn = psycopg2.connect(_database_url())
        conn.autocommit = False
        wrapped = _PgConnWrapper(conn)
        try:
            yield wrapped
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ── Postgres schema helpers ───────────────────────────────────────────────────

def adapt_schema_for_postgres(sql: str) -> str:
    """
    Convert SQLite DDL to Postgres-compatible DDL.

    Handles:
    - INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY
    - PRAGMA → removed
    - CHECK (id = 1) → kept as-is (Postgres supports it)

    Args:
        sql: SQLite DDL string.

    Returns:
        Postgres-compatible DDL.
    """
    import re
    # AUTOINCREMENT → SERIAL
    sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def init_schema(db_path: str, schema_sql: str) -> None:
    """
    Execute a schema DDL block on the configured backend.

    On Postgres, adapts the SQL automatically. Idempotent (uses IF NOT EXISTS).

    Args:
        db_path:    SQLite path (ignored for Postgres).
        schema_sql: Raw DDL (written for SQLite; auto-adapted for Postgres).
    """
    if _is_postgres():
        schema_sql = adapt_schema_for_postgres(schema_sql)

    with get_conn(db_path) as conn:
        conn.executescript(schema_sql)
