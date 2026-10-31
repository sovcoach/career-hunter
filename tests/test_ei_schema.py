"""An EI record file's schema: sanitized columns only, retention, sealing, claim period."""

from __future__ import annotations

import sqlite3
from itertools import count

import pytest

from careerhunter import enums
from careerhunter.ei_store import EI_APPLICATION_FIELDS

_ids = count(1)

# Every table and column an EI record file may have. Adding a column anywhere fails this
# test until it is added here on purpose.
ALLOWED_COLUMNS = {
    "claim": {"id", "label", "start_date", "end_date", "sealed_at", "created_at"},
    "applications": {
        "id",
        *EI_APPLICATION_FIELDS,
        "voided",
        "void_reason",
        "voided_at",
        "created_at",
    },
    "status_history": {"id", "application_id", "status", "status_date", "recorded_at"},
    "activities": {
        "id",
        "date",
        "date_precision",
        "week_of",
        "type",
        "description",
        "related_company",
        "voided",
        "void_reason",
        "voided_at",
        "created_at",
    },
    "edits": {"id", "table_name", "row_id", "column_name", "old_value", "new_value", "edited_at"},
    "schema_migrations": {"version", "name", "checksum", "applied_at"},
    "db_identity": {"id", "track", "created_at"},
}

# Learning-layer fields that must never reach the EI record, by the column names they
# have (or could plausibly be given) in search.db.
FORBIDDEN_COLUMNS = [
    "fit_rating",
    "fit",
    "gaps",
    "llm_output",
    "reasoning",
    "skip_reason",
    "reason",
    "scout_metadata",
    "notes",
    "note",
    "source",
]


def columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}


def _corr() -> str:
    return f"{next(_ids):032x}"


def _app(conn: sqlite3.Connection, **cols: object) -> int:
    values = {
        "employer": "Acme Widgets",
        "role": "Clerk",
        "location": "",
        "date_applied": "2026-09-01",
        "date_precision": "exact",
        "week_of": None,
        "method": "email",
    } | cols
    cur = conn.execute(
        "INSERT INTO applications (correlation_id, employer, role, location, date_applied, "
        "date_precision, week_of, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _corr(),
            values["employer"],
            values["role"],
            values["location"],
            values["date_applied"],
            values["date_precision"],
            values["week_of"],
            values["method"],
        ),
    )
    return int(cur.lastrowid or 0)


def _activity(conn: sqlite3.Connection, **cols: object) -> int:
    values = {"date": "2026-09-01", "date_precision": "exact", "week_of": None} | cols
    cur = conn.execute(
        "INSERT INTO activities (date, date_precision, week_of, type, description) "
        "VALUES (?, ?, ?, 'course', ?)",
        (values["date"], values["date_precision"], values["week_of"], f"Course {next(_ids)}"),
    )
    return int(cur.lastrowid or 0)


def _seal(conn: sqlite3.Connection, end: str = "2026-12-31") -> None:
    conn.execute(
        "UPDATE claim SET end_date = ?, sealed_at = '2027-01-05T00:00:00Z' WHERE id = 1", (end,)
    )


def test_exactly_the_allowed_columns(ei: sqlite3.Connection) -> None:
    assert columns(ei) == ALLOWED_COLUMNS


@pytest.mark.parametrize("column", FORBIDDEN_COLUMNS)
def test_no_learning_field_exists_in_the_schema(ei: sqlite3.Connection, column: str) -> None:
    for table, cols in columns(ei).items():
        assert column not in cols, f"{table}.{column}"
    assert "skips" not in columns(ei)


def test_claim_row_written_at_creation(ei: sqlite3.Connection) -> None:
    row = ei.execute("SELECT label, start_date, end_date, sealed_at FROM claim").fetchone()
    assert tuple(row) == ("2026-27", "2026-08-05", None, None)
    with pytest.raises(sqlite3.IntegrityError):
        ei.execute("INSERT INTO claim (id, label, start_date) VALUES (2, '2027-28', '2027-08-01')")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE claim SET label = '2027-28'",
        "UPDATE claim SET start_date = '2026-08-01'",
        "UPDATE claim SET created_at = '2000-01-01T00:00:00Z'",
    ],
)
def test_claim_identity_is_fixed(ei: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="can not be changed"):
        ei.execute(sql)


def test_claim_end_can_be_recorded_and_is_audited(ei: sqlite3.Connection) -> None:
    ei.execute("UPDATE claim SET end_date = '2027-08-04'")
    row = ei.execute("SELECT column_name, old_value, new_value FROM edits").fetchone()
    assert tuple(row) == ("end_date", None, "2027-08-04")
    with pytest.raises(sqlite3.IntegrityError):
        ei.execute("UPDATE claim SET end_date = '2026-08-01'")  # before the start


def test_end_date_cannot_cut_off_existing_records(ei: sqlite3.Connection) -> None:
    _app(ei, date_applied="2026-10-01")
    with pytest.raises(sqlite3.IntegrityError, match="records after that end date"):
        ei.execute("UPDATE claim SET end_date = '2026-09-30'")
    ei.execute("UPDATE claim SET end_date = '2026-10-01'")


@pytest.mark.parametrize(
    ("cols", "why"),
    [
        ({"method": "carrier_pigeon"}, "bad method"),
        ({"date_precision": "exact", "date_applied": None}, "exact without date"),
        ({"date_precision": "week", "date_applied": None, "week_of": "2026-09-09"}, "not Monday"),
        ({"date_precision": "unknown"}, "unknown with a date"),
        ({"date_applied": "2026-13-45"}, "invalid date"),
        ({"employer": "  "}, "blank employer"),
    ],
)
def test_application_checks(ei: sqlite3.Connection, cols: dict[str, object], why: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _app(ei, **cols)


@pytest.mark.parametrize("bad", ["short", "G" * 32, "A" * 32, "0" * 33])
def test_correlation_id_must_be_opaque_hex(ei: sqlite3.Connection, bad: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        ei.execute(
            "INSERT INTO applications (correlation_id, employer, role, date_precision, method) "
            "VALUES (?, 'A', 'B', 'unknown', 'email')",
            (bad,),
        )


def test_natural_key(ei: sqlite3.Connection) -> None:
    _app(ei, date_applied="2026-09-01")
    _app(ei, date_applied="2026-09-02")  # re-applying another day
    with pytest.raises(sqlite3.IntegrityError):
        _app(ei, date_applied="2026-09-01", employer="ACME WIDGETS")


def test_statuses_and_methods_match_python(ei: sqlite3.Connection) -> None:
    app = _app(ei)
    for status in enums.STATUSES:
        ei.execute(
            "INSERT INTO status_history (application_id, status) VALUES (?, ?)", (app, status)
        )
    with pytest.raises(sqlite3.IntegrityError):
        ei.execute(
            "INSERT INTO status_history (application_id, status) VALUES (?, 'ghosted')", (app,)
        )
    for i, method in enumerate(enums.METHODS):
        _app(ei, method=method, role=f"Role {i}")


@pytest.mark.parametrize(
    "table", ["claim", "applications", "status_history", "activities", "edits", "schema_migrations"]
)
def test_deletes_are_refused_everywhere(ei: sqlite3.Connection, table: str) -> None:
    a = _app(ei)
    ei.execute("INSERT INTO status_history (application_id, status) VALUES (?, 'applied')", (a,))
    _activity(ei)
    ei.execute("UPDATE applications SET resume_file = 'cv.pdf'")  # gives `edits` a row
    with pytest.raises(sqlite3.IntegrityError, match=r"disabled|permanent"):
        ei.execute(f"DELETE FROM {table}")  # noqa: S608 - table names are fixed above


def test_status_history_is_append_only(ei: sqlite3.Connection) -> None:
    a = _app(ei)
    ei.execute("INSERT INTO status_history (application_id, status) VALUES (?, 'applied')", (a,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ei.execute("UPDATE status_history SET status = 'offer'")


def test_correlation_id_and_identity_are_fixed(ei: sqlite3.Connection) -> None:
    a = _app(ei)
    for column, value in (("correlation_id", _corr()), ("id", 99), ("created_at", "x")):
        with pytest.raises(sqlite3.IntegrityError, match="can not be changed"):
            ei.execute(f"UPDATE applications SET {column} = ? WHERE id = ?", (value, a))  # noqa: S608


# --- Claim period: the database's own second line behind the app's check ------------


@pytest.mark.parametrize(
    ("cols", "allowed"),
    [
        ({"date_applied": "2026-08-04"}, False),  # day before the start
        ({"date_applied": "2026-08-05"}, True),  # the start day
        ({"date_precision": "week", "date_applied": None, "week_of": "2026-07-27"}, False),
        ({"date_precision": "week", "date_applied": None, "week_of": "2026-08-03"}, True),
        ({"date_precision": "unknown", "date_applied": None}, True),
    ],
)
def test_application_dates_must_fall_in_the_claim(
    ei: sqlite3.Connection, cols: dict[str, object], allowed: bool
) -> None:
    if allowed:
        _app(ei, **cols)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="outside the claim period"):
            _app(ei, **cols)


def test_claim_end_bounds_dates(ei: sqlite3.Connection) -> None:
    ei.execute("UPDATE claim SET end_date = '2027-08-04'")  # a Wednesday
    _app(ei, date_precision="week", date_applied=None, week_of="2027-08-02")  # straddles end
    with pytest.raises(sqlite3.IntegrityError, match="outside the claim period"):
        _app(ei, date_applied="2027-08-05")
    with pytest.raises(sqlite3.IntegrityError, match="outside the claim period"):
        _activity(ei, date_precision="week", date=None, week_of="2027-08-09")


def test_dates_cannot_be_moved_out_of_the_claim(ei: sqlite3.Connection) -> None:
    a = _app(ei)
    act = _activity(ei)
    with pytest.raises(sqlite3.IntegrityError, match="outside the claim period"):
        ei.execute("UPDATE applications SET date_applied = '2026-01-01' WHERE id = ?", (a,))
    with pytest.raises(sqlite3.IntegrityError, match="outside the claim period"):
        ei.execute("UPDATE activities SET date = '2026-01-01' WHERE id = ?", (act,))


def test_activity_dates_must_fall_in_the_claim(ei: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="outside the claim period"):
        _activity(ei, date="2026-08-04")
    _activity(ei, date_precision="unknown", date=None)


# --- Sealing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "write",
    [
        lambda c: _app(c),
        lambda c: c.execute("UPDATE applications SET resume_file = 'x'"),
        lambda c: c.execute(
            "INSERT INTO status_history (application_id, status) "
            "VALUES ((SELECT min(id) FROM applications), 'offer')"
        ),
        lambda c: _activity(c),
        lambda c: c.execute("UPDATE activities SET related_company = 'x'"),
        lambda c: c.execute("UPDATE claim SET end_date = '2027-01-01'"),
        lambda c: c.execute("UPDATE claim SET sealed_at = NULL"),
    ],
    ids=[
        "add application",
        "edit application",
        "add status",
        "add activity",
        "edit activity",
        "move end",
        "unseal",
    ],
)
def test_a_sealed_record_refuses_every_write(ei: sqlite3.Connection, write: object) -> None:
    _app(ei)
    _activity(ei)
    _seal(ei)
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        write(ei)  # type: ignore[operator]


def test_sealing_needs_an_end_date(ei: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        ei.execute("UPDATE claim SET sealed_at = '2027-01-05T00:00:00Z'")


def test_voided_rows_are_frozen(ei: sqlite3.Connection) -> None:
    a = _app(ei)
    ei.execute(
        "UPDATE applications SET voided = 1, void_reason = 'dup', voided_at = 'now' WHERE id = ?",
        (a,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        ei.execute("UPDATE applications SET voided = 0, void_reason = NULL, voided_at = NULL")
