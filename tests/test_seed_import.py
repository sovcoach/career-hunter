from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from careerhunter.seed import SeedError, import_rows, import_seed, monday_of, parse
from tests.conftest import FIXTURES

SEED = FIXTURES / "seed_synthetic.yaml"
TODAY = dt.date(2026, 10, 1)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("companies", "postings", "applications", "status_history", "activities")
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608


def test_import_inserts_everything(conn: sqlite3.Connection) -> None:
    result = import_seed(conn, SEED, today=TODAY)
    assert counts(conn) == {
        "companies": 4,
        "postings": 4,
        "applications": 4,
        "status_history": 4,
        "activities": 3,
    }
    assert result.total_inserted == 19
    assert result.existing == {}


def test_reimport_never_duplicates(conn: sqlite3.Connection) -> None:
    import_seed(conn, SEED, today=TODAY)
    before = counts(conn)
    again = import_seed(conn, SEED, today=TODAY)
    assert again.total_inserted == 0
    assert again.existing["applications"] == 4
    assert again.existing["companies"] == 4
    assert counts(conn) == before


def test_each_application_gets_one_applied_status(conn: sqlite3.Connection) -> None:
    import_seed(conn, SEED, today=TODAY)
    import_seed(conn, SEED, today=TODAY)
    rows = conn.execute(
        "SELECT a.date_precision, s.status, s.status_date FROM applications a "
        "JOIN status_history s ON s.application_id = a.id ORDER BY a.id"
    ).fetchall()
    assert [r["status"] for r in rows] == ["applied"] * 4
    assert [r["status_date"] for r in rows] == ["2026-09-15", "2026-09-07", None, "2026-07-01"]


def test_normalization(conn: sqlite3.Connection) -> None:
    import_seed(conn, SEED, today=TODAY)
    globex = conn.execute(
        "SELECT a.method, a.week_of, p.first_seen FROM applications a "
        "JOIN postings p ON p.id = a.posting_id WHERE p.title = 'Customer Success Specialist'"
    ).fetchone()
    assert globex["method"] == "linkedin_easy_apply"
    assert globex["week_of"] == "2026-09-07"  # Wednesday 09-09 -> Monday 09-07
    assert globex["first_seen"] == "2026-09-07"
    undated = conn.execute(
        "SELECT p.first_seen FROM postings p WHERE p.title = 'Project Coordinator'"
    ).fetchone()
    assert undated["first_seen"] == TODAY.isoformat()
    acme = conn.execute("SELECT watchlist, ats_type FROM companies WHERE name = 'acme widgets'")
    assert tuple(acme.fetchone()) == (1, "greenhouse")


def test_company_match_is_case_insensitive(conn: sqlite3.Connection) -> None:
    rows = parse(
        {
            "applications": [
                {
                    "company": "ACME WIDGETS",
                    "title": "Clerk",
                    "method": "email",
                    "date_precision": "unknown",
                }
            ]
        },
        TODAY,
    )
    import_seed(conn, SEED, today=TODAY)
    import_rows(conn, rows)
    assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == 4


def test_validation_collects_all_problems_and_writes_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    bad = {
        "applications": [
            {
                "company": "Acme Widgets",
                "title": "Clerk",
                "method": "fax",
                "date_precision": "exact",
            },
            {"title": "No Company", "method": "email", "date_precision": "week"},
            {
                "company": "X",
                "title": "Y",
                "method": "email",
                "date_precision": "unknown",
                "date_applied": "2026-09-01",
            },
        ],
        "activities": [{"type": "party", "date_precision": "exact", "date": "2026-02-30"}],
        "surprise": [],
    }
    path = tmp_path / "seed.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(SeedError) as exc:
        import_seed(conn, path, today=TODAY)
    text = str(exc.value)
    for expected in (
        "'fax'",
        "exact needs date_applied",
        "company is required",
        "week needs week_of",
        "unknown must not have",
        "'party'",
        "not a YYYY-MM-DD",
        "description is required",
        "unknown top-level",
    ):
        assert expected in text
    assert counts(conn)["companies"] == 0


def _app(**fields: object) -> dict[str, object]:
    return {
        "company": "A Co",
        "title": "Analyst",
        "location": "Springfield, ON",
        "method": "email",
        "date_precision": "exact",
        "date_applied": "2026-03-10",
    } | fields


def test_url_owned_by_another_company_rolls_back(conn: sqlite3.Connection) -> None:
    rows = parse(
        {
            "applications": [
                _app(url="https://shared.example.com/1"),
                _app(company="B Co", url="https://shared.example.com/1"),
            ]
        },
        TODAY,
    )
    with pytest.raises(SeedError, match="different company"):
        import_rows(conn, rows)
    assert counts(conn)["companies"] == 0


def test_repost_without_url_is_a_new_posting(conn: sqlite3.Connection) -> None:
    """Same role re-posted months later and re-applied to: two postings, two applications."""
    rows = parse(
        {
            "applications": [
                _app(date_applied="2026-03-10"),
                _app(date_applied="2026-07-15"),
            ]
        },
        TODAY,
    )
    import_rows(conn, rows)
    import_rows(conn, rows)
    assert counts(conn)["postings"] == 2
    assert counts(conn)["applications"] == 2


def test_reapply_to_same_url_on_another_date(conn: sqlite3.Connection) -> None:
    """Same posting (same url) applied to twice: one posting, two applications."""
    url = "https://a.example.com/jobs/42"
    rows = parse(
        {
            "applications": [
                _app(url=url, date_applied="2026-03-10"),
                _app(url=url, date_applied="2026-07-15"),
            ]
        },
        TODAY,
    )
    first = import_rows(conn, rows)
    assert first.inserted == {"companies": 1, "postings": 1, "applications": 2, "status_history": 2}
    again = import_rows(conn, rows)
    assert again.total_inserted == 0
    assert again.existing == {"companies": 1, "postings": 1, "applications": 2}


def test_requisition_id_identifies_posting(conn: sqlite3.Connection) -> None:
    rows = parse(
        {
            "applications": [
                _app(requisition_id="R-100", date_applied="2026-03-10"),
                # Re-posted in July with the same requisition id: still the same posting.
                _app(requisition_id="R-100", date_applied="2026-07-15"),
                # Different requisition, same title and month: a different posting.
                _app(requisition_id="R-200", date_applied="2026-07-16"),
            ]
        },
        TODAY,
    )
    import_rows(conn, rows)
    import_rows(conn, rows)
    assert counts(conn)["postings"] == 2
    assert counts(conn)["applications"] == 3


def test_undated_entry_not_duplicated_in_a_later_month(conn: sqlite3.Connection) -> None:
    rows_october = parse(
        {"applications": [_app(date_precision="unknown", date_applied=None)]}, dt.date(2026, 10, 1)
    )
    rows_december = parse(
        {"applications": [_app(date_precision="unknown", date_applied=None)]}, dt.date(2026, 12, 1)
    )
    import_rows(conn, rows_october)
    assert import_rows(conn, rows_december).total_inserted == 0
    assert counts(conn)["postings"] == 1


def test_missing_seed_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="not found"):
        import_seed(conn, tmp_path / "seed.yaml")


def test_example_template_is_valid(conn: sqlite3.Connection, tmp_path: Path) -> None:
    template = Path(__file__).parents[1] / "seed.example.yaml"
    shutil.copyfile(template, tmp_path / "seed.yaml")
    assert import_seed(conn, tmp_path / "seed.yaml", today=TODAY).inserted["applications"] == 3


def test_monday_of() -> None:
    assert monday_of(dt.date(2026, 9, 13)) == dt.date(2026, 9, 7)  # Sunday
    assert monday_of(dt.date(2026, 9, 7)) == dt.date(2026, 9, 7)
