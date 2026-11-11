"""`backfill.py`, the import: the file's place, validation, classes and writes.

Synthetic companies, roles and job descriptions only, against temporary databases.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import backfill, ledger, search_store
from careerhunter.backfill import AMBIGUOUS, ATTACHING, CONFLICT, CREATE, REFUSED, UNCHANGED
from careerhunter.claims import ActiveClaim
from careerhunter.jobdesc import sha256
from careerhunter.models import Application, Dating, Posting
from careerhunter.storage_guard import DataDir
from tests.conftest import EARLIER
from tests.test_jobdesc import make_pdf

TODAY = dt.date(2026, 11, 10)
DAY = "2026-09-15"  # inside the active claim
JD = "About the role\nExample Robotics is looking for a Client Experience Analyst."
LINKEDIN = "https://www.linkedin.com/jobs/view/4000000001/?refId=abc123&trk=public_jobs_topcard"
LINKEDIN_CANONICAL = "https://www.linkedin.com/jobs/view/4000000001/"
CAREERS = "https://careers.example.com/jobs/77"


@pytest.fixture
def imports(datadir: DataDir) -> Path:
    for name in ("imports", "snapshots"):
        (datadir.root / name).mkdir(mode=0o700, exist_ok=True)
    return datadir.imports


def entry(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "employer": "Example Robotics Inc.",
        "role": "Analyst, Client Experience",
        "date": DAY,
        "date_precision": "day",
        "method": "linkedin_easy_apply",
    }
    base.update(fields)
    return {k: v for k, v in base.items() if v is not ...}


def write(imports: Path, *entries: dict[str, Any], name: str = "backfill.json") -> Path:
    path = imports / name
    path.write_text(json.dumps({"version": 1, "applications": list(entries)}))
    return path


def planned(
    datadir: DataDir,
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    path: Path,
    *,
    route: bool = False,
) -> list[backfill.Planned]:
    parsed = backfill.load(datadir, path, TODAY)
    record = backfill.Record(active.claim, active.conn) if active else backfill.Record(None)
    return backfill.plan(search, record, parsed, route_out_of_claim=route)


def run(
    datadir: DataDir,
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    path: Path,
    *,
    route: bool = False,
) -> list[backfill.Outcome]:
    plan = planned(datadir, search, active, path, route=route)
    return backfill.apply(datadir.root, search, active, plan, TODAY)


def kinds(items: list[backfill.Planned] | list[backfill.Outcome]) -> list[str]:
    return [item.kind for item in items]


def existing(
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    *,
    company: str = "Example Robotics",
    title: str = "Client Experience Analyst",
    day: str = "2026-09-14",
    url: str | None = None,
) -> str:
    posting = Posting(company=company, title=title, first_seen=day, url=url)
    app = Application(posting, Dating("exact", day=day), "company_site")
    return ledger.record_application(search, active, app).correlation_id


def snapshots(datadir: DataDir) -> list[Path]:
    return sorted(p for p in (datadir.root / "snapshots").rglob("*") if p.is_file())


def rows(conn: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(sql)]


# --- Where the file may be -------------------------------------------------------------


def test_a_file_in_imports_is_accepted(datadir: DataDir, imports: Path) -> None:
    path = write(imports, entry())
    assert backfill.import_file(datadir, path) == path.resolve()
    nested = imports / "2026"
    nested.mkdir()
    assert backfill.import_file(datadir, write(nested, entry())) == (nested / "backfill.json")


def test_a_file_elsewhere_in_the_data_folder_is_refused(datadir: DataDir, imports: Path) -> None:
    path = datadir.root / "backfill.json"
    path.write_text("{}")
    with pytest.raises(backfill.ImportPathError, match="under imports/"):
        backfill.import_file(datadir, path)


def test_a_path_outside_the_data_folder_is_refused(
    datadir: DataDir, imports: Path, tmp_path: Path
) -> None:
    path = tmp_path / "outside.json"
    path.write_text("{}")
    with pytest.raises(backfill.ImportPathError, match="under imports/"):
        backfill.import_file(datadir, path)


def test_a_symlink_that_escapes_imports_is_refused(
    datadir: DataDir, imports: Path, tmp_path: Path
) -> None:
    target = tmp_path / "outside.json"
    target.write_text("{}")
    link = imports / "link.json"
    os.symlink(target, link)
    with pytest.raises(backfill.ImportPathError, match="under imports/"):
        backfill.import_file(datadir, link)


def test_a_dotdot_path_is_refused(datadir: DataDir, imports: Path) -> None:
    (datadir.root / "seed.json").write_text("{}")
    with pytest.raises(backfill.ImportPathError, match="under imports/"):
        backfill.import_file(datadir, imports / ".." / "seed.json")


def test_a_folder_and_a_missing_file_are_refused(datadir: DataDir, imports: Path) -> None:
    with pytest.raises(backfill.ImportPathError, match="not a file"):
        (imports / "folder.json").mkdir()
        backfill.import_file(datadir, imports / "folder.json")
    with pytest.raises(backfill.ImportPathError, match="not found"):
        backfill.import_file(datadir, imports / "missing.json")


def test_without_an_imports_folder_nothing_is_read(datadir: DataDir) -> None:
    with pytest.raises(backfill.ImportPathError, match="careerhunter init"):
        backfill.import_file(datadir, datadir.root / "imports" / "backfill.json")


def test_a_jd_file_must_resolve_under_imports(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    (imports / "jd.txt").write_text(JD)
    (datadir.root / "elsewhere.txt").write_text(JD)
    os.symlink(datadir.root / "elsewhere.txt", imports / "escape.txt")
    path = write(
        imports,
        entry(jd_file="jd.txt"),
        entry(employer="Synthetic Widgets", jd_file="../elsewhere.txt"),
        entry(employer="Synthetic Gears", jd_file="escape.txt"),
        entry(employer="Synthetic Levers", jd_file="missing.txt"),
    )
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [CREATE, REFUSED, REFUSED, REFUSED]
    assert plan[1].reasons == ("jd_file must be a file under imports/ in the data folder",)
    assert plan[2].reasons == ("jd_file must be a file under imports/ in the data folder",)
    assert plan[3].reasons == ("jd_file was not found",)


# --- Validation -----------------------------------------------------------------------


def test_every_problem_in_an_entry_is_listed_and_the_rest_go_ahead(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    bad = {
        "employer": " ",
        "role": "x" * 201,
        "date": "2026-02-30",
        "date_precision": "month",
        "method": "carrier_pigeon",
        "work_model": "sometimes",
        "url": "ftp://jobs.example.com/1",
        "jd_text": "a",
        "jd_file": "jd.txt",
        "notes": 5,
        "resolve": True,
        "salary": 1,
    }
    path = write(imports, bad, entry())
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [REFUSED, CREATE]
    assert plan[0].reasons == (
        "unknown keys: ['salary']",
        "employer is blank",
        "role must be 1 to 200 characters",
        "work_model must be one of ['onsite', 'hybrid', 'remote', None]",
        "method must be one of ['company_site', 'linkedin_easy_apply', 'email', 'referral', "
        "'agency', 'recruiter_inbound', 'other']",
        "date_precision must be one of ['day', 'week']",
        "date 2026-02-30 is not a real date",
        "url must be an http or https link",
        "give jd_text or jd_file, not both",
        "notes must be text of at most 2000 characters, or null",
        'resolve must be a search application id (1 or more) or "new"',
    )


def test_missing_fields_and_a_future_date_are_refused(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    path = write(imports, {"employer": "Example Robotics"}, entry(date="2099-01-05"), [1])
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [REFUSED, REFUSED, REFUSED]
    assert plan[0].reasons == (
        "role is required",
        "date is required",
        "date_precision is required",
        "method is required",
    )
    assert plan[1].reasons == ("date 2099-01-05 is in the future",)
    assert plan[2].reasons == ("the entry must be an object",)


def test_a_bad_jd_text_is_refused(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    path = write(imports, entry(jd_text="a\x00b"), entry(employer="Synthetic Widgets", jd_text=""))
    plan = planned(datadir, search, active, path)
    assert plan[0].reasons == ("jd_text: The job description contains a NUL character.",)
    assert plan[1].reasons == ("jd_text must be 1 to 100000 characters",)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[]", "the top level must be an object"),
        ('{"version": 2, "applications": [{}]}', "version must be 1"),
        ('{"version": true, "applications": [{}]}', "version must be 1"),
        ('{"version": 1, "applications": []}', "applications must be a list"),
        ('{"version": 1, "applications": [], "extra": 1}', "unknown top-level keys"),
        ('{"version": 1, "version": 1, "applications": [{}]}', "appears twice"),
        ('{"version": 1, "applications": [{"date": NaN}]}', "NaN is not allowed"),
        ('{"version": 1,', "not valid JSON"),
    ],
)
def test_a_bad_file_stops_everything(
    datadir: DataDir, imports: Path, text: str, problem: str
) -> None:
    path = imports / "bad.json"
    path.write_text(text)
    with pytest.raises(backfill.ImportFileError, match=problem):
        backfill.load(datadir, path, TODAY)


def test_a_file_that_is_not_utf8_is_refused(datadir: DataDir, imports: Path) -> None:
    path = imports / "bad.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(backfill.ImportFileError, match="not UTF-8"):
        backfill.load(datadir, path, TODAY)


def test_a_week_is_stored_as_its_monday(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    path = write(imports, entry(date="2026-09-17", date_precision="week"))  # a Thursday
    plan = planned(datadir, search, active, path)
    assert plan[0].entry is not None
    assert plan[0].entry.dating == Dating("week", week_of="2026-09-14")
    backfill.apply(datadir.root, search, active, plan, TODAY)
    assert rows(active.conn, "SELECT date_precision, week_of FROM applications") == [
        ("week", "2026-09-14")
    ]


# --- CREATE ---------------------------------------------------------------------------


def test_create_goes_through_the_log_path_and_never_fetches(
    datadir: DataDir,
    imports: Path,
    search: sqlite3.Connection,
    active: ActiveClaim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = backfill.selfsourced.record

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[7])  # the fetcher argument is the import's own
        return real(*args, **kwargs)

    monkeypatch.setattr(backfill.selfsourced, "record", counted)
    path = write(
        imports,
        entry(url=LINKEDIN, jd_text=JD, location="Example City", work_model="hybrid", notes="N1"),
        entry(employer="Synthetic Widgets", role="Data Analyst", url=CAREERS),
    )
    outcomes = run(datadir, search, active, path)
    assert kinds(outcomes) == [CREATE, CREATE]
    assert calls == [backfill.refuse_fetch, backfill.refuse_fetch]
    assert rows(search, "SELECT url, location, work_model FROM postings ORDER BY id") == [
        (LINKEDIN_CANONICAL, "Example City", "hybrid"),
        (CAREERS, "", None),
    ]
    assert rows(active.conn, "SELECT employer, role, posting_url FROM applications") == [
        ("Example Robotics Inc.", "Analyst, Client Experience", LINKEDIN_CANONICAL),
        ("Synthetic Widgets", "Data Analyst", CAREERS),
    ]
    assert rows(search, "SELECT notes, jd_sha256, jd_expected FROM applications ORDER BY id") == [
        ("N1", sha256(JD.encode()), 1),
        (None, None, 1),
    ]
    assert rows(active.conn, "SELECT text, jd_source FROM job_descriptions") == [(JD, "pasted")]
    assert len(snapshots(datadir)) == 1  # the JD; the careers page was never fetched


def test_notes_never_reach_the_ei_record(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    run(datadir, search, active, write(imports, entry(notes="SENTINEL-NOTE-7731")))
    dump = "\n".join(active.conn.iterdump())
    assert "SENTINEL-NOTE-7731" not in dump
    assert rows(search, "SELECT notes FROM applications") == [("SENTINEL-NOTE-7731",)]


def test_a_jd_file_is_saved_in_its_own_type(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    (imports / "jds").mkdir()
    (imports / "jds" / "role.pdf").write_bytes(make_pdf(["Synthetic duties for the role."]))
    run(datadir, search, active, write(imports, entry(jd_file="jds/role.pdf")))
    assert rows(active.conn, "SELECT jd_source, file_type FROM job_descriptions") == [
        ("file", "pdf")
    ]
    assert [p.suffix for p in snapshots(datadir)] == [".pdf"]


def test_without_an_active_claim_it_goes_to_search_only(
    datadir: DataDir, imports: Path, search: sqlite3.Connection
) -> None:
    outcomes = run(datadir, search, None, write(imports, entry(jd_text=JD)))
    assert kinds(outcomes) == [CREATE]
    assert rows(search, "SELECT ei_claim, jd_sha256 FROM applications") == [
        (None, sha256(JD.encode()))
    ]


def test_out_of_claim_is_refused_or_routed_to_search(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    path = write(imports, entry(date="2026-07-01", jd_text=JD))  # before the claim started
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [REFUSED]
    assert "--route-out-of-claim-to-search" in plan[0].reasons[0]
    outcomes = run(datadir, search, active, path, route=True)
    assert kinds(outcomes) == [CREATE]
    assert rows(search, "SELECT ei_claim, jd_sha256 FROM applications") == [
        (None, sha256(JD.encode()))
    ]
    assert rows(active.conn, "SELECT count(*) FROM applications") == [(0,)]


def test_a_link_another_company_has_is_a_conflict(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, company="Synthetic Widgets", title="Data Analyst", url=CAREERS)
    plan = planned(datadir, search, active, write(imports, entry(url=CAREERS + "?utm_source=x")))
    assert kinds(plan) == [CONFLICT]
    assert plan[0].reasons == ("that link is recorded for another company",)


# --- ATTACHING, UNCHANGED and idempotence ------------------------------------------------


def test_attach_adds_the_jd_and_the_link_and_keeps_the_date(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = existing(search, active)  # "Example Robotics" / "Client Experience Analyst"
    path = write(imports, entry(url=LINKEDIN, jd_text=JD, notes="ignored on attach"))
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [ATTACHING]
    assert plan[0].set_url and plan[0].add_jd and plan[0].target is not None
    assert plan[0].target.correlation_id == corr
    backfill.apply(datadir.root, search, active, plan, TODAY)
    assert rows(search, "SELECT date_applied, notes, jd_sha256 FROM applications") == [
        ("2026-09-14", None, sha256(JD.encode()))
    ]
    assert rows(search, "SELECT url FROM postings") == [(LINKEDIN_CANONICAL,)]
    assert rows(active.conn, "SELECT date_applied, posting_url FROM applications") == [
        ("2026-09-14", LINKEDIN_CANONICAL)
    ]
    assert rows(active.conn, "SELECT text FROM job_descriptions") == [(JD,)]
    assert ledger.reconcile(search, {active.claim.label: active.conn}) == []


def test_attach_fills_only_a_link(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active)
    plan = planned(datadir, search, active, write(imports, entry(url=CAREERS)))
    assert kinds(plan) == [ATTACHING] and plan[0].set_url and not plan[0].add_jd
    backfill.apply(datadir.root, search, active, plan, TODAY)
    assert rows(search, "SELECT url, snapshot_path FROM postings") == [(CAREERS, None)]
    assert snapshots(datadir) == []


def test_a_text_less_pdf_becomes_only_the_job_ad_copy(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active)
    (imports / "scan.pdf").write_bytes(make_pdf([""]))
    path = write(imports, entry(jd_file="scan.pdf"))
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [ATTACHING] and plan[0].set_snapshot and not plan[0].add_jd
    backfill.apply(datadir.root, search, active, plan, TODAY)
    assert rows(search, "SELECT jd_sha256 FROM applications") == [(None,)]
    assert kinds(planned(datadir, search, active, path)) == [UNCHANGED]


def test_a_second_run_reports_every_entry_unchanged(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active)
    existing(search, active, company="Synthetic Gears", title="Ops Analyst")
    path = write(
        imports,
        entry(url=LINKEDIN, jd_text=JD),  # ATTACHING
        entry(employer="Synthetic Widgets", role="Data Analyst", jd_text=JD),  # CREATE
        entry(employer="Synthetic Gears", role="Ops Analyst", resolve="new"),  # CREATE, "new"
        entry(employer="Synthetic Levers", role="Field Associate", date_precision="week"),
    )
    assert kinds(run(datadir, search, active, path)) == [ATTACHING, CREATE, CREATE, CREATE]
    before = snapshots(datadir)
    again = planned(datadir, search, active, path)
    assert kinds(again) == [UNCHANGED] * 4
    assert again[2].reasons == ("already recorded",)
    assert kinds(backfill.apply(datadir.root, search, active, again, TODAY)) == [UNCHANGED] * 4
    assert snapshots(datadir) == before
    assert ledger.reconcile(search, {active.claim.label: active.conn}) == []


def test_matching_folds_the_employer_and_the_title(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = existing(search, active, company="The Example Robotics Co.")
    path = write(imports, entry(employer="EXAMPLE ROBOTICS, LTD", jd_text=JD))
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [ATTACHING]
    assert plan[0].target is not None and plan[0].target.correlation_id == corr


def test_several_candidates_take_the_one_with_the_same_date(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, day="2026-09-01")
    corr = existing(search, active, day=DAY)
    plan = planned(datadir, search, active, write(imports, entry(jd_text=JD)))
    assert kinds(plan) == [ATTACHING]
    assert plan[0].target is not None and plan[0].target.correlation_id == corr


# --- AMBIGUOUS and resolve ------------------------------------------------------------


def test_several_candidates_without_one_date_are_ambiguous(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, day="2026-09-01")
    existing(search, active, day="2026-09-02")
    plan = planned(datadir, search, active, write(imports, entry(jd_text=JD)))
    assert kinds(plan) == [AMBIGUOUS]
    assert [c.dating.day for c in plan[0].candidates] == ["2026-09-01", "2026-09-02"]
    assert kinds(backfill.apply(datadir.root, search, active, plan, TODAY)) == [AMBIGUOUS]
    assert rows(search, "SELECT count(*) FROM applications WHERE jd_sha256 IS NOT NULL") == [(0,)]


def test_titles_that_are_only_equivalent_are_ambiguous(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, title="Senior Client Experience Specialist")
    plan = planned(datadir, search, active, write(imports, entry(jd_text=JD)))
    assert kinds(plan) == [AMBIGUOUS]
    assert plan[0].reasons == ("the titles are only equivalent",)
    assert [c.title for c in plan[0].candidates] == ["Senior Client Experience Specialist"]


def test_two_entries_for_one_application_are_both_ambiguous(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active)
    path = write(imports, entry(jd_text=JD), entry(role="Client Experience Analyst", url=CAREERS))
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [AMBIGUOUS, AMBIGUOUS]
    assert plan[0].reasons == plan[1].reasons
    assert plan[0].reasons[0].startswith("entries 1, 2 all match application")


def test_two_new_entries_for_the_same_application_are_ambiguous(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    path = write(imports, entry(), entry(role="Client Experience Analyst"))
    assert kinds(planned(datadir, search, active, path)) == [AMBIGUOUS, AMBIGUOUS]


def test_resolve_names_the_application_or_creates_a_new_one(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, day="2026-09-01")
    second = existing(search, active, day="2026-09-02")
    second_id = int(search_store.jd_references(search)[second].application_id)
    path = write(
        imports,
        entry(jd_text=JD, resolve=second_id),
        entry(date="2026-09-20", resolve="new"),
        entry(employer="Synthetic Widgets", resolve=999),
    )
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [ATTACHING, CREATE, REFUSED]
    assert plan[0].target is not None and plan[0].target.correlation_id == second
    assert plan[2].reasons == ("resolve: there is no application 999 in search.db",)
    backfill.apply(datadir.root, search, active, plan, TODAY)
    assert rows(search, "SELECT count(*) FROM applications") == [(3,)]


def test_a_link_and_date_under_another_title_is_ambiguous(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, title="Customer Insights Lead", day=DAY, url=CAREERS)
    plan = planned(
        datadir, search, active, write(imports, entry(employer="Example Robotics", url=CAREERS))
    )
    assert kinds(plan) == [AMBIGUOUS]
    assert plan[0].reasons == ("its link and date are already recorded under another title",)


# --- CONFLICT and REFUSED -------------------------------------------------------------


def test_a_different_link_or_jd_is_a_conflict(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, url=CAREERS)
    existing(search, active, company="Synthetic Widgets", title="Data Analyst")
    run(
        datadir,
        search,
        active,
        write(imports, entry(employer="Synthetic Widgets", role="Data Analyst", jd_text=JD)),
    )
    path = write(
        imports,
        entry(url=CAREERS + "/other"),
        entry(employer="Synthetic Widgets", role="Data Analyst", jd_text=JD + " Changed."),
        name="second.json",
    )
    plan = planned(datadir, search, active, path)
    assert kinds(plan) == [CONFLICT, CONFLICT]
    assert plan[0].reasons == ("the application has a different link",)
    assert plan[1].reasons == ("the application has a different job description",)
    assert kinds(backfill.apply(datadir.root, search, active, plan, TODAY)) == [CONFLICT] * 2
    assert rows(search, "SELECT url FROM postings ORDER BY id") == [(CAREERS,), (None,)]


def test_the_same_link_in_another_form_is_not_a_conflict(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active, url=CAREERS + "?utm_source=board")  # stored as it came
    plan = planned(datadir, search, active, write(imports, entry(url=CAREERS)))
    assert kinds(plan) == [UNCHANGED]


def test_a_link_another_posting_has_is_a_conflict_on_attach(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active)
    existing(search, active, title="Data Analyst", url=CAREERS)
    plan = planned(datadir, search, active, write(imports, entry(url=CAREERS)))
    assert kinds(plan) == [CONFLICT]
    assert plan[0].reasons == ("that link is recorded for another posting",)


def test_an_application_in_another_claim_is_refused(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, None)  # search.db only
    search.execute("UPDATE applications SET ei_claim = ?", (EARLIER.label,))
    plan = planned(datadir, search, active, write(imports, entry(jd_text=JD)))
    assert kinds(plan) == [REFUSED]
    assert "claim 2025-26, which is not the active claim" in plan[0].reasons[0]


def test_a_voided_application_is_refused(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = existing(search, active)
    active.conn.execute(
        "UPDATE applications SET voided = 1, void_reason = 'synthetic', "
        "voided_at = '2026-10-01T00:00:00Z' WHERE correlation_id = ?",
        (corr,),
    )
    plan = planned(datadir, search, active, write(imports, entry(jd_text=JD)))
    assert kinds(plan) == [REFUSED]
    assert plan[0].reasons == ("the application was voided, so it can't change",)


def test_a_search_only_application_gets_the_jd_in_search_only(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, None)
    assert kinds(run(datadir, search, active, write(imports, entry(jd_text=JD)))) == [ATTACHING]
    assert rows(search, "SELECT ei_claim, jd_sha256 FROM applications") == [
        (None, sha256(JD.encode()))
    ]
    assert rows(active.conn, "SELECT count(*) FROM job_descriptions") == [(0,)]


def test_a_refusal_while_applying_is_reported(
    datadir: DataDir,
    imports: Path,
    search: sqlite3.Connection,
    active: ActiveClaim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing(search, active)
    plan = planned(datadir, search, active, write(imports, entry(jd_text=JD)))

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise ledger.AttachRefused("Synthetic refusal.")

    monkeypatch.setattr(backfill.ledger, "attach_job_description", refused)
    outcomes = backfill.apply(datadir.root, search, active, plan, TODAY)
    assert [(o.kind, o.detail) for o in outcomes] == [(REFUSED, "Synthetic refusal.")]


def test_the_plan_writes_nothing(
    datadir: DataDir, imports: Path, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    existing(search, active)
    path = write(
        imports, entry(url=LINKEDIN, jd_text=JD), entry(employer="Synthetic Widgets", jd_text=JD)
    )
    before = (rows(search, "SELECT * FROM applications"), rows(search, "SELECT * FROM postings"))
    assert kinds(planned(datadir, search, active, path)) == [ATTACHING, CREATE]
    after = (rows(search, "SELECT * FROM applications"), rows(search, "SELECT * FROM postings"))
    assert before == after
    assert snapshots(datadir) == []
    assert rows(active.conn, "SELECT count(*) FROM job_descriptions") == [(0,)]
