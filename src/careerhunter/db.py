"""SQLite connection settings and the versioned migration runner.

Migrations are SQL files in careerhunter/migrations named NNNN_short_name.sql. Each is
applied once, inside a transaction, and its SHA-256 is recorded. If a migration that
was already applied is later edited, the runner refuses to continue: history is fixed,
so changes go in a new migration.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def db_files(path: Path) -> list[Path]:
    """The database file plus its WAL and shared-memory companions."""
    return [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]


def loose_permissions(path: Path) -> list[str]:
    """Database files that other users could read or write, as 'name (mode)'."""
    loose = []
    for f in db_files(path):
        if f.exists():
            mode = stat.S_IMODE(f.stat().st_mode)
            if mode & 0o077:
                loose.append(f"{f.name} ({oct(mode)})")
    return loose


def connect(path: Path) -> sqlite3.Connection:
    """Open the database in autocommit mode; callers use `transaction()` for writes.

    The file is created owner-only (0600) before SQLite touches it, and the WAL and
    shared-memory files are tightened to match once WAL mode is on.
    """
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    for f in db_files(path):
        if f.exists():
            os.chmod(f, 0o600)
    return conn


def load_migrations() -> list[Migration]:
    found: list[Migration] = []
    for entry in resources.files("careerhunter.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append(
                Migration(int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8"))
            )
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"Migration versions must be 1..N with no gaps, found {versions}")
    return found


_BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TRIGGER IF NOT EXISTS no_delete_schema_migrations BEFORE DELETE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'schema_migrations is permanent'); END;
CREATE TRIGGER IF NOT EXISTS no_update_schema_migrations BEFORE UPDATE ON schema_migrations
BEGIN SELECT RAISE(ABORT, 'schema_migrations is permanent'); END;
"""


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations. Returns the versions applied this run."""
    migrations = load_migrations() if migrations is None else migrations
    conn.executescript(_BOOKKEEPING)
    applied = {
        row["version"]: row["checksum"]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }
    newly: list[int] = []
    for m in migrations:
        if m.version in applied:
            if applied[m.version] != m.checksum:
                raise MigrationError(
                    f"Migration {m.version:04d}_{m.name} was edited after it was applied. "
                    "Put schema changes in a new migration instead."
                )
            continue
        script = (
            "BEGIN;\n"
            f"{m.sql}\n;\n"
            "INSERT INTO schema_migrations (version, name, checksum) "
            f"VALUES ({m.version}, '{m.name}', '{m.checksum}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        newly.append(m.version)
    return newly


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"] or 0)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
