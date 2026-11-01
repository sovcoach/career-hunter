"""The migration runner: two tracks, checksums, and a recorded identity per file."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import db


@pytest.fixture(params=db.TRACKS)
def track(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def fresh(tmp_path: Path, track: str, opener: Any) -> sqlite3.Connection:
    c: sqlite3.Connection = opener(tmp_path / f"{track}.db", track)
    return c


def test_pragmas(fresh: sqlite3.Connection) -> None:
    assert fresh.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert fresh.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    assert fresh.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_each_track_is_contiguous_and_checksummed(fresh: sqlite3.Connection, track: str) -> None:
    migrations = db.load_migrations(track)
    expected = {"search": [1, 2], "ei_record": [1]}[track]
    assert [m.version for m in migrations] == expected
    assert db.schema_version(fresh) == db.latest_version(track) == expected[-1]
    recorded = [
        r[0] for r in fresh.execute("SELECT checksum FROM schema_migrations ORDER BY version")
    ]
    assert recorded == [m.checksum for m in migrations]


def test_migrate_is_idempotent(fresh: sqlite3.Connection, track: str) -> None:
    assert db.migrate(fresh, track) == []


def test_file_records_its_track(fresh: sqlite3.Connection, track: str) -> None:
    assert db.file_track(fresh) == track
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        fresh.execute("UPDATE db_identity SET track = 'search'")
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        fresh.execute("DELETE FROM db_identity")


def test_edited_migration_is_refused(fresh: sqlite3.Connection, track: str) -> None:
    original = db.load_migrations(track)[0]
    tampered = db.Migration(original.version, original.name, original.sql + "\n-- edit")
    with pytest.raises(db.MigrationError, match="edited after it was applied"):
        db.migrate(fresh, track, [tampered])


def test_schema_migrations_cannot_be_edited(fresh: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        fresh.execute("UPDATE schema_migrations SET checksum = 'x'")


@pytest.mark.parametrize(
    ("made_as", "opened_as"), [("search", "ei_record"), ("ei_record", "search")]
)
def test_a_file_cannot_be_migrated_as_the_other_track(
    tmp_path: Path, made_as: str, opened_as: str, opener: Any
) -> None:
    c = opener(tmp_path / "x.db", made_as)
    with pytest.raises(db.MigrationError, match=f"is a {made_as} database"):
        db.migrate(c, opened_as)


def test_a_v010_database_is_refused(tmp_path: Path, opener: Any) -> None:
    """The single careerhunter.db of v0.1.0 has migrations but no identity."""
    c = opener(tmp_path / "careerhunter.db")
    c.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, checksum TEXT);"
        "CREATE TABLE companies (id INTEGER PRIMARY KEY);"
    )
    for track in db.TRACKS:
        with pytest.raises(db.MigrationError, match="no career-hunter identity"):
            db.migrate(c, track)


def test_unknown_track_is_refused(tmp_path: Path) -> None:
    with pytest.raises(db.MigrationError, match="unknown migration track"):
        db.load_migrations("careerhunter")


def test_failed_migration_rolls_back(tmp_path: Path, opener: Any) -> None:
    c = opener(tmp_path / "x.db")
    bad = db.Migration(1, "bad", "CREATE TABLE t (x INTEGER); INSERT INTO nope VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(c, "search", [bad])
    assert db.schema_version(c) == 0
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='t'").fetchone()[0] == 0


def test_transaction_rolls_back_on_error(tmp_path: Path, opener: Any) -> None:
    c = opener(tmp_path / "x.db", "search")
    with pytest.raises(RuntimeError), db.transaction(c):
        c.execute("INSERT INTO companies (name, source) VALUES ('Rolled Back Inc', 'manual')")
        raise RuntimeError("boom")
    assert c.execute("SELECT count(*) FROM companies").fetchone()[0] == 0


def test_transaction_rolls_back_when_commit_fails(tmp_path: Path, opener: Any) -> None:
    c = opener(tmp_path / "x.db", "search")
    c.execute("PRAGMA defer_foreign_keys = ON")  # the FK check moves to COMMIT
    with pytest.raises(sqlite3.IntegrityError), db.transaction(c):
        c.execute(
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (999, 'Clerk', '2026-09-01', 'manual')"
        )
    assert not c.in_transaction
    assert c.execute("SELECT count(*) FROM postings").fetchone()[0] == 0


def test_read_only_connection_cannot_write(
    fresh: sqlite3.Connection, tmp_path: Path, opener: Any
) -> None:
    path = Path(fresh.execute("PRAGMA database_list").fetchone()["file"])
    ro = opener(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("CREATE TABLE t (x)")
    with pytest.raises(FileNotFoundError):
        db.connect_read_only(tmp_path / "missing.db")
