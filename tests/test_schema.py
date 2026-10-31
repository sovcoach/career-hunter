from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careerhunter import db


def _company(conn: sqlite3.Connection, name: str = "Acme Widgets") -> int:
    cur = conn.execute("INSERT INTO companies (name, source) VALUES (?, 'manual')", (name,))
    return int(cur.lastrowid or 0)


def _posting(conn: sqlite3.Connection, company_id: int, title: str = "Clerk") -> int:
    cur = conn.execute(
        "INSERT INTO postings (company_id, title, first_seen, source) "
        "VALUES (?, ?, '2026-09-01', 'manual')",
        (company_id, title),
    )
    return int(cur.lastrowid or 0)


def _application(conn: sqlite3.Connection, posting_id: int, **cols: object) -> int:
    values = {
        "date_applied": "2026-09-01",
        "date_precision": "exact",
        "week_of": None,
        "method": "email",
    } | cols
    cur = conn.execute(
        "INSERT INTO applications (posting_id, date_applied, date_precision, week_of, method) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            posting_id,
            values["date_applied"],
            values["date_precision"],
            values["week_of"],
            values["method"],
        ),
    )
    return int(cur.lastrowid or 0)


def test_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_all_tables_exist_and_version(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "companies",
        "postings",
        "applications",
        "status_history",
        "activities",
        "skips",
        "edits",
        "schema_migrations",
    } <= tables
    assert db.schema_version(conn) == 1


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    assert db.migrate(conn) == []


def test_edited_migration_is_refused(conn: sqlite3.Connection) -> None:
    original = db.load_migrations()[0]
    tampered = db.Migration(original.version, original.name, original.sql + "\n-- edit")
    with pytest.raises(db.MigrationError, match="edited after it was applied"):
        db.migrate(conn, [tampered])


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "x.db")
    bad = db.Migration(1, "bad", "CREATE TABLE t (x INTEGER); INSERT INTO nope VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(c, [bad])
    assert db.schema_version(c) == 0
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='t'").fetchone()[0] == 0
    c.close()


def test_packaged_migrations_are_contiguous() -> None:
    assert [m.version for m in db.load_migrations()] == [1]


@pytest.mark.parametrize(
    ("cols", "why"),
    [
        ({"method": "carrier_pigeon"}, "bad method"),
        ({"date_precision": "exact", "date_applied": None}, "exact without date"),
        ({"date_precision": "week", "date_applied": None, "week_of": "2026-09-09"}, "not Monday"),
        ({"date_precision": "week", "date_applied": None, "week_of": None}, "week without week_of"),
        ({"date_precision": "unknown"}, "unknown with a date"),
        ({"date_applied": "2026-13-45"}, "invalid date"),
        ({"date_precision": "sometime"}, "bad precision"),
    ],
)
def test_application_checks(conn: sqlite3.Connection, cols: dict[str, object], why: str) -> None:
    posting = _posting(conn, _company(conn))
    with pytest.raises(sqlite3.IntegrityError):
        _application(conn, posting, **cols)


def test_application_precisions_accepted(conn: sqlite3.Connection) -> None:
    c = _company(conn)
    _application(conn, _posting(conn, c, "A"))
    _application(
        conn, _posting(conn, c, "B"), date_precision="week", date_applied=None, week_of="2026-09-07"
    )
    _application(conn, _posting(conn, c, "C"), date_precision="unknown", date_applied=None)


def test_reapplying_to_a_posting_on_another_date(conn: sqlite3.Connection) -> None:
    posting = _posting(conn, _company(conn))
    _application(conn, posting, date_applied="2026-03-10")
    _application(conn, posting, date_applied="2026-07-15")
    with pytest.raises(sqlite3.IntegrityError):
        _application(conn, posting, date_applied="2026-07-15")


def _raw_posting(conn: sqlite3.Connection, company_id: int, **cols: object) -> None:
    values = {
        "title": "Clerk",
        "location": "",
        "url": None,
        "requisition_id": None,
        "first_seen": "2026-03-02",
    } | cols
    conn.execute(
        "INSERT INTO postings (company_id, title, location, url, requisition_id, first_seen, "
        "source) VALUES (:c, :title, :location, :url, :requisition_id, :first_seen, 'manual')",
        {"c": company_id, **values},
    )


def test_posting_fallback_key_is_per_month(conn: sqlite3.Connection) -> None:
    c = _company(conn)
    _raw_posting(conn, c, first_seen="2026-03-02")
    with pytest.raises(sqlite3.IntegrityError):
        _raw_posting(conn, c, first_seen="2026-03-28", title="CLERK")  # same month, any case
    _raw_posting(conn, c, first_seen="2026-07-01")  # re-posted later: a new posting


def test_posting_url_and_requisition_keys(conn: sqlite3.Connection) -> None:
    c = _company(conn)
    other = _company(conn, "Globex Corporation")
    _raw_posting(conn, c, url="https://acme.example.com/jobs/1")
    _raw_posting(conn, c, url="https://acme.example.com/jobs/2")  # same title, other url
    with pytest.raises(sqlite3.IntegrityError):
        _raw_posting(conn, c, url="https://acme.example.com/jobs/1", title="Other")
    _raw_posting(conn, c, requisition_id="REQ-7")
    _raw_posting(conn, other, requisition_id="REQ-7")  # requisition ids are per company
    with pytest.raises(sqlite3.IntegrityError):
        _raw_posting(conn, c, requisition_id="REQ-7", first_seen="2026-08-01")


@pytest.mark.parametrize(
    "ats",
    [
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "successfactors",
        "icims",
        "taleo",
        "smartrecruiters",
        "jobvite",
        "bamboohr",
        "other",
    ],
)
def test_ats_types_accepted(conn: sqlite3.Connection, ats: str) -> None:
    conn.execute("INSERT INTO companies (name, source, ats_type) VALUES ('X', 'manual', ?)", (ats,))


@pytest.mark.parametrize(
    "method",
    [
        "company_site",
        "linkedin_easy_apply",
        "email",
        "referral",
        "agency",
        "recruiter_inbound",
        "other",
    ],
)
def test_methods_accepted(conn: sqlite3.Connection, method: str) -> None:
    _application(conn, _posting(conn, _company(conn)), method=method)


def test_schema_enums_match_python(conn: sqlite3.Connection) -> None:
    from careerhunter import enums

    for ats in enums.ATS_TYPES:
        conn.execute(
            "INSERT INTO companies (name, source, ats_type) VALUES (?, 'manual', ?)",
            (f"Co {ats}", ats),
        )
    c = _company(conn, "Method Co")
    for i, method in enumerate(enums.METHODS):
        _application(conn, _posting(conn, c, f"Role {i}"), method=method)


def test_company_names_unique_case_insensitively(conn: sqlite3.Connection) -> None:
    _company(conn, "Acme Widgets")
    with pytest.raises(sqlite3.IntegrityError):
        _company(conn, "ACME widgets")


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("INSERT INTO companies (name, source) VALUES ('X', 'linkedin')", ()),
        ("INSERT INTO companies (name, source, ats_type) VALUES ('X', 'manual', 'taleo9')", ()),
        ("INSERT INTO companies (name, source, watchlist) VALUES ('X', 'manual', 2)", ()),
        ("INSERT INTO companies (name, source) VALUES ('  ', 'manual')", ()),
        (
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (999, 'T', '2026-09-01', 'manual')",
            (),
        ),
        (
            "INSERT INTO postings (company_id, title, first_seen, source, work_model) "
            "VALUES (:c, 'T', '2026-09-01', 'manual', 'office')",
            ("c",),
        ),
        (
            "INSERT INTO postings (company_id, title, first_seen, source, fit_rating) "
            "VALUES (:c, 'T', '2026-09-01', 'manual', 11)",
            ("c",),
        ),
        (
            "INSERT INTO postings (company_id, title, first_seen, source, snapshot_path) "
            "VALUES (:c, 'T', '2026-09-01', 'manual', '/Volumes/x/a.html')",
            ("c",),
        ),
        (
            "INSERT INTO activities (date_precision, type, description) "
            "VALUES ('unknown', 'party', 'x')",
            (),
        ),
        (
            "INSERT INTO activities (date_precision, date, type, description) "
            "VALUES ('week', '2026-09-07', 'course', 'x')",
            (),
        ),
        ("INSERT INTO skips (posting_id, date, reason) VALUES (999, '2026-09-01', 'x')", ()),
    ],
)
def test_constraints_reject(conn: sqlite3.Connection, sql: str, params: tuple[str, ...]) -> None:
    c = _company(conn, "Base Co")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, {"c": c} if params else ())


def test_status_values_checked(conn: sqlite3.Connection) -> None:
    app = _application(conn, _posting(conn, _company(conn)))
    conn.execute(
        "INSERT INTO status_history (application_id, status, status_date) "
        "VALUES (?, 'screening', '2026-09-02')",
        (app,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO status_history (application_id, status) VALUES (?, 'ghosted')", (app,)
        )


@pytest.mark.parametrize(
    "table",
    [
        "companies",
        "postings",
        "applications",
        "status_history",
        "activities",
        "skips",
        "edits",
        "schema_migrations",
    ],
)
def test_deletes_are_refused_everywhere(conn: sqlite3.Connection, table: str) -> None:
    c = _company(conn)
    p = _posting(conn, c)
    a = _application(conn, p)
    conn.execute(
        "INSERT INTO status_history (application_id, status, status_date) "
        "VALUES (?, 'applied', '2026-09-01')",
        (a,),
    )
    conn.execute(
        "INSERT INTO activities (date_precision, type, description) "
        "VALUES ('unknown', 'course', 'Synthetic course')"
    )
    conn.execute("INSERT INTO skips (posting_id, date, reason) VALUES (?, '2026-09-01', 'x')", (p,))
    conn.execute("UPDATE companies SET watchlist = 1")  # gives `edits` a row to protect
    with pytest.raises(sqlite3.IntegrityError, match=r"disabled|permanent"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 - table names are fixed above


def test_status_history_is_append_only(conn: sqlite3.Connection) -> None:
    a = _application(conn, _posting(conn, _company(conn)))
    conn.execute(
        "INSERT INTO status_history (application_id, status, status_date) "
        "VALUES (?, 'applied', '2026-09-01')",
        (a,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE status_history SET status = 'offer'")


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), db.transaction(conn):
        _company(conn, "Rolled Back Inc")
        raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == 0
