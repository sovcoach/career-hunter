"""The migration runner: two tracks, checksums, and a recorded identity per file."""

from __future__ import annotations

import sqlite3
import threading
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


# --- one BEGIN IMMEDIATE transaction per run (#51) ---------------------------------------


def _old_migrate(conn: sqlite3.Connection, track: str) -> None:
    """The runner before #51: one executescript per migration. A reference only."""
    conn.executescript(
        f"BEGIN;\n{db._BOOKKEEPING}\n"
        f"INSERT INTO db_identity (id, track) VALUES (1, '{track}');\nCOMMIT;"
    )
    for m in db.load_migrations(track):
        conn.executescript(
            f"BEGIN;\n{m.sql}\n;\n"
            "INSERT INTO schema_migrations (version, name, checksum) "
            f"VALUES ({m.version}, '{m.name}', '{m.checksum}');\nCOMMIT;"
        )


def _recorded(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
    return [tuple(row) for row in rows]


def test_new_runner_builds_the_same_schema_as_the_old_one(tmp_path: Path, track: str) -> None:
    old = db.connect(tmp_path / "old.db")
    new = db.connect(tmp_path / "new.db")
    try:
        _old_migrate(old, track)
        db.migrate(new, track)
        assert _schema(new) == _schema(old)
        assert _recorded(new) == _recorded(old)
        assert db.file_track(new) == db.file_track(old) == track
    finally:
        old.close()
        new.close()


def _traced(conn: sqlite3.Connection) -> list[str]:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    return seen


def _two_migrations() -> list[db.Migration]:
    return [
        db.Migration(1, "one", "CREATE TABLE a (x INTEGER);\nCREATE INDEX ix_a ON a (x);"),
        db.Migration(2, "two", "CREATE TABLE b (y INTEGER); INSERT INTO b (y) VALUES (1);"),
    ]


def test_sql_order_is_one_transaction(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "x.db")
    try:
        seen = _traced(conn)
        assert db.migrate(conn, "search", _two_migrations()) == [1, 2]
        begins = [i for i, s in enumerate(seen) if s.startswith("BEGIN")]
        commits = [i for i, s in enumerate(seen) if s == "COMMIT"]
        assert [seen[i] for i in begins] == ["BEGIN IMMEDIATE"]
        assert commits == [len(seen) - 1]
        body = seen[begins[0] + 1 : commits[0]]
        assert "INSERT INTO db_identity (id, track) VALUES (1, 'search')" in body
        order = [
            "CREATE TABLE a (x INTEGER);",
            "CREATE INDEX ix_a ON a (x);",
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (1, 'one', ",
            "CREATE TABLE b (y INTEGER);",
            "INSERT INTO b (y) VALUES (1);",
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (2, 'two', ",
        ]
        positions = [
            next(i for i, s in enumerate(body) if s.startswith(prefix)) for prefix in order
        ]
        assert positions == sorted(positions)
        identity = body.index("INSERT INTO db_identity (id, track) VALUES (1, 'search')")
        assert identity < positions[0]
        assert not any(s.startswith(("ROLLBACK", "SAVEPOINT")) for s in seen)
    finally:
        conn.close()


def test_fully_migrated_file_takes_no_lock(fresh: sqlite3.Connection, track: str) -> None:
    seen = _traced(fresh)
    assert db.migrate(fresh, track) == []
    assert not any(s.startswith(("BEGIN", "COMMIT", "PRAGMA busy_timeout =")) for s in seen)


def test_edited_migration_is_refused_without_a_lock(fresh: sqlite3.Connection, track: str) -> None:
    original = db.load_migrations(track)[0]
    tampered = db.Migration(original.version, original.name, original.sql + "\n-- edit")
    seen = _traced(fresh)
    with pytest.raises(db.MigrationError, match="edited after it was applied"):
        db.migrate(fresh, track, [tampered])
    assert not any(s.startswith("BEGIN") for s in seen)


def test_only_pending_migrations_run(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "x.db")
    try:
        first, second = _two_migrations()
        assert db.migrate(conn, "search", [first]) == [1]
        seen = _traced(conn)
        assert db.migrate(conn, "search", [first, second]) == [2]
        assert not any(s.startswith("CREATE TABLE a") for s in seen)
    finally:
        conn.close()


@pytest.mark.parametrize("failing", ["first", "later"])
def test_a_failure_rolls_back_the_whole_run(tmp_path: Path, failing: str) -> None:
    conn = db.connect(tmp_path / "x.db")
    try:
        ok = db.Migration(1, "ok", "CREATE TABLE a (x INTEGER);")
        bad = db.Migration(2, "bad", "CREATE TABLE b (y INTEGER);\nINSERT INTO nope VALUES (1);")
        if failing == "later":
            db.migrate(conn, "search", [ok])
            fine = db.Migration(2, "fine", "CREATE TABLE c (z INTEGER);")
            bad = db.Migration(
                3, "bad", "CREATE TABLE b (y INTEGER);\nINSERT INTO nope VALUES (1);"
            )
            plan = [ok, fine, bad]
            expected = 1
        else:
            plan = [ok, bad]
            expected = 0
        with pytest.raises(sqlite3.OperationalError, match="no such table: nope"):
            db.migrate(conn, "search", plan)
        assert not conn.in_transaction
        assert db.schema_version(conn) == expected
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "b" not in names and "c" not in names
        if failing == "first":
            assert names == set()  # the identity claim was rolled back too
            assert db.file_track(conn) is None
        else:
            assert db.file_track(conn) == "search"
    finally:
        conn.close()


def test_a_connection_that_is_not_autocommit_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    db.connect(path).close()
    implicit = sqlite3.connect(path)  # the module's default: implicit BEGIN before DML
    try:
        with pytest.raises(db.MigrationError, match="autocommit connection"):
            db.migrate(implicit, "search", _two_migrations())
    finally:
        implicit.close()
    conn = db.connect(path)
    try:
        conn.execute("BEGIN")
        with pytest.raises(db.MigrationError, match="no open transaction"):
            db.migrate(conn, "search", _two_migrations())
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_lock_wait_that_runs_out_is_a_migration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.db"
    holder = db.connect(path)
    waiter = db.connect(path)
    try:
        monkeypatch.setattr(db, "MIGRATE_BUSY_TIMEOUT_MS", 0)
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(db.MigrationError, match="held the migration lock; nothing was applied"):
            db.migrate(waiter, "search", _two_migrations())
        assert not waiter.in_transaction
        assert waiter.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
        holder.execute("ROLLBACK")
        assert db.migrate(waiter, "search", _two_migrations()) == [1, 2]
    finally:
        holder.close()
        waiter.close()


_WAIT = 10.0  # a guard against a hang; a passing run never waits this long


@pytest.mark.parametrize("winner", ["first", "second"])
def test_two_connections_race_and_the_loser_applies_nothing(tmp_path: Path, winner: str) -> None:
    """The winner holds the lock until the loser is queued behind it.

    The loser read the old state before the winner committed, so it takes the slow
    path, waits for the lock, re-reads, and finds nothing pending.
    """
    path = tmp_path / "x.db"
    db.connect(path).close()
    names = ["first", "second"]
    loser = names[1 - names.index(winner)]
    winner_locked = threading.Event()
    loser_queued = threading.Event()
    results: dict[str, list[int]] = {}
    # An exception in a worker thread would otherwise vanish with the thread. It is
    # kept here, and the test fails unless both entries are None.
    errors: dict[str, BaseException | None] = {}
    traces: dict[str, list[str]] = {}

    def run(name: str) -> None:
        conn = db.connect(path)
        seen: list[str] = []
        traces[name] = seen

        def trace(statement: str) -> None:
            seen.append(statement)
            if name == winner and statement.startswith("CREATE TABLE a"):
                winner_locked.set()
                loser_queued.wait(_WAIT)
            if name == loser and statement == "BEGIN IMMEDIATE":
                loser_queued.set()

        conn.set_trace_callback(trace)
        try:
            if name == loser:
                winner_locked.wait(_WAIT)
            results[name] = db.migrate(conn, "search", _two_migrations())
            errors[name] = None
        except BaseException as exc:
            errors[name] = exc
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_WAIT)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == {"first": None, "second": None}
    assert winner_locked.is_set() and loser_queued.is_set()
    assert results[winner] == [1, 2]
    assert results[loser] == []
    assert not any(s.startswith("CREATE TABLE") for s in traces[loser])
    check = db.connect(path)
    try:
        assert _recorded(check) == [(m.version, m.name, m.checksum) for m in _two_migrations()]
    finally:
        check.close()


def test_a_process_that_arrives_after_the_commit_takes_the_fast_path(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    early = db.connect(path)
    late = db.connect(path)
    try:
        assert db.migrate(early, "search", _two_migrations()) == [1, 2]
        seen = _traced(late)
        assert db.migrate(late, "search", _two_migrations()) == []
        assert not any(s.startswith("BEGIN") for s in seen)
    finally:
        early.close()
        late.close()
