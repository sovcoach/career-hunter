"""Seed import: routing by layer and claim, the per-precision claim check, idempotency."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import claims
from careerhunter.claims import ActiveClaim, Claim, ClaimSettings
from careerhunter.export import collect_applications, export
from careerhunter.ledger import reconcile
from careerhunter.seed import SeedError, SeedRows, import_rows, import_seed, monday_of, parse
from careerhunter.storage_guard import DataDir
from tests.conftest import FIXTURES, pdf_text

SEED = FIXTURES / "seed_synthetic.yaml"
TODAY = dt.date(2026, 10, 1)
SEARCH_TABLES = ("companies", "postings", "applications", "decision_history", "skips")
EI_TABLES = ("applications", "status_history", "activities")
ENDED = Claim("2026-27", dt.date(2026, 8, 5), dt.date(2027, 8, 4))  # both Wednesdays


def counts(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608


def seed(search: sqlite3.Connection, active: ActiveClaim | None, **kw: Any) -> Any:
    return import_seed(search, active, SEED, today=TODAY, **kw)


def rows(*apps: dict[str, Any], activities: list[dict[str, Any]] | None = None) -> SeedRows:
    return parse({"applications": list(apps), "activities": activities or []}, TODAY)


def _app(**fields: object) -> dict[str, object]:
    return {
        "company": "A Co",
        "title": "Analyst",
        "location": "Springfield",
        "method": "email",
        "date_precision": "exact",
        "date_applied": "2026-09-10",
    } | fields


@pytest.fixture
def ended(datadir: DataDir) -> Any:
    """The active claim, with an end date, so both edges of the claim can be tested."""
    with claims.active_claim(datadir, ClaimSettings(ENDED, (ENDED,))) as a:
        yield a


# --- Routing --------------------------------------------------------------------------


def test_each_record_goes_to_its_layer(search: sqlite3.Connection, active: ActiveClaim) -> None:
    result = seed(search, active)
    assert counts(search, SEARCH_TABLES) == {
        "companies": 4,
        "postings": 4,
        "applications": 4,
        "decision_history": 4,
        "skips": 0,
    }
    assert counts(active.conn, EI_TABLES) == {
        "applications": 4,
        "status_history": 4,
        "activities": 3,
    }
    assert result.claim == "2026-27"
    assert result.total_inserted == 16 + 11
    links = search.execute("SELECT correlation_id, ei_claim FROM applications").fetchall()
    ei = {r[0] for r in active.conn.execute("SELECT correlation_id FROM applications")}
    assert {r["correlation_id"] for r in links} == ei
    assert {r["ei_claim"] for r in links} == {"2026-27"}
    assert reconcile(search, {"2026-27": active.conn}) == []


def test_notes_stay_in_search(search: sqlite3.Connection, active: ActiveClaim) -> None:
    seed(search, active)
    notes = [r[0] for r in search.execute("SELECT notes FROM applications WHERE notes IS NOT NULL")]
    assert notes == ["Synthetic application one.", "Synthetic undated application."]
    dump = "\n".join(active.conn.iterdump())
    assert "Synthetic application one." not in dump
    assert "imported from seed" not in dump  # decision notes are search-only too


def test_reimport_never_duplicates(search: sqlite3.Connection, active: ActiveClaim) -> None:
    seed(search, active)
    before = counts(search, SEARCH_TABLES), counts(active.conn, EI_TABLES)
    again = seed(search, active)
    assert again.total_inserted == 0
    assert again.search.existing == {"companies": 4, "postings": 4, "applications": 4}
    assert again.ei.existing == {"applications": 4, "activities": 3}
    assert (counts(search, SEARCH_TABLES), counts(active.conn, EI_TABLES)) == before


def test_each_application_gets_one_applied_status_in_each_layer(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    seed(search, active)
    seed(search, active)
    for conn, table in ((active.conn, "status_history"), (search, "decision_history")):
        rows_ = conn.execute(
            f"SELECT a.date_precision, s.status, s.status_date FROM applications a "  # noqa: S608
            f"JOIN {table} s ON s.application_id = a.id ORDER BY a.id"
        ).fetchall()
        assert [r["status"] for r in rows_] == ["applied"] * 4
        assert [r["status_date"] for r in rows_] == [
            "2026-09-15",
            "2026-09-07",
            None,
            "2026-08-17",
        ]


def test_normalization(search: sqlite3.Connection, active: ActiveClaim) -> None:
    seed(search, active)
    globex = search.execute(
        "SELECT a.method, a.week_of, p.first_seen FROM applications a "
        "JOIN postings p ON p.id = a.posting_id WHERE p.title = 'Customer Success Specialist'"
    ).fetchone()
    assert globex["method"] == "linkedin_easy_apply"
    assert globex["week_of"] == "2026-09-07"  # Wednesday 09-09 -> Monday 09-07
    assert globex["first_seen"] == "2026-09-07"
    undated = search.execute("SELECT first_seen FROM postings WHERE title = 'Project Coordinator'")
    assert undated.fetchone()["first_seen"] == TODAY.isoformat()
    acme = search.execute("SELECT watchlist, ats_type FROM companies WHERE name = 'acme widgets'")
    assert tuple(acme.fetchone()) == (1, "greenhouse")
    ei_globex = active.conn.execute(
        "SELECT employer, method, week_of, work_model FROM applications "
        "WHERE role = 'Customer Success Specialist'"
    ).fetchone()
    assert tuple(ei_globex) == ("Globex Corporation", "linkedin_easy_apply", "2026-09-07", "remote")


def test_company_match_is_case_insensitive(search: sqlite3.Connection, active: ActiveClaim) -> None:
    seed(search, active)
    import_rows(search, active, rows(_app(company="ACME WIDGETS", title="Clerk")))
    assert search.execute("SELECT count(*) FROM companies").fetchone()[0] == 4


def test_validation_collects_all_problems_and_writes_nothing(
    search: sqlite3.Connection, active: ActiveClaim, tmp_path: Path
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
        import_seed(search, active, path, today=TODAY)
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
    assert counts(search, SEARCH_TABLES)["companies"] == 0
    assert counts(active.conn, EI_TABLES) == dict.fromkeys(EI_TABLES, 0)


def test_url_owned_by_another_company_writes_nothing(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    both = rows(
        _app(url="https://shared.example.com/1"),
        _app(company="B Co", url="https://shared.example.com/1"),
    )
    with pytest.raises(SeedError, match="different company"):
        import_rows(search, active, both)
    assert counts(search, SEARCH_TABLES)["companies"] == 0
    assert counts(active.conn, EI_TABLES)["applications"] == 0


def test_repost_without_url_is_a_new_posting(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    """Same role re-posted months later and re-applied to: two postings, two applications."""
    both = rows(_app(date_applied="2026-08-10"), _app(date_applied="2026-11-15"))
    import_rows(search, active, both)
    import_rows(search, active, both)
    assert counts(search, SEARCH_TABLES)["postings"] == 2
    assert counts(search, SEARCH_TABLES)["applications"] == 2
    assert counts(active.conn, EI_TABLES)["applications"] == 2


def test_reapply_to_same_url_on_another_date(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    url = "https://a.example.com/jobs/42"
    both = rows(_app(url=url, date_applied="2026-08-10"), _app(url=url, date_applied="2026-11-15"))
    first = import_rows(search, active, both)
    assert first.search.inserted == {
        "companies": 1,
        "postings": 1,
        "applications": 2,
        "decision_history": 2,
    }
    assert first.ei.inserted == {"applications": 2, "status_history": 2}
    again = import_rows(search, active, both)
    assert again.total_inserted == 0
    assert again.search.existing == {"companies": 1, "postings": 1, "applications": 2}


def test_requisition_id_identifies_posting(search: sqlite3.Connection, active: ActiveClaim) -> None:
    three = rows(
        _app(requisition_id="R-100", date_applied="2026-08-10"),
        # Re-posted later with the same requisition id: still the same posting.
        _app(requisition_id="R-100", date_applied="2026-11-15"),
        # Different requisition, same title and month: a different posting.
        _app(requisition_id="R-200", date_applied="2026-11-16"),
    )
    import_rows(search, active, three)
    import_rows(search, active, three)
    assert counts(search, SEARCH_TABLES)["postings"] == 2
    assert counts(search, SEARCH_TABLES)["applications"] == 3


def test_postings_the_ei_record_cannot_tell_apart_are_refused(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    twins = rows(_app(requisition_id="R-1"), _app(requisition_id="R-2"))
    with pytest.raises(SeedError, match="can't tell them apart"):
        import_rows(search, active, twins)
    assert counts(search, SEARCH_TABLES)["applications"] == 0


def test_duplicate_entries_in_one_file_are_recorded_once(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    result = import_rows(search, active, rows(_app(), _app()))
    assert result.search.inserted["applications"] == 1
    assert result.search.existing["applications"] == 1
    assert counts(active.conn, EI_TABLES)["applications"] == 1


def test_undated_entry_not_duplicated_in_a_later_month(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    undated = _app(date_precision="unknown", date_applied=None)
    import_rows(search, active, parse({"applications": [undated]}, dt.date(2026, 10, 1)))
    later = import_rows(search, active, parse({"applications": [undated]}, dt.date(2026, 12, 1)))
    assert later.total_inserted == 0
    assert counts(search, SEARCH_TABLES)["postings"] == 1


def test_missing_seed_file(search: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="not found"):
        import_seed(search, None, tmp_path / "seed.yaml")


def test_example_template_is_valid(
    search: sqlite3.Connection, active: ActiveClaim, tmp_path: Path
) -> None:
    template = Path(__file__).parents[1] / "seed.example.yaml"
    shutil.copyfile(template, tmp_path / "seed.yaml")
    result = import_seed(search, active, tmp_path / "seed.yaml", today=TODAY)
    assert result.ei.inserted["applications"] == 3


def test_monday_of() -> None:
    assert monday_of(dt.date(2026, 9, 13)) == dt.date(2026, 9, 7)  # Sunday
    assert monday_of(dt.date(2026, 9, 7)) == dt.date(2026, 9, 7)


# --- The claim check, one test per precision -----------------------------------------------


@pytest.mark.parametrize(
    ("day", "message"),
    [
        ("2026-08-04", "2026-08-04 is before claim 2026-27 started (2026-08-05)"),
        ("2027-08-05", "2027-08-05 is after claim 2026-27 ended (2027-08-04)"),
    ],
)
def test_exact_date_outside_the_claim_is_refused(
    search: sqlite3.Connection, ended: ActiveClaim, day: str, message: str
) -> None:
    with pytest.raises(SeedError) as exc:
        import_rows(search, ended, rows(_app(), _app(title="Clerk", date_applied=day)))
    assert message in str(exc.value)
    assert "--route-out-of-claim-to-search" in str(exc.value)
    assert counts(search, SEARCH_TABLES)["applications"] == 0
    assert counts(ended.conn, EI_TABLES)["applications"] == 0


@pytest.mark.parametrize("week_of", ["2026-08-05", "2027-08-02"], ids=["start", "end"])
def test_week_overlapping_the_claim_edge_is_accepted(
    search: sqlite3.Connection, ended: ActiveClaim, week_of: str
) -> None:
    result = import_rows(
        search, ended, rows(_app(date_precision="week", date_applied=None, week_of=week_of))
    )
    assert result.ei.inserted["applications"] == 1
    assert result.warnings == []


@pytest.mark.parametrize(
    ("week_of", "message"),
    [("2026-08-01", "entirely before"), ("2027-08-11", "entirely after")],
)
def test_week_entirely_outside_the_claim_is_refused(
    search: sqlite3.Connection, ended: ActiveClaim, week_of: str, message: str
) -> None:
    with pytest.raises(SeedError, match=message):
        import_rows(
            search, ended, rows(_app(date_precision="week", date_applied=None, week_of=week_of))
        )
    assert counts(ended.conn, EI_TABLES)["applications"] == 0


def test_unknown_date_is_accepted_with_a_warning_and_listed_as_date_unknown(
    search: sqlite3.Connection, ended: ActiveClaim, datadir: DataDir
) -> None:
    result = import_rows(search, ended, rows(_app(date_precision="unknown", date_applied=None)))
    assert result.warnings == [
        "applications[0] (A Co / Analyst): date unknown: recorded under claim 2026-27 and "
        'listed under "Date unknown" in exports'
    ]
    flag = ended.conn.execute("SELECT date_precision, date_applied FROM applications").fetchone()
    assert tuple(flag) == ("unknown", None)
    listed = collect_applications(ended.conn, dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert [(r["employer"], r["date_applied"]) for r in listed] == [("A Co", "unknown")]
    out = export(ended.conn, datadir.exports, dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert "A Co" in out.applications_csv.read_text()
    assert "Date unknown" in pdf_text(out.pdf)


def test_unknown_activity_warns(search: sqlite3.Connection, active: ActiveClaim) -> None:
    act = {"date_precision": "unknown", "type": "course", "description": "Synthetic course"}
    result = import_rows(search, active, rows(activities=[act]))
    assert result.warnings and result.ei.inserted == {"activities": 1}


# --- Out-of-claim applications: refused by default, or routed to search on request --------


def test_out_of_claim_applications_can_be_routed_to_search_only(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    mixed = rows(_app(), _app(title="Clerk", date_applied="2026-07-01"))
    result = import_rows(search, active, mixed, route_out_of_claim=True)
    assert result.search_only == [
        "applications[1] (A Co / Clerk): 2026-07-01 is before claim 2026-27 started (2026-08-05)"
    ]
    linked = dict(
        search.execute(
            "SELECT p.title, a.ei_claim FROM applications a JOIN postings p ON p.id = a.posting_id"
        ).fetchall()
    )
    assert linked == {"Analyst": "2026-27", "Clerk": None}
    roles = [r[0] for r in active.conn.execute("SELECT role FROM applications")]
    assert roles == ["Analyst"]
    assert reconcile(search, {"2026-27": active.conn}) == []
    again = import_rows(search, active, mixed, route_out_of_claim=True)
    assert again.total_inserted == 0


def test_activities_outside_the_claim_stay_refused_even_with_routing(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    late = {"date_precision": "exact", "date": "2026-07-01", "type": "course", "description": "X"}
    with pytest.raises(SeedError, match="before claim 2026-27 started"):
        import_rows(search, active, rows(_app(), activities=[late]), route_out_of_claim=True)
    assert counts(search, SEARCH_TABLES)["applications"] == 0


def test_without_an_active_claim_applications_go_to_search_only(
    datadir: DataDir, search: sqlite3.Connection
) -> None:
    result = import_rows(search, None, rows(_app()))
    assert result.claim is None
    assert search.execute("SELECT ei_claim FROM applications").fetchone()[0] is None
    assert datadir.ei_record_files() == []
    with pytest.raises(SeedError, match="no EI claim is active"):
        seed(search, None)


# --- A re-run completes a missing side -----------------------------------------------------


def test_rerun_restores_a_missing_search_row_under_the_same_id(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    seed(search, active)
    ids = {r[0] for r in active.conn.execute("SELECT correlation_id FROM applications")}
    search.execute("DELETE FROM decision_history")
    search.execute("DELETE FROM applications")
    assert len(reconcile(search, {"2026-27": active.conn})) == 4
    result = seed(search, active)
    assert result.search.inserted["applications"] == 4
    assert {r[0] for r in search.execute("SELECT correlation_id FROM applications")} == ids
    assert reconcile(search, {"2026-27": active.conn}) == []


def test_rerun_restores_a_missing_ei_row_under_the_same_id(
    search: sqlite3.Connection, active: ActiveClaim, tmp_path: Path
) -> None:
    backup = sqlite3.connect(tmp_path / "backup.db")
    try:
        active.conn.backup(backup)
        seed(search, active)
        backup.backup(active.conn)  # restore the empty record
    finally:
        backup.close()
    assert len(reconcile(search, {"2026-27": active.conn})) == 4
    result = seed(search, active)
    assert result.ei.inserted["applications"] == 4
    assert reconcile(search, {"2026-27": active.conn}) == []


def test_history_seeded_before_the_claim_is_linked_later(
    datadir: DataDir, search: sqlite3.Connection, active: ActiveClaim
) -> None:
    import_rows(search, None, rows(_app()))
    (corr,) = [r[0] for r in search.execute("SELECT correlation_id FROM applications")]
    result = import_rows(search, active, rows(_app()))
    assert result.search.existing == {"companies": 1, "postings": 1, "applications": 1}
    assert result.ei.inserted["applications"] == 1
    assert search.execute("SELECT ei_claim FROM applications").fetchone()[0] == "2026-27"
    assert [r[0] for r in active.conn.execute("SELECT correlation_id FROM applications")] == [corr]
