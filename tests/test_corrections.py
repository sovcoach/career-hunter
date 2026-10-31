"""Voiding and the edits audit log in an EI record: corrections never destroy history."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from careerhunter import records
from careerhunter.claims import ActiveClaim
from careerhunter.export import collect_activities, collect_applications, export
from careerhunter.seed import import_seed
from tests.conftest import FIXTURES

SEPT = (dt.date(2026, 9, 1), dt.date(2026, 9, 30))


@pytest.fixture
def seeded(search: sqlite3.Connection, active: ActiveClaim) -> sqlite3.Connection:
    import_seed(search, active, FIXTURES / "seed_synthetic.yaml", today=dt.date(2026, 10, 1))
    return active.conn


def app_id(conn: sqlite3.Connection, title: str) -> int:
    return int(conn.execute("SELECT id FROM applications WHERE role = ?", (title,)).fetchone()[0])


def edits(conn: sqlite3.Connection, table: str, row_id: int) -> dict[str, tuple[str, str]]:
    return {
        r["column_name"]: (r["old_value"], r["new_value"])
        for r in conn.execute(
            "SELECT column_name, old_value, new_value FROM edits "
            "WHERE table_name = ? AND row_id = ? ORDER BY id",
            (table, row_id),
        )
    }


def test_void_application_retained_audited_and_excluded(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    acme = app_id(seeded, "Operations Coordinator")
    records.void_application(seeded, acme, "Entered twice by mistake")

    row = seeded.execute("SELECT * FROM applications WHERE id = ?", (acme,)).fetchone()
    assert (row["voided"], row["void_reason"]) == (1, "Entered twice by mistake")
    assert row["voided_at"]

    logged = edits(seeded, "applications", acme)
    assert logged["voided"] == ("0", "1")
    assert logged["void_reason"] == (None, "Entered twice by mistake")
    assert "voided_at" in logged

    employers = [r["employer"] for r in collect_applications(seeded, *SEPT)]
    assert "Acme Widgets" not in employers
    result = export(seeded, tmp_path, *SEPT, now=dt.datetime(2026, 10, 30, 9, 0))
    assert "Acme Widgets" not in result.applications_csv.read_text()
    assert result.application_count == 2


def test_void_activity_excluded_from_export(seeded: sqlite3.Connection) -> None:
    fair = int(seeded.execute("SELECT id FROM activities WHERE type = 'job_fair'").fetchone()["id"])
    records.void_activity(seeded, fair, "Did not attend after all")
    assert "job fair" not in [a["type"] for a in collect_activities(seeded, *SEPT)]
    assert seeded.execute("SELECT count(*) FROM activities").fetchone()[0] == 3


def test_voided_rows_are_frozen_and_cannot_be_unvoided(seeded: sqlite3.Connection) -> None:
    acme = app_id(seeded, "Operations Coordinator")
    records.void_application(seeded, acme, "Mistake")
    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        seeded.execute(
            "UPDATE applications SET voided = 0, void_reason = NULL, voided_at = NULL WHERE id = ?",
            (acme,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        seeded.execute("UPDATE applications SET resume_file = 'x' WHERE id = ?", (acme,))
    with pytest.raises(records.RecordError, match="already voided"):
        records.void_application(seeded, acme, "Again")


def test_void_needs_reason_and_valid_target(seeded: sqlite3.Connection) -> None:
    acme = app_id(seeded, "Operations Coordinator")
    with pytest.raises(records.RecordError, match="needs a reason"):
        records.void_application(seeded, acme, "   ")
    with pytest.raises(records.RecordError, match="no application"):
        records.void_application(seeded, 9999, "Missing")
    with pytest.raises(records.RecordError, match="can be voided"):
        records.void(seeded, "companies", 1, "Nope")
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute("UPDATE applications SET voided = 1 WHERE id = ?", (acme,))  # no reason


@pytest.mark.parametrize(
    ("sql", "table", "column", "old", "new"),
    [
        (
            "UPDATE applications SET resume_file = 'resume-v3.pdf' WHERE role = 'Data Clerk'",
            "applications",
            "resume_file",
            None,
            "resume-v3.pdf",
        ),
        (
            "UPDATE applications SET location = 'Shelbyville, ON' WHERE role = 'Data Clerk'",
            "applications",
            "location",
            "Ogdenville, ON",
            "Shelbyville, ON",
        ),
        (
            "UPDATE activities SET related_company = 'Synthetic Expo' WHERE type = 'job_fair'",
            "activities",
            "related_company",
            None,
            "Synthetic Expo",
        ),
        (
            "UPDATE claim SET end_date = '2027-08-04'",
            "claim",
            "end_date",
            None,
            "2027-08-04",
        ),
    ],
)
def test_every_permitted_update_is_audited(
    seeded: sqlite3.Connection, sql: str, table: str, column: str, old: str | None, new: str
) -> None:
    before = seeded.execute("SELECT count(*) FROM edits").fetchone()[0]
    seeded.execute(sql)
    row = seeded.execute(
        "SELECT table_name, column_name, old_value, new_value, edited_at FROM edits "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert seeded.execute("SELECT count(*) FROM edits").fetchone()[0] == before + 1
    assert (row["table_name"], row["column_name"], row["old_value"], row["new_value"]) == (
        table,
        column,
        old,
        new,
    )
    assert row["edited_at"]


def test_no_op_update_writes_nothing(seeded: sqlite3.Connection) -> None:
    seeded.execute("UPDATE applications SET employer = employer")
    assert seeded.execute("SELECT count(*) FROM edits").fetchone()[0] == 0


def test_edits_log_is_append_only(seeded: sqlite3.Connection) -> None:
    seeded.execute("UPDATE applications SET resume_file = 'cv.pdf' WHERE role = 'Data Clerk'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        seeded.execute("UPDATE edits SET new_value = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="disabled"):
        seeded.execute("DELETE FROM edits")


@pytest.mark.parametrize("table", ["applications", "activities"])
def test_identity_and_created_at_are_fixed(seeded: sqlite3.Connection, table: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="can not be changed"):
        seeded.execute(f"UPDATE {table} SET id = id + 100")  # noqa: S608
    with pytest.raises(sqlite3.IntegrityError, match="can not be changed"):
        seeded.execute(f"UPDATE {table} SET created_at = '2000-01-01T00:00:00Z'")  # noqa: S608
