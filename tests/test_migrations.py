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
    expected = {"search": [1, 2, 3, 4, 5, 6, 7], "ei_record": [1]}[track]
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


# --- splitting migration SQL into statements -------------------------------------------


def test_split_keeps_a_one_line_trigger_body_whole() -> None:
    sql = (
        "CREATE TABLE t (x INTEGER);\n"
        "CREATE TRIGGER no_delete BEFORE DELETE ON t\n"
        "BEGIN SELECT RAISE(ABORT, 'kept'); END;\n"
    )
    assert db.split_statements(sql) == [
        "CREATE TABLE t (x INTEGER);",
        "CREATE TRIGGER no_delete BEFORE DELETE ON t\nBEGIN SELECT RAISE(ABORT, 'kept'); END;",
    ]


def test_split_keeps_a_multi_line_trigger_body_whole() -> None:
    sql = (
        "CREATE TABLE t (x INTEGER);\n"
        "CREATE TABLE log (x INTEGER);\n"
        "CREATE TRIGGER audit AFTER UPDATE ON t\n"
        "BEGIN\n"
        "    INSERT INTO log (x) VALUES (OLD.x);\n"
        "    INSERT INTO log (x) VALUES (NEW.x);\n"
        "END;\n"
    )
    statements = db.split_statements(sql)
    assert len(statements) == 3
    assert statements[2].startswith("CREATE TRIGGER audit") and statements[2].endswith("END;")


def test_split_separates_two_statements_on_one_line() -> None:
    sql = "CREATE TABLE a (x INTEGER); CREATE TABLE b (y INTEGER);"
    assert db.split_statements(sql) == [
        "CREATE TABLE a (x INTEGER);",
        "CREATE TABLE b (y INTEGER);",
    ]


def test_split_ignores_semicolons_in_comments_and_strings() -> None:
    sql = (
        "-- a note; with a semicolon\n"
        "CREATE TABLE t (x TEXT DEFAULT 'a;b', y TEXT DEFAULT 'it''s; fine');\n"
        "/* block; comment */\n"
        "INSERT INTO t (x) VALUES ('c;d'); -- trailing; note\n"
    )
    statements = db.split_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("-- a note; with a semicolon\nCREATE TABLE t")
    assert "'it''s; fine'" in statements[0]
    assert statements[1].endswith("VALUES ('c;d');")


def test_split_drops_comment_only_and_empty_pieces() -> None:
    sql = "-- header only\n;\nCREATE TABLE t (x INTEGER);;\n-- footer\n"
    assert db.split_statements(sql) == ["CREATE TABLE t (x INTEGER);"]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t (x INTEGER);\nCREATE TABLE u (y INTEGER)",
        "CREATE TRIGGER t BEFORE DELETE ON x BEGIN SELECT 1;",
        "CREATE TABLE t (x TEXT DEFAULT 'open;",
    ],
)
def test_split_refuses_an_incomplete_tail(sql: str) -> None:
    with pytest.raises(db.MigrationError, match="incomplete statement"):
        db.split_statements(sql)


def test_every_shipped_migration_splits_into_complete_statements(track: str) -> None:
    for migration in db.load_migrations(track):
        statements = db.split_statements(migration.sql)
        assert statements, migration.name
        for statement in statements:
            assert sqlite3.complete_statement(statement), (migration.name, statement[:60])


def _schema(conn: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_split_statements_build_the_same_schema_as_executescript(
    tmp_path: Path, track: str
) -> None:
    whole = sqlite3.connect(tmp_path / "whole.db", isolation_level=None)
    split = sqlite3.connect(tmp_path / "split.db", isolation_level=None)
    try:
        for migration in db.load_migrations(track):
            whole.executescript(migration.sql)
            for statement in db.split_statements(migration.sql):
                split.execute(statement)
        assert _schema(split) == _schema(whole)
    finally:
        whole.close()
        split.close()
