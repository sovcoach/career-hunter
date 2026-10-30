from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from careerhunter.export import (
    ACTIVITY_COLUMNS,
    APPLICATION_COLUMNS,
    collect_activities,
    collect_applications,
    csv_safe,
    export,
)
from careerhunter.seed import import_seed
from tests.conftest import FIXTURES

NOW = dt.datetime(2026, 10, 30, 9, 30, 0)
SEPT = (dt.date(2026, 9, 1), dt.date(2026, 9, 30))


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    import_seed(conn, FIXTURES / "seed_synthetic.yaml", today=dt.date(2026, 10, 1))
    acme = conn.execute(
        "SELECT a.id FROM applications a JOIN postings p ON p.id = a.posting_id "
        "WHERE p.title = 'Operations Coordinator'"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO status_history (application_id, status, status_date, note) "
        "VALUES (?, 'screening', '2026-09-22', 'phone screen')",
        (acme,),
    )
    conn.execute(
        "INSERT INTO status_history (application_id, status, status_date) "
        "VALUES (?, 'interview', '2026-09-29')",
        (acme,),
    )
    return conn


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_export_writes_three_files(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    result = export(seeded, tmp_path, *SEPT, now=NOW)
    assert result.pdf.name == "ei_2026-09-01_2026-09-30_20261030T093000.pdf"
    assert result.pdf.read_bytes().startswith(b"%PDF")
    assert result.application_count == 3  # July application is out of range
    assert result.activity_count == 3

    apps = read_csv(result.applications_csv)
    assert list(apps[0].keys()) == list(APPLICATION_COLUMNS)
    assert [a["employer"] for a in apps] == ["Globex Corporation", "Acme Widgets", "Initech"]

    globex, acme, initech = apps
    assert globex["date_applied"] == "week of 2026-09-07"
    assert globex["method"] == "LinkedIn Easy Apply"
    assert globex["current_status"] == "applied"
    assert acme["date_applied"] == "2026-09-15"
    assert acme["date_precision"] == "exact"
    assert acme["current_status"] == "interview"
    assert acme["status_history"] == (
        "2026-09-15 applied (imported from seed); 2026-09-22 screening (phone screen); "
        "2026-09-29 interview"
    )
    assert acme["posting_url"] == "https://acme.example.com/careers/1001"
    assert initech["date_applied"] == "unknown"
    assert initech["status_history"].startswith("date unknown applied")

    acts = read_csv(result.activities_csv)
    assert list(acts[0].keys()) == list(ACTIVITY_COLUMNS)
    assert [a["type"] for a in acts] == ["agency registration", "job fair", "course"]
    assert acts[0]["date"] == "week of 2026-09-14"


def test_week_overlapping_range_edge_is_included(seeded: sqlite3.Connection) -> None:
    # Globex week of Mon 2026-09-07 runs to Sun 09-13; a range starting 09-13 still overlaps.
    rows = collect_applications(seeded, dt.date(2026, 9, 13), dt.date(2026, 9, 14))
    assert "Globex Corporation" in [r["employer"] for r in rows]
    rows = collect_applications(seeded, dt.date(2026, 9, 14), dt.date(2026, 9, 14))
    assert "Globex Corporation" not in [r["employer"] for r in rows]


def test_unknown_dates_always_listed(seeded: sqlite3.Connection) -> None:
    rows = collect_applications(seeded, dt.date(2025, 1, 1), dt.date(2025, 1, 31))
    assert [r["employer"] for r in rows] == ["Initech"]
    acts = collect_activities(seeded, dt.date(2025, 1, 1), dt.date(2025, 1, 31))
    assert [a["description"] for a in acts] == ["Synthetic spreadsheet course."]


def test_export_never_overwrites(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    export(seeded, tmp_path, *SEPT, now=NOW)
    with pytest.raises(FileExistsError):
        export(seeded, tmp_path, *SEPT, now=NOW)


def test_reversed_range_rejected(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="after"):
        export(seeded, tmp_path, SEPT[1], SEPT[0], now=NOW)


def test_empty_export_still_valid(conn: sqlite3.Connection, tmp_path: Path) -> None:
    result = export(conn, tmp_path, *SEPT, now=NOW)
    assert result.application_count == 0
    assert read_csv(result.applications_csv) == []
    assert result.pdf.read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("=HYPERLINK(1)", "'=HYPERLINK(1)"),
        ("+1", "'+1"),
        ("-x", "'-x"),
        ("@a", "'@a"),
        ("Acme", "Acme"),
        (None, ""),
    ],
)
def test_csv_formula_injection_neutralized(raw: str | None, safe: str) -> None:
    assert csv_safe(raw) == safe
