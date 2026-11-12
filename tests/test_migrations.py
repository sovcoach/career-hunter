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
    expected = {"search": list(range(1, 25)), "ei_record": [1, 2, 3]}[track]
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


def test_0008_keeps_existing_postings_and_leaves_them_unstamped(tmp_path: Path) -> None:
    """A schema-7 file with a scored posting moves to 8 with the new columns null."""
    conn = db.connect(tmp_path / "search.db")
    try:
        first_seven = db.load_migrations("search")[:7]
        assert db.migrate(conn, "search", first_seven) == [1, 2, 3, 4, 5, 6, 7]
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source, gaps, llm_output) "
            "VALUES (?, 'Coordinator', '2026-10-01', 'watcher', '[]', 'text')",
            (company,),
        )
        assert db.migrate(conn, "search", db.load_migrations("search")[:8]) == [8]
        row = conn.execute(
            "SELECT title, llm_output, scored_resume_sha256, score_attempted_at FROM postings"
        ).fetchone()
        assert tuple(row) == ("Coordinator", "text", None, None)
        assert conn.execute("SELECT count(*) FROM resume_import").fetchone()[0] == 0
    finally:
        conn.close()


def test_0009_keeps_scored_postings_and_leaves_their_fit_null(tmp_path: Path) -> None:
    """A schema-8 file with a scored posting moves to 9 with no fit and no runs."""
    conn = db.connect(tmp_path / "search.db")
    try:
        assert db.migrate(conn, "search", db.load_migrations("search")[:8]) == list(range(1, 9))
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source, gaps, llm_output, "
            "scout_metadata) VALUES (?, 'Coordinator', '2026-10-01', 'watcher', '[]', 'text', "
            '\'{"tier": "strong"}\')',
            (company,),
        )
        assert db.migrate(conn, "search", db.load_migrations("search")[:9]) == [9]
        row = conn.execute("SELECT title, scout_metadata, fit FROM postings").fetchone()
        assert tuple(row) == ("Coordinator", '{"tier": "strong"}', None)
        assert conn.execute("SELECT count(*) FROM watch_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_0010_leaves_every_existing_posting_unchecked(tmp_path: Path) -> None:
    """A schema-9 file moves to 10 with no lane and lane_checked 0, skipped ones too."""
    conn = db.connect(tmp_path / "search.db")
    try:
        assert db.migrate(conn, "search", db.load_migrations("search")[:9]) == list(range(1, 10))
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        for title in ("Coordinator", "Skipped Coordinator"):
            conn.execute(
                "INSERT INTO postings (company_id, title, first_seen, source) "
                "VALUES (?, ?, '2026-10-01', 'watcher')",
                (company, title),
            )
        conn.execute(
            "INSERT INTO skips (posting_id, date, reason) VALUES (2, '2026-10-01', 'manual')"
        )
        assert db.migrate(conn, "search", db.load_migrations("search")[:10]) == [10]
        rows = conn.execute("SELECT title, lane, lane_checked FROM postings ORDER BY id")
        assert [tuple(row) for row in rows] == [
            ("Coordinator", None, 0),
            ("Skipped Coordinator", None, 0),
        ]
    finally:
        conn.close()


def test_0011_leaves_existing_postings_without_requirements_or_a_run(tmp_path: Path) -> None:
    """A schema-10 file moves to 11 with no key requirements and no scoring run."""
    conn = db.connect(tmp_path / "search.db")
    try:
        assert db.migrate(conn, "search", db.load_migrations("search")[:10]) == list(range(1, 11))
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source, llm_output, fit) "
            "VALUES (?, 'Coordinator', '2026-10-01', 'watcher', '{}', 'high')",
            (company,),
        )
        assert db.migrate(conn, "search", db.load_migrations("search")[:11]) == [11]
        row = conn.execute("SELECT title, fit, key_requirements, score_run_id FROM postings")
        assert tuple(row.fetchone()) == ("Coordinator", "high", None, None)
    finally:
        conn.close()


def _at_eleven_with_discovery_rows(path: Path) -> sqlite3.Connection:
    """A schema-11 file with a board, a career page and a posting tied to a company."""
    conn = db.connect(path)
    assert db.migrate(conn, "search", db.load_migrations("search")[:11]) == list(range(1, 12))
    company = conn.execute(
        "INSERT INTO companies (name, ats_type, ats_slug, source) "
        "VALUES ('Example Widgets', 'workday', 'examplewidgets', 'watcher')"
    ).lastrowid
    conn.execute(
        "INSERT INTO postings (company_id, title, first_seen, source) "
        "VALUES (?, 'Coordinator', '2026-10-01', 'watcher')",
        (company,),
    )
    conn.execute(
        "INSERT INTO discovery_candidates (kind, url, ats_type, ats_slug, suggested_name, "
        "status, first_seen, notified_at) VALUES ('board', 'https://jobs.lever.co/examplecorp', "
        "'lever', 'examplecorp', 'examplecorp', 'pending', '2026-10-02', '2026-10-02T09:00:00Z')"
    )
    conn.execute(
        "INSERT INTO discovery_candidates (kind, url, suggested_name, status, first_seen) "
        "VALUES ('career', 'https://careers.example.org/', 'careers.example.org', "
        "'rejected', '2026-10-03')"
    )
    return conn


def test_0012_rebuilds_the_discovery_queue_and_keeps_every_row(tmp_path: Path) -> None:
    conn = _at_eleven_with_discovery_rows(tmp_path / "search.db")
    try:
        before = [tuple(r) for r in conn.execute("SELECT * FROM discovery_candidates ORDER BY id")]
        assert db.migrate(conn, "search", db.load_migrations("search")[:12]) == [12]
        after = conn.execute(
            "SELECT id, kind, url, ats_type, ats_slug, suggested_name, status, first_seen, "
            "created_at, notified_at, ats_host, ats_site FROM discovery_candidates ORDER BY id"
        )
        assert [tuple(r) for r in after] == [(*row, None, None) for row in before]
        indexes = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'discovery_candidates' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert indexes == {"ux_discovery_board", "ux_discovery_career_url"}
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'discovery_candidates_new'"
            ).fetchone()[0]
            == 0
        )
        company = conn.execute("SELECT ats_type, ats_host, ats_site FROM companies").fetchone()
        assert tuple(company) == ("workday", None, None)
    finally:
        conn.close()


def test_0012_leaves_foreign_keys_enforced_and_consistent(tmp_path: Path) -> None:
    conn = _at_eleven_with_discovery_rows(tmp_path / "search.db")
    try:
        assert db.migrate(conn, "search", db.load_migrations("search")[:12]) == [12]
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO postings (company_id, title, first_seen, source) "
                "VALUES (999, 'Orphan', '2026-10-01', 'watcher')"
            )
    finally:
        conn.close()
    reopened = db.connect(tmp_path / "search.db")
    try:
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()


def test_0012_queues_the_new_ats_types_and_a_gone_board(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:12])
        for ats, host, site in (
            ("smartrecruiters", None, None),
            ("bamboohr", None, None),
            ("workday", "wd5", "External"),
        ):
            conn.execute(
                "INSERT INTO discovery_candidates (kind, url, ats_type, ats_slug, ats_host, "
                "ats_site, suggested_name, status, first_seen) "
                "VALUES ('board', 'https://example.test/', ?, 'examplecorp', ?, ?, "
                "'examplecorp', 'gone', '2026-10-02')",
                (ats, host, site),
            )
        assert conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()[0] == 3
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("cols", "why"),
    [
        ({"ats_type": "workday"}, "a Workday board needs its host and site"),
        ({"ats_type": "workday", "ats_host": "wd5"}, "a Workday board needs its site"),
        ({"ats_type": "workday", "ats_host": "wdx", "ats_site": "S"}, "the host is wd and digits"),
        ({"ats_type": "workday", "ats_host": "wd5x", "ats_site": "S"}, "digits only after wd"),
        ({"ats_type": "lever", "ats_host": "wd5"}, "only Workday has a host"),
        ({"ats_type": "lever", "ats_site": "S"}, "only Workday has a site"),
        ({"ats_type": "icims"}, "not a board the watcher reads"),
        ({"kind": "career", "ats_type": None, "status": "gone"}, "a career page is never gone"),
        ({"status": "lost"}, "not a status"),
    ],
)
def test_0012_discovery_rows_are_checked(tmp_path: Path, cols: dict[str, object], why: str) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:12])
        row: dict[str, object] = {
            "kind": "board",
            "url": "https://example.test/",
            "ats_type": "lever",
            "ats_slug": "examplecorp",
            "ats_host": None,
            "ats_site": None,
            "suggested_name": "examplecorp",
            "status": "pending",
            "first_seen": "2026-10-02",
        }
        row.update(cols)
        if row["kind"] == "career":
            row["ats_slug"] = None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_candidates (kind, url, ats_type, ats_slug, ats_host, "
                "ats_site, suggested_name, status, first_seen) VALUES (:kind, :url, :ats_type, "
                ":ats_slug, :ats_host, :ats_site, :suggested_name, :status, :first_seen)",
                row,
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("ats", "host", "site", "ok"),
    [
        ("workday", "wd5", "External", True),
        ("workday", "wd12", "Careers_Site", True),
        ("workday", None, None, True),  # stored before 0012, or not filled in yet
        ("workday", "wd", "External", False),
        ("workday", None, "External", False),
        ("greenhouse", "wd5", None, False),
        ("greenhouse", None, "External", False),
        ("workday", "wd5", " ", False),
    ],
)
def test_0012_company_workday_identity_is_checked(
    tmp_path: Path, ats: str, host: str | None, site: str | None, ok: bool
) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:12])
        insert = (
            "INSERT INTO companies (name, ats_type, ats_slug, ats_host, ats_site, source) "
            "VALUES ('Example Widgets', ?, 'examplewidgets', ?, ?, 'watcher')"
        )
        if ok:
            conn.execute(insert, (ats, host, site))
        else:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, (ats, host, site))
    finally:
        conn.close()


def test_0013_leaves_existing_postings_without_a_posted_date(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:12])
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (?, 'Coordinator', '2026-10-01', 'watcher')",
            (company,),
        )
        assert db.migrate(conn, "search", db.load_migrations("search")[:13]) == [13]
        assert conn.execute("SELECT posted_date FROM postings").fetchone()[0] is None
        conn.execute("UPDATE postings SET posted_date = '2026-10-30'")
        for bad in ("2026-10-30T10:00:00Z", "2026-13-01", "yesterday"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE postings SET posted_date = ?", (bad,))
    finally:
        conn.close()


def test_0014_starts_discovery_at_the_first_query_with_no_backoff(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:14])
        row = conn.execute("SELECT * FROM discovery_state").fetchall()
        assert [tuple(r) for r in row] == [(1, 0, None, 0, None)]
        for sql in (
            "INSERT INTO discovery_state (id) VALUES (2)",
            "UPDATE discovery_state SET cursor = -1",
            "UPDATE discovery_state SET failures = 1",  # a failure needs its backoff time
            "UPDATE discovery_state SET backoff_until = '2026-11-01T00:00:00Z'",
            "UPDATE discovery_state SET last_pass_at = '2026-11-01'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    finally:
        conn.close()


def test_0015_keeps_one_posting_per_ats_key(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:15])
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        for title in ("One", "Two"):
            conn.execute(
                "INSERT INTO postings (company_id, title, first_seen, source) "
                "VALUES (?, ?, '2026-10-30', 'watcher')",
                (company, title),
            )
        assert (
            conn.execute("SELECT count(*) FROM postings WHERE ats_key IS NULL").fetchone()[0] == 2
        )
        conn.execute(
            "UPDATE postings SET ats_key = 'greenhouse:examplewidgets:1' WHERE title = 'One'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE postings SET ats_key = 'greenhouse:examplewidgets:1' WHERE title = 'Two'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE postings SET ats_key = '' WHERE title = 'Two'")
    finally:
        conn.close()


def test_0016_leaves_every_existing_skip_to_the_watcher(tmp_path: Path) -> None:
    """No fill: a skip stored before 0016 is never taken for the operator's."""
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:15])
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source, alerted_at) "
            "VALUES (?, 'Coordinator', '2026-10-30', 'watcher', '2026-10-30T09:00:00Z')",
            (company,),
        )
        for reason in ("not a fit", "seniority", "too_old"):
            conn.execute(
                "INSERT INTO skips (posting_id, date, reason) VALUES (1, '2026-10-30', ?)",
                (reason,),
            )
        assert db.migrate(conn, "search", db.load_migrations("search")[:16]) == [16]
        assert [r[0] for r in conn.execute("SELECT by_operator FROM skips")] == [0, 0, 0]
        conn.execute(
            "INSERT INTO skips (posting_id, date, reason, by_operator) "
            "VALUES (1, '2026-10-31', 'any free text at all', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE skips SET by_operator = 2")
    finally:
        conn.close()


def test_0017_counts_refusals_per_posting(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:17])
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (?, 'Coordinator', '2026-10-30', 'watcher')",
            (company,),
        )
        conn.execute(
            "INSERT INTO alert_refusals (posting_id, refusals, last_refused_at) "
            "VALUES (1, 1, '2026-10-30T09:00:00Z')"
        )
        for sql in (
            "INSERT INTO alert_refusals (posting_id, refusals, last_refused_at) "
            "VALUES (1, 1, '2026-10-30T09:00:00Z')",  # one row per posting
            "INSERT INTO alert_refusals (posting_id, refusals, last_refused_at) "
            "VALUES (999, 1, '2026-10-30T09:00:00Z')",  # a posting that exists
            "UPDATE alert_refusals SET refusals = 0",
            "UPDATE alert_refusals SET last_refused_at = '2026-10-30'",
            "UPDATE alert_refusals SET given_up_at = 'soon'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    finally:
        conn.close()


def test_0018_adds_health_and_leaves_earlier_runs_unknown(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:17])
        conn.execute(
            "INSERT INTO watch_runs (started_at, finished_at, boards_fetched, boards_failed, "
            "boards_left, held_unscored, held_no_fit, held_off_lane) "
            "VALUES ('2026-11-01T10:00:00Z', '2026-11-01T10:05:00Z', 1, 0, 0, 0, 0, 0)"
        )
        conn.execute("INSERT INTO watch_runs (started_at) VALUES ('2026-11-01T11:00:00Z')")
        assert db.migrate(conn, "search", db.load_migrations("search")[:18]) == [18]
        assert [r[0] for r in conn.execute("SELECT useful FROM watch_runs")] == [None, None]
        assert tuple(
            conn.execute("SELECT useful_at, unproductive FROM discovery_state").fetchone()
        ) == (
            None,
            0,
        )
        rows = conn.execute(
            "SELECT component, failures, last_failure, alerted_failures, recovery_due "
            "FROM component_health ORDER BY component"
        )
        assert [tuple(r) for r in rows] == [
            ("boards", 0, None, None, 0),
            ("lm_studio", 0, None, None, 0),
            ("searxng", 0, None, None, 0),
        ]
        conn.execute("UPDATE watch_runs SET useful = 1 WHERE id = 1")
        for sql in (
            "UPDATE watch_runs SET useful = 1 WHERE id = 2",  # an unfinished run has none
            "UPDATE watch_runs SET useful = 2 WHERE id = 1",
            "UPDATE discovery_state SET unproductive = -1",
            "INSERT INTO component_health (component) VALUES ('embedding')",
            "UPDATE component_health SET failures = 1",  # a failure names its kind
            "UPDATE component_health SET last_failure = 'unreachable'",  # and needs a count
            "UPDATE component_health SET failures = 1, last_failure = 'gone'",
            "UPDATE component_health SET alerted_failures = 0",
            "UPDATE component_health SET recovery_due = 2",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    finally:
        conn.close()


def test_0019_clears_only_workday_keys_in_the_old_path_form(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:18])
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'watcher')"
        ).lastrowid
        keys = {
            "Old": "workday:examplewidgets:wd5:external:/job/Example-City/Clerk_JR00001",
            "New": "workday:examplewidgets:wd5:external:JR00002",
            "Other": "greenhouse:examplewidgets:1",
        }
        for title, key in keys.items():
            conn.execute(
                "INSERT INTO postings (company_id, title, first_seen, source, ats_key) "
                "VALUES (?, ?, '2026-10-30', 'watcher', ?)",
                (company, title, key),
            )
        assert db.migrate(conn, "search", db.load_migrations("search")[:19]) == [19]
        rows = dict(conn.execute("SELECT title, ats_key FROM postings").fetchall())
        assert rows == {"Old": None, "New": keys["New"], "Other": keys["Other"]}
    finally:
        conn.close()


def _ei_at_0001(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    db.migrate(conn, "ei_record", db.load_migrations("ei_record")[:1])
    conn.execute("INSERT INTO claim (id, label, start_date) VALUES (1, '2026-27', '2026-09-01')")
    for i, voided in ((1, 0), (2, 1)):
        conn.execute(
            "INSERT INTO applications (correlation_id, employer, role, date_applied, "
            "date_precision, method) VALUES (?, 'Example Robotics', ?, '2026-09-02', 'exact', "
            "'email')",
            (f"{i:032x}", f"Analyst {i}"),
        )
        if voided:
            conn.execute(
                "UPDATE applications SET voided = 1, void_reason = 'duplicate', "
                "voided_at = '2026-09-03T00:00:00Z' WHERE id = ?",
                (i,),
            )
    return conn


def test_ei_0002_marks_every_existing_row_pre_feature_past_a_voided_one(tmp_path: Path) -> None:
    conn = _ei_at_0001(tmp_path / "ei.db")
    try:
        edits = conn.execute("SELECT count(*) FROM edits").fetchone()[0]
        assert db.migrate(conn, "ei_record") == list(range(2, db.latest_version("ei_record") + 1))
        rows = conn.execute("SELECT jd_expected FROM applications ORDER BY id").fetchall()
        assert [r[0] for r in rows] == [0, 0]
        assert conn.execute("SELECT count(*) FROM edits").fetchone()[0] == edits  # no UPDATE
        assert conn.execute("SELECT count(*) FROM job_descriptions").fetchone()[0] == 0
    finally:
        conn.close()


def test_ei_store_reads_a_file_still_at_0001(tmp_path: Path) -> None:
    from careerhunter import ei_store

    conn = _ei_at_0001(tmp_path / "ei.db")
    try:
        assert not ei_store.has_job_descriptions(conn)
        assert ei_store.current_job_description(conn, f"{1:032x}") is None
        assert ei_store.current_job_descriptions(conn) == {}
        assert ei_store.jd_expected(conn) == {f"{1:032x}": 0, f"{2:032x}": 0}
    finally:
        conn.close()


def test_search_0020_marks_every_existing_application_pre_feature(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:19])
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'manual')"
        ).lastrowid
        posting = conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (?, 'Clerk', '2026-10-30', 'manual')",
            (company,),
        ).lastrowid
        conn.execute(
            "INSERT INTO applications (correlation_id, posting_id, date_applied, date_precision, "
            "method) VALUES (?, ?, '2026-10-30', 'exact', 'email')",
            ("a" * 32, posting),
        )
        assert db.migrate(conn, "search") == list(range(20, db.latest_version("search") + 1))
        row = conn.execute("SELECT jd_expected, jd_sha256, jd_path FROM applications").fetchone()
        assert tuple(row) == (0, None, None)
        with pytest.raises(sqlite3.IntegrityError, match="jd_expected"):
            conn.execute("UPDATE applications SET jd_expected = 1")
        for sql in (
            "UPDATE applications SET jd_sha256 = 'AB'",
            "UPDATE applications SET jd_path = '/abs/x.txt'",
            "UPDATE applications SET jd_path = 'snapshots/../x.txt'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.execute(
            "UPDATE applications SET jd_sha256 = ?, jd_path = 'snapshots/2026/ab.txt'",
            ("ab" * 32,),
        )
    finally:
        conn.close()


def test_search_0021_lets_a_checked_lane_move_and_logs_it(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", db.load_migrations("search")[:20])
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'manual')"
        ).lastrowid
        checked, unchecked = (
            conn.execute(
                "INSERT INTO postings (company_id, title, first_seen, source) "
                "VALUES (?, ?, '2026-10-30', 'manual')",
                (company, title),
            ).lastrowid
            for title in ("Clerk", "Courier")
        )
        conn.execute("UPDATE postings SET lane = 'ops', lane_checked = 1 WHERE id = ?", (checked,))
        with pytest.raises(sqlite3.IntegrityError, match="set once"):
            conn.execute("UPDATE postings SET lane = 'support' WHERE id = ?", (checked,))
        assert db.migrate(conn, "search") == list(range(21, db.latest_version("search") + 1))
        rows = conn.execute("SELECT id, lane, lane_checked FROM postings ORDER BY id")
        assert [tuple(r) for r in rows] == [(checked, "ops", 1), (unchecked, None, 0)]
        conn.execute("UPDATE postings SET lane = 'support' WHERE id = ?", (checked,))
        changes = conn.execute("SELECT posting_id, old_lane, new_lane FROM lane_changes")
        assert [tuple(r) for r in changes] == [(checked, "ops", "support")]
        with pytest.raises(sqlite3.IntegrityError, match="lane_checked stays 1"):
            conn.execute(
                "UPDATE postings SET lane = NULL, lane_checked = 0 WHERE id = ?", (checked,)
            )
    finally:
        conn.close()


def test_trigger_counts_on_a_fresh_file(fresh: sqlite3.Connection, track: str) -> None:
    """The counts ARCHITECTURE states for a fresh file."""
    found = fresh.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()
    assert found[0] == {"search": 9, "ei_record": 41}[track]
