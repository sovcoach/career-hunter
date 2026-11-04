"""SQLite connection settings and the versioned migration runner.

There are two kinds of database file, each with its own migration track:

- `search`: search.db, the search and learning layer;
- `ei_record`: one ei_record_<label>.db per EI claim.

Migrations are SQL files in careerhunter/migrations/<track>/ named NNNN_short_name.sql.
Each is applied once and its SHA-256 is recorded. All the migrations pending in one run
are applied inside a single BEGIN IMMEDIATE transaction, so only one process migrates a
file at a time, and a failure applies nothing. If a migration that was already applied
is later edited, the runner refuses to continue: history is fixed, so changes go in a
new migration.

Every file also records which track it belongs to, in a one-row `db_identity` table, and
the runner checks it on every open. A search.db can't be migrated as an EI record (or
the other way round), and a file without an identity, such as the single
careerhunter.db from v0.1.0, is refused.
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
# How long migrate waits for another process's write lock before it gives up.
MIGRATE_BUSY_TIMEOUT_MS = 60_000
TRACKS = ("search", "ei_record")
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


def tighten_permissions(path: Path) -> None:
    for f in db_files(path):
        if f.exists():
            os.chmod(f, 0o600)


def connect(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    """Open the database in autocommit mode; callers use `transaction()` for writes.

    The file is created owner-only (0600) before SQLite touches it, and the WAL and
    shared-memory files are tightened to match once WAL mode is on. `wal=False` keeps a
    rollback journal, for a file that is being built before it is moved into place.
    """
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        conn.close()
        raise
    tighten_permissions(path)
    return conn


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an existing database for reading only. Never creates or changes a file's mode."""
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _check_track(track: str) -> None:
    if track not in TRACKS:
        raise MigrationError(f"unknown migration track {track!r}; expected one of {TRACKS}")


def load_migrations(track: str) -> list[Migration]:
    _check_track(track)
    found: list[Migration] = []
    for entry in resources.files(f"careerhunter.migrations.{track}").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append(
                Migration(int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8"))
            )
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(
            f"{track} migration versions must be 1..N with no gaps, found {versions}"
        )
    return found


def latest_version(track: str) -> int:
    return len(load_migrations(track))


_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _only_comments(text: str) -> bool:
    """True when `text` holds nothing but comments, whitespace and semicolons.

    A `--` inside a string literal would also be cut, but what is left of that
    statement is then not empty, so a real statement is never mistaken for a comment.
    """
    rest = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))
    return not rest.replace(";", "").strip()


def split_statements(sql: str) -> list[str]:
    """Split migration SQL into single statements, in order, for `execute`.

    The text is cut at every `;` and the pieces are joined again until SQLite says the
    statement is complete. A `;` inside a comment, a string literal or a trigger body
    therefore does not end a statement, and two statements on one line come out
    separately. Pieces that hold only comments are dropped. Text after the last
    complete statement must be comments or whitespace; anything else is refused.
    """
    statements: list[str] = []
    buffer = ""
    pieces = sql.split(";")
    for piece in pieces[:-1]:
        buffer += piece + ";"
        if sqlite3.complete_statement(buffer):
            if not _only_comments(buffer):
                statements.append(buffer.strip())
            buffer = ""
    buffer += pieces[-1]
    if not _only_comments(buffer):
        raise MigrationError("migration SQL ends with an incomplete statement")
    return statements


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
CREATE TABLE IF NOT EXISTS db_identity (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    track      TEXT    NOT NULL CHECK (track IN ('search', 'ei_record')),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TRIGGER IF NOT EXISTS no_delete_db_identity BEFORE DELETE ON db_identity
BEGIN SELECT RAISE(ABORT, 'db_identity is permanent'); END;
CREATE TRIGGER IF NOT EXISTS no_update_db_identity BEFORE UPDATE ON db_identity
BEGIN SELECT RAISE(ABORT, 'db_identity is permanent'); END;
"""


def file_track(conn: sqlite3.Connection) -> str | None:
    """The track recorded in this file, or None if it has no identity."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'db_identity'"
    ).fetchone()
    if not has_table:
        return None
    row = conn.execute("SELECT track FROM db_identity").fetchone()
    return str(row["track"]) if row else None


def _wrong_track(found: str, track: str) -> MigrationError:
    return MigrationError(f"this is a {found} database, not a {track} database")


def _claim_identity(conn: sqlite3.Connection, track: str) -> None:
    """Give a brand-new file its identity, or check the identity it already has.

    Runs inside the migration transaction: nothing here commits.
    """
    found = file_track(conn)
    if found == track:
        return
    if found is not None:
        raise _wrong_track(found, track)
    if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
        raise MigrationError(
            "this database has no career-hunter identity (it may be the single "
            f"careerhunter.db from v0.1.0); refusing to treat it as a {track} database"
        )
    for statement in split_statements(_BOOKKEEPING):
        conn.execute(statement)
    conn.execute("INSERT INTO db_identity (id, track) VALUES (1, ?)", (track,))


def _applied(conn: sqlite3.Connection) -> dict[int, str] | None:
    """Applied versions and their checksums, or None when the file has no bookkeeping."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not has_table:
        return None
    return {
        int(row[0]): str(row[1])
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }


def _pending(applied: dict[int, str], migrations: list[Migration]) -> list[Migration]:
    """The migrations not applied yet. An applied one whose checksum changed is refused."""
    pending: list[Migration] = []
    for m in migrations:
        if m.version not in applied:
            pending.append(m)
        elif applied[m.version] != m.checksum:
            raise MigrationError(
                f"Migration {m.version:04d}_{m.name} was edited after it was applied. "
                "Put schema changes in a new migration instead."
            )
    return pending


def _require_autocommit(conn: sqlite3.Connection) -> None:
    """The runner issues BEGIN and COMMIT itself, so the module must not."""
    if (
        conn.isolation_level is not None
        or conn.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
        or conn.in_transaction
    ):
        raise MigrationError("migrate needs an autocommit connection with no open transaction")


def _take_migration_lock(conn: sqlite3.Connection) -> None:
    """BEGIN IMMEDIATE, waiting up to MIGRATE_BUSY_TIMEOUT_MS for another writer."""
    previous = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    conn.execute(f"PRAGMA busy_timeout = {int(MIGRATE_BUSY_TIMEOUT_MS)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise MigrationError(
                "another process held the migration lock; nothing was applied"
            ) from exc
        raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {previous}")


def migrate(
    conn: sqlite3.Connection, track: str, migrations: list[Migration] | None = None
) -> list[int]:
    """Apply pending migrations for `track`. Returns the versions applied this run.

    When the file already has its identity and every migration is applied (checksums
    still checked), this returns without taking a write lock. Otherwise it takes one
    BEGIN IMMEDIATE transaction, claims or checks the identity, re-reads the applied
    versions inside the lock, runs each pending migration statement by statement, and
    commits once. Any error rolls the whole run back and is re-raised, so a process
    that waited for another one's lock applies only what is still pending, or nothing.
    """
    _check_track(track)
    migrations = load_migrations(track) if migrations is None else migrations
    _require_autocommit(conn)
    found = file_track(conn)
    if found is not None and found != track:
        raise _wrong_track(found, track)
    if found == track:
        applied = _applied(conn)
        if applied is not None and not _pending(applied, migrations):
            return []
    _take_migration_lock(conn)
    try:
        _claim_identity(conn, track)
        applied = _applied(conn)
        if applied is None:  # pragma: no cover - _claim_identity creates the table
            raise MigrationError("schema_migrations is missing after the identity check")
        newly: list[int] = []
        for m in _pending(applied, migrations):
            for statement in split_statements(m.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
                (m.version, m.name, m.checksum),
            )
            newly.append(m.version)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:  # a failed COMMIT can leave the transaction open
            conn.execute("ROLLBACK")
        raise
    return newly


def schema_version(conn: sqlite3.Connection) -> int:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"] or 0)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:  # a failed COMMIT can leave the transaction open
            conn.execute("ROLLBACK")
        raise
