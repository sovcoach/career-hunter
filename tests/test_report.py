"""`careerhunter report` (#34): the one read-only join of search.db and a claim's EI record."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from pathlib import Path

import pytest

import careerhunter
from careerhunter import claims, cli, db, ei_store, ledger, records
from careerhunter import report as claim_report
from careerhunter.claims import ClaimSettings
from careerhunter.ei_store import EiApplication
from careerhunter.models import Application, Dating, Posting
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE, FakeProbe, set_claims
from tests.test_log import DAY, Pages, _log

SRC = Path(careerhunter.__file__).parent


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    assert cli.main(["migrate"], probe=probe) == 0
    capsys.readouterr()
    return data_env


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> Pages:
    found = Pages()
    monkeypatch.setattr(cli, "fetch_page", found)
    return found


def _digest(ready: Path) -> dict[str, str]:
    """The bytes of both databases and of any WAL with content, so a read can be proven
    harmless. A read-only open may leave an empty WAL (and its index) behind; SQLite
    gives them the database file's own mode, which is checked too."""
    found = {}
    for path in (ready / "search.db", ready / f"ei_record_{ACTIVE.label}.db"):
        for part in db.db_files(path):
            if not part.exists():
                continue
            assert part.stat().st_mode & 0o777 == 0o600, part.name
            if part.name.endswith("-shm") or (
                part.name.endswith("-wal") and not part.stat().st_size
            ):
                continue
            found[part.name] = hashlib.sha256(part.read_bytes()).hexdigest()
    return found


def _report(probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(["report", "--claim", ACTIVE.label], probe=probe)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_report_joins_both_layers_into_a_private_file(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _log(probe) == cli.EXIT_OK
    before = _digest(ready)
    code, out, _ = _report(probe, capsys)
    assert code == cli.EXIT_OK
    relative = out.strip()
    assert re.fullmatch(rf"reports/report-{ACTIVE.label}-\d{{8}}T\d{{6}}Z\.txt", relative)
    path = ready / relative
    assert path.stat().st_mode & 0o777 == 0o600
    assert (ready / "reports").stat().st_mode & 0o777 == 0o700
    text = path.read_text()
    assert "applications in both layers: 1 (0 voided in the EI record, left out)" in text
    assert "only in the EI record: 0; only in search.db: 0" in text
    assert "  No lane: 1 (applied 1)" in text
    assert (
        f"  {DAY}  Example Robotics — Synthetic Analyst  | company_site | EI status: applied "
        "| lane: No lane | fit: not scored | source: self_sourced"
    ) in text
    assert str(ready) not in out  # only the relative path is printed
    assert _digest(ready) == before  # neither file changed


def test_the_report_connection_cannot_write_either_file(
    ready: Path, probe: FakeProbe, pages: Pages
) -> None:
    assert _log(probe) == cli.EXIT_OK
    conn = claim_report.connect(ready / "search.db", ready / f"ei_record_{ACTIVE.label}.db")
    try:
        for sql in (
            "UPDATE ei.applications SET role = 'x'",
            "INSERT INTO main.skips (posting_id, date, reason) VALUES (1, '2026-10-20', 'x')",
        ):
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(sql)
    finally:
        conn.close()


def test_orphans_on_either_side_are_counted_and_left_unchanged(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _log(probe) == cli.EXIT_OK
    search = db.connect(ready / "search.db")
    try:
        posting_id = search.execute("SELECT id FROM postings").fetchone()[0]
        search.execute(
            "INSERT INTO applications (correlation_id, ei_claim, posting_id, date_applied, "
            "date_precision, method) VALUES (?, ?, ?, '2026-10-21', 'exact', 'email')",
            (secrets.token_hex(16), ACTIVE.label, posting_id),
        )
    finally:
        search.close()
    with claims.active_claim(
        DataDir(root=ready, mount_point=ready.parent),
        ClaimSettings(ACTIVE, (ACTIVE,)),
    ) as active:
        assert active is not None
        with db.transaction(active.conn):
            ei_store.insert_application(
                active.conn,
                EiApplication(
                    correlation_id=secrets.token_hex(16),
                    employer="Example Gadgets",
                    role="Data Clerk",
                    location="",
                    work_model=None,
                    posting_url=None,
                    date_precision="exact",
                    date_applied="2026-10-22",
                    week_of=None,
                    method="email",
                    snapshot_path=None,
                    resume_file=None,
                    jd_expected=1,
                ),
            )
    before = _digest(ready)
    code, out, _ = _report(probe, capsys)
    assert code == cli.EXIT_OK
    text = (ready / out.strip()).read_text()
    assert "applications in both layers: 1" in text
    assert (
        "only in the EI record: 1; only in search.db: 1; run careerhunter doctor for the details"
    ) in text
    assert "Example Gadgets" not in text  # an orphan is counted, not listed
    assert _digest(ready) == before


def test_a_claim_with_no_file_is_an_error(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert cli.main(["report", "--claim", "2030-31"], probe=probe) == cli.EXIT_ERROR
    assert "there is no record for that claim" in capsys.readouterr().err
    assert not (ready / "reports").exists()


def test_only_the_report_attaches_a_database() -> None:
    """ADR-014 as amended: no other code path joins the two files."""
    attaching = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*")
        if path.suffix in (".py", ".sql") and re.search(r"\bATTACH\b", path.read_text())
    )
    assert attaching == ["report.py"]


def _record(
    active: claims.ActiveClaim,
    search: sqlite3.Connection,
    title: str,
    dating: object,
    lane: str | None,
    checked: int,
) -> None:
    posting = Posting("Example Robotics", title, "2026-10-20", source="watcher")
    ledger.record_application(search, active, Application(posting, dating, "email"))
    search.execute(
        "UPDATE postings SET lane = ?, lane_checked = ?, fit = 'medium' WHERE title = ?",
        (lane, checked, title),
    )


def test_weeks_unknown_dates_lanes_and_voided_rows(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = ClaimSettings(ACTIVE, (ACTIVE,))
    with claims.active_claim(DataDir(root=ready, mount_point=ready.parent), settings) as active:
        assert active is not None
        search = db.connect(ready / "search.db")
        try:
            _record(active, search, "Clerk", Dating("week", week_of="2026-10-19"), "operations", 1)
            _record(active, search, "Planner", Dating("unknown"), None, 1)
            _record(active, search, "Voided Role", Dating("exact", day="2026-10-21"), None, 0)
        finally:
            search.close()
        voided = active.conn.execute(
            "SELECT id FROM applications WHERE role = 'Voided Role'"
        ).fetchone()[0]
        records.void_application(active.conn, voided, "a synthetic mistake")
    code, out, _ = _report(probe, capsys)
    assert code == cli.EXIT_OK
    text = (ready / out.strip()).read_text()
    assert "applications in both layers: 2 (1 voided in the EI record, left out)" in text
    assert "  operations: 1 (applied 1)" in text
    assert "  Off-lane: 1 (applied 1)" in text
    assert "  week of 2026-10-19  Example Robotics — Clerk  | email" in text
    assert "lane: operations | fit: Medium | source: watcher" in text
    assert "  date unknown  Example Robotics — Planner" in text
    assert "Voided Role" not in text


def test_an_empty_claim_says_none(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    with claims.active_claim(
        DataDir(root=ready, mount_point=ready.parent), ClaimSettings(ACTIVE, (ACTIVE,))
    ):
        pass  # creates the claim's file, with nothing in it
    code, out, _ = _report(probe, capsys)
    assert code == cli.EXIT_OK
    text = (ready / out.strip()).read_text()
    assert "by lane\n  none\n\napplications\n  none\n" in text


def test_a_bad_label_or_no_search_db_is_an_error(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert cli.main(["report", "--claim", "../x"], probe=probe) == cli.EXIT_ERROR
    for f in db.db_files(ready / "search.db"):
        f.unlink(missing_ok=True)
    assert cli.main(["report", "--claim", ACTIVE.label], probe=probe) == cli.EXIT_ERROR
    assert "search.db is missing" in capsys.readouterr().err


def test_a_claim_file_that_is_not_a_database_is_an_error(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    bogus = ready / f"ei_record_{ACTIVE.label}.db"
    bogus.write_bytes(b"not a database at all, just synthetic bytes" * 4)
    with pytest.raises(sqlite3.DatabaseError):
        claim_report.connect(ready / "search.db", bogus)
    code, _, err = _report(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "can't be read as a claim record" in err
    assert not (ready / "reports").exists()
