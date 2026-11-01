from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import db
from careerhunter.claims import ActiveClaim
from careerhunter.export import (
    ACTIVITY_COLUMNS,
    APPLICATION_COLUMNS,
    collect_activities,
    collect_applications,
    csv_safe,
    export,
)
from careerhunter.ledger import record_status
from careerhunter.seed import import_seed
from careerhunter.storage_guard import DataDir
from tests.conftest import FIXTURES, pdf_text

NOW = dt.datetime(2026, 10, 30, 9, 30, 0)
SEPT = (dt.date(2026, 9, 1), dt.date(2026, 9, 30))


@pytest.fixture
def seeded(search: sqlite3.Connection, active: ActiveClaim) -> sqlite3.Connection:
    import_seed(search, active, FIXTURES / "seed_synthetic.yaml", today=dt.date(2026, 10, 1))
    acme = active.conn.execute(
        "SELECT correlation_id FROM applications WHERE role = 'Operations Coordinator'"
    ).fetchone()[0]
    record_status(search, active, acme, "screening", "2026-09-22", note="phone screen")
    record_status(search, active, acme, "interview_scheduled", "2026-09-29")
    return active.conn


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_export_writes_three_files(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    result = export(seeded, tmp_path, *SEPT, now=NOW)
    assert result.claim == "2026-27"
    assert result.pdf.name == "ei_2026-27_2026-09-01_2026-09-30_20261030T093000.pdf"
    assert result.pdf.read_bytes().startswith(b"%PDF")
    text = pdf_text(result.pdf)
    assert r"EI claim 2026-27 \(from 2026-08-05\)" in text  # PDF strings escape parentheses
    assert "phone screen" not in text  # status notes are search-only
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
    assert acme["current_status"] == "interview scheduled"
    assert acme["status_history"] == (
        "2026-09-15 applied; 2026-09-22 screening; 2026-09-29 interview scheduled"
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


@pytest.fixture
def permissive_umask() -> Any:
    previous = os.umask(0)  # anything the process creates could be world-writable
    yield
    os.umask(previous)


def test_export_files_are_owner_only_whatever_the_umask(
    seeded: sqlite3.Connection, tmp_path: Path, permissive_umask: None
) -> None:
    result = export(seeded, tmp_path, *SEPT, now=NOW)
    for path in (result.pdf, result.applications_csv, result.activities_csv):
        assert path.stat().st_mode & 0o777 == 0o600, path.name
    (tmp_path / "control").write_text("")  # proves the umask really is wide open
    assert (tmp_path / "control").stat().st_mode & 0o777 == 0o666


def test_reversed_range_rejected(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="after"):
        export(seeded, tmp_path, SEPT[1], SEPT[0], now=NOW)


def test_empty_export_still_valid(ei: sqlite3.Connection, tmp_path: Path) -> None:
    result = export(ei, tmp_path, *SEPT, now=NOW)
    assert result.application_count == 0
    assert read_csv(result.applications_csv) == []
    assert result.pdf.read_bytes().startswith(b"%PDF")


def test_export_needs_only_the_claim_file(
    seeded: sqlite3.Connection, datadir: DataDir, tmp_path: Path, opener: Any
) -> None:
    """A copy of the claim file, alone in a folder, exports the same record."""
    alone = tmp_path / "alone"
    alone.mkdir()
    copy = sqlite3.connect(alone / "ei_record_2026-27.db")
    try:
        seeded.backup(copy)
    finally:
        copy.close()
    assert not (alone / "search.db").exists()
    statements: list[str] = []
    ro = opener(alone / "ei_record_2026-27.db", read_only=True)
    ro.set_trace_callback(statements.append)
    result = export(ro, alone, *SEPT, now=NOW)
    assert result.application_count == 3
    assert not [s for s in statements if "ATTACH" in s.upper()]
    original = export(seeded, datadir.exports, *SEPT, now=NOW)
    assert read_csv(result.applications_csv) == read_csv(original.applications_csv)


def test_export_refuses_a_file_that_is_not_a_claim_record(tmp_path: Path, opener: Any) -> None:
    other = opener(tmp_path / "search.db", "search")
    with pytest.raises(ValueError, match="not a complete EI record"):
        export(other, tmp_path, *SEPT, now=NOW)
    assert db.file_track(other) == "search"


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
