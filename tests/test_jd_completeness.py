"""JD completeness (ADR-024): doctor's line, the report's and the export's JD reference,
and the export's folder of JDs. Synthetic data only."""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, db, ei_store, jobdesc, ledger, search_store
from careerhunter import report as claim_report
from careerhunter.claims import ActiveClaim
from careerhunter.export import export
from careerhunter.models import Application, Dating, Posting
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE

TODAY = dt.date(2026, 10, 28)
NOW = dt.datetime(2026, 10, 30, 9, 30, 0)
RANGE = (dt.date(2026, 9, 1), dt.date(2026, 10, 31))
TEXT = "About the role\nSynthetic analyst duties.\n"


def application(title: str, day: str = "2026-09-15", url: str | None = None) -> Application:
    return Application(
        Posting(
            company="Example Robotics",
            title=title,
            first_seen=day,
            url=url or f"https://careers.example.com/jobs/{title.replace(' ', '-')}?utm_source=x",
            source="self_sourced",
        ),
        Dating("exact", day=day),
        "company_site",
    )


@pytest.fixture
def saved(datadir: DataDir) -> Any:
    (datadir.root / "snapshots").mkdir(mode=0o700, exist_ok=True)
    return lambda jd: jobdesc.save(datadir.root, jd, TODAY)


def pre_feature(
    search: sqlite3.Connection, active: ActiveClaim, title: str, jd: jobdesc.SavedJd | None = None
) -> str:
    """An application as it was recorded before JDs were kept: jd_expected 0 in both."""
    app = application(title)
    corr = ledger.new_correlation_id()
    with db.transaction(search):
        _, posting_id, _, _ = search_store.find_or_create_posting(search, app.posting, app.dating)
        search.execute(
            "INSERT INTO applications (correlation_id, ei_claim, posting_id, date_applied, "
            "date_precision, method) VALUES (?, ?, ?, ?, 'exact', 'company_site')",
            (corr, ACTIVE.label, posting_id, app.dating.day),
        )
    ei_app = dataclasses.replace(ledger.sanitize(app, corr), jd_expected=0)
    ei_id = ei_store.insert_application(active.conn, ei_app)
    ei_store.add_status(active.conn, ei_id, "applied", app.dating.day)
    if jd is not None:
        ei_store.insert_job_description(active.conn, ei_id, ledger.ei_job_description(jd))
        app_id = int(search.execute("SELECT max(id) FROM applications").fetchone()[0])
        search_store.set_application_jd(search, app_id, jd.description.text_sha256, jd.path)
    return corr


@pytest.fixture
def mixed(search: sqlite3.Connection, active: ActiveClaim, saved: Any) -> dict[str, Any]:
    """Four applications: a JD, none, a pre-feature one with a JD and one without."""
    pasted = saved(jobdesc.from_paste(TEXT))
    filed = saved(jobdesc.from_file(b"<html><p>Synthetic planner duties.</p></html>"))
    ledger.record_application(search, active, application("Analyst"), pasted)
    ledger.record_application(search, active, application("Planner", "2026-09-16"))
    pre_feature(search, active, "Clerk", filed)
    pre_feature(search, active, "Coordinator")
    return {"pasted": pasted, "filed": filed}


# --- doctor -------------------------------------------------------------------------------


def _doctor_line(search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]) -> str:
    report = cli._Report()
    cli._doctor_job_descriptions(report, search)
    return capsys.readouterr().out.strip()


def test_doctor_warns_about_a_missing_jd_and_counts_the_pre_feature_ones(
    search: sqlite3.Connection, mixed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _doctor_line(search, capsys) == (
        "job descriptions WARN 1 application has no job description (reply to the bot's "
        "request with it); 1 application from before JDs were kept"
    )


def test_doctor_marks_only_pre_feature_ones_and_is_ok_when_none_are_missing(
    search: sqlite3.Connection,
    active: ActiveClaim,
    saved: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _doctor_line(search, capsys) == "job descriptions OK  every application has one"
    pre_feature(search, active, "Coordinator")
    assert _doctor_line(search, capsys) == (
        "job descriptions --  1 application from before JDs were kept, with no JD"
    )
    ledger.record_application(
        search, active, application("Analyst"), saved(jobdesc.from_paste(TEXT))
    )
    assert "--" in _doctor_line(search, capsys)


# --- report -------------------------------------------------------------------------------


def _report_rows(datadir: DataDir) -> list[claim_report.Row]:
    conn = claim_report.connect(datadir.search_db_path, datadir.ei_record_path(ACTIVE.label))
    try:
        return list(claim_report.build(conn, ACTIVE.label).rows)
    finally:
        conn.close()


def test_every_report_row_has_the_canonical_link_and_a_jd_reference(
    datadir: DataDir, mixed: dict[str, Any]
) -> None:
    rows = {row.role: row for row in _report_rows(datadir)}
    pasted, filed = mixed["pasted"].description, mixed["filed"].description
    assert rows["Analyst"].jd == f"JD {pasted.text_sha256[:8]} (pasted)"
    assert rows["Planner"].jd == "JD missing"
    assert rows["Clerk"].jd == f"JD {filed.text_sha256[:8]} (file)"  # any JD shows its ref
    assert rows["Coordinator"].jd == "JD missing (pre-feature)"
    assert rows["Analyst"].link == "https://careers.example.com/jobs/Analyst"


def test_the_rendered_report_shows_both(datadir: DataDir, mixed: dict[str, Any]) -> None:
    conn = claim_report.connect(datadir.search_db_path, datadir.ei_record_path(ACTIVE.label))
    try:
        text = claim_report.render(claim_report.build(conn, ACTIVE.label), NOW)
    finally:
        conn.close()
    assert "| JD missing (pre-feature) | https://careers.example.com/jobs/Coordinator" in text
    assert "Synthetic analyst" not in text  # the reference, never the text


def _ei_at_0001(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    db.migrate(conn, "ei_record", db.load_migrations("ei_record")[:1])
    conn.execute(
        "INSERT INTO claim (id, label, start_date) VALUES (1, ?, ?)",
        (ACTIVE.label, ACTIVE.start.isoformat()),
    )
    conn.execute(
        "INSERT INTO applications (correlation_id, employer, role, date_applied, "
        "date_precision, method, posting_url) VALUES (?, 'Example Robotics', 'Analyst', "
        "'2026-09-15', 'exact', 'email', 'https://careers.example.com/a#x')",
        ("c" * 32,),
    )
    conn.execute(
        "INSERT INTO status_history (application_id, status, status_date) "
        "VALUES (1, 'applied', '2026-09-15')"
    )
    return conn


def test_the_report_reads_an_ei_file_still_at_0001(
    datadir: DataDir, search: sqlite3.Connection
) -> None:
    _ei_at_0001(datadir.ei_record_path(ACTIVE.label)).close()
    app = application("Analyst", url="https://careers.example.com/a")
    with db.transaction(search):
        _, posting_id, _, _ = search_store.find_or_create_posting(search, app.posting, app.dating)
        search.execute(
            "INSERT INTO applications (correlation_id, ei_claim, posting_id, date_applied, "
            "date_precision, method) VALUES (?, ?, ?, '2026-09-15', 'exact', 'email')",
            ("c" * 32, ACTIVE.label, posting_id),
        )
    (row,) = _report_rows(datadir)
    assert (row.jd, row.link) == ("JD missing (pre-feature)", "https://careers.example.com/a")


# --- export -------------------------------------------------------------------------------


def test_the_export_has_a_jd_column_and_a_folder_of_jds(
    datadir: DataDir, active: ActiveClaim, mixed: dict[str, Any]
) -> None:
    result = export(active.conn, datadir.exports, *RANGE, now=NOW)
    with result.applications_csv.open(newline="", encoding="utf-8") as fh:
        refs = {r["role"]: r["job_description"] for r in csv.DictReader(fh)}
    pasted, filed = mixed["pasted"].description, mixed["filed"].description
    assert refs == {
        "Analyst": f"JD {pasted.text_sha256[:8]} (pasted)",
        "Planner": "JD missing",
        "Clerk": f"JD {filed.text_sha256[:8]} (file)",
        "Coordinator": "JD missing (pre-feature)",
    }
    folder = result.job_descriptions
    assert folder is not None and result.jd_count == 2
    assert folder.name == result.pdf.stem + "-job-descriptions"
    assert folder.stat().st_mode & 0o777 == 0o700
    files = sorted(p.name for p in folder.iterdir())
    assert files == [
        f"001_{pasted.text_sha256[:8]}.txt",
        f"001_{pasted.text_sha256[:8]}_original.txt",
        f"002_{filed.text_sha256[:8]}.txt",
        f"002_{filed.text_sha256[:8]}_original.html",
    ]
    for path in folder.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    first = (folder / files[0]).read_text(encoding="utf-8")
    header, _, body = first.partition("\n\n")
    assert body == TEXT  # verbatim
    assert "Employer: Example Robotics" in header and "Role: Analyst" in header
    assert f"SHA-256 of the text: {pasted.text_sha256}" in header
    original = (folder / files[3]).read_bytes()
    assert original == (datadir.root / mixed["filed"].path).read_bytes()


def test_an_export_never_overwrites_a_jd_folder(
    datadir: DataDir, active: ActiveClaim, mixed: dict[str, Any]
) -> None:
    first = export(active.conn, datadir.exports, *RANGE, now=NOW)
    assert first.job_descriptions is not None
    with pytest.raises(FileExistsError):
        export(active.conn, datadir.exports, *RANGE, now=NOW)


def test_an_original_that_is_gone_is_left_out(
    datadir: DataDir, active: ActiveClaim, mixed: dict[str, Any]
) -> None:
    (datadir.root / mixed["filed"].path).unlink()
    result = export(active.conn, datadir.exports, *RANGE, now=NOW)
    assert result.job_descriptions is not None
    assert len(list(result.job_descriptions.iterdir())) == 3


def test_an_export_without_jds_makes_no_folder(
    datadir: DataDir, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    ledger.record_application(search, active, application("Planner"))
    result = export(active.conn, datadir.exports, *RANGE, now=NOW)
    assert result.job_descriptions is None and result.jd_count == 0
    assert not list(datadir.exports.glob("*-job-descriptions"))


def test_the_export_reads_an_ei_file_still_at_0001(datadir: DataDir) -> None:
    conn = _ei_at_0001(datadir.root / "ei_old.db")
    try:
        result = export(conn, datadir.exports, *RANGE, now=NOW)
    finally:
        conn.close()
    with result.applications_csv.open(newline="", encoding="utf-8") as fh:
        (row,) = list(csv.DictReader(fh))
    assert row["job_description"] == "JD missing (pre-feature)"
    assert result.job_descriptions is None


# --- a JD marked unavailable (EI migration 0003) ------------------------------------------

GONE = "posting removed before capture"


def _planner(search: sqlite3.Connection) -> str:
    return str(
        search.execute(
            "SELECT a.correlation_id FROM applications a JOIN postings p ON p.id = a.posting_id "
            "WHERE p.title = 'Planner'"
        ).fetchone()[0]
    )


def test_doctor_shows_a_marked_jd_as_dashes_not_a_warning(
    search: sqlite3.Connection,
    active: ActiveClaim,
    mixed: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger.mark_jd_unavailable(search, active, _planner(search), GONE)
    assert _doctor_line(search, capsys).splitlines() == [
        f"job descriptions --  1 application: JD unavailable ({GONE})",
        "job descriptions --  1 application from before JDs were kept, with no JD",
    ]


def test_doctor_is_ok_when_every_other_application_has_a_jd(
    search: sqlite3.Connection, active: ActiveClaim, capsys: pytest.CaptureFixture[str]
) -> None:
    corr = ledger.record_application(search, active, application("Planner")).correlation_id
    ledger.mark_jd_unavailable(search, active, corr, GONE)
    assert _doctor_line(search, capsys).splitlines() == [
        f"job descriptions --  1 application: JD unavailable ({GONE})",
        "job descriptions OK  every other application has one",
    ]


def test_doctor_lists_at_most_three_explanations(
    search: sqlite3.Connection, active: ActiveClaim, capsys: pytest.CaptureFixture[str]
) -> None:
    for number in range(5):
        app = application(f"Role {number}", url=f"https://careers.example.com/r{number}")
        corr = ledger.record_application(search, active, app).correlation_id
        ledger.mark_jd_unavailable(search, active, corr, f"gone {number}")
    first = _doctor_line(search, capsys).splitlines()[0]
    assert first == (
        "job descriptions --  5 applications: JD unavailable (gone 0; gone 1; gone 2; and 2 more)"
    )


def test_the_report_and_the_export_show_a_marked_jd(
    datadir: DataDir, search: sqlite3.Connection, active: ActiveClaim, mixed: dict[str, Any]
) -> None:
    ledger.mark_jd_unavailable(search, active, _planner(search), GONE)
    rows = {row.role: row for row in _report_rows(datadir)}
    assert rows["Planner"].jd == f"JD -- ({GONE})"
    assert rows["Coordinator"].jd == "JD missing (pre-feature)"
    result = export(active.conn, datadir.exports, *RANGE, now=NOW)
    with result.applications_csv.open(newline="", encoding="utf-8") as fh:
        refs = {r["role"]: r["job_description"] for r in csv.DictReader(fh)}
    assert refs["Planner"] == f"JD -- ({GONE})"


def test_a_later_jd_replaces_the_marker_in_the_report(
    datadir: DataDir,
    search: sqlite3.Connection,
    active: ActiveClaim,
    mixed: dict[str, Any],
    saved: Any,
) -> None:
    corr = _planner(search)
    ledger.mark_jd_unavailable(search, active, corr, GONE)
    jd = saved(jobdesc.from_paste("Synthetic planner duties, found later."))
    ledger.attach_job_description(search, active, corr, jd)
    rows = {row.role: row for row in _report_rows(datadir)}
    assert rows["Planner"].jd == f"JD {jd.description.text_sha256[:8]} (pasted)"
