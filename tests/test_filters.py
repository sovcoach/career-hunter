"""Filter decisions. Synthetic titles and places only; no network and no model."""

from __future__ import annotations

import sqlite3

from careerhunter import search_store, watch
from careerhunter.fetch import FetchError
from careerhunter.filters import PostingText, decide
from careerhunter.models import Application, Dating, Posting
from careerhunter.storage_guard import DataDir
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, postings, url

GATES = {
    "hard_gates": {
        "min_salary": None,
        "required_languages": ["French"],
        "credentials": ["security clearance"],
        "shift_patterns": ["overnight"],
        "pay_structures": ["commission_only"],
        "seniority_words": ["Senior", "Director"],
        "title_exceptions": ["Senior Customer Advocate"],
        "never_gated_families": ["support"],
    }
}


def text(title: str, **kwargs: object) -> PostingText:
    values = {
        "company": "Example Robotics",
        "title": title,
        "location": "Example City",
        "work_model": "remote",
    }
    values.update(kwargs)
    return PostingText(**values)  # type: ignore[arg-type]


def verdict(title: str, **kwargs: object):
    posting_kwargs = {
        key: kwargs.pop(key)
        for key in ("company", "location", "work_model", "description", "requisition_id")
        if key in kwargs
    }
    return decide(
        text(title, **posting_kwargs),
        config=kwargs.pop("config", GATES),
        exclusions=kwargs.pop("exclusions", {}),
        role_filters=kwargs.pop("role_filters", None),
        applied=bool(kwargs.pop("applied", False)),
    )


def test_exclusions_match_in_order_and_a_missing_description_does_not() -> None:
    exclusions = {
        "companies": ["Example Staffing Mill"],
        "title_keywords": ["Night"],
        "description_keywords": ["unpaid"],
        "roles": [{"company": "Example Robotics", "title": "Clerk", "location": "Example City"}],
        "exclude_applied": True,
    }
    assert verdict("Operations Coordinator", exclusions=exclusions).reason is None
    staffing = verdict(
        "Operations Coordinator", company="example staffing mill", exclusions=exclusions
    )
    assert staffing.reason == "excluded_company"
    assert verdict("Night Clerk", exclusions=exclusions).reason == "excluded_title"
    assert (
        verdict("Clerk", description="An unpaid trial", exclusions=exclusions).reason
        == "excluded_description"
    )
    # The keyword is a description keyword, and this list row has no description.
    assert verdict("Unpaid Clerk", exclusions=exclusions).reason == "excluded_role"
    assert (
        verdict(
            "Clerk",
            location="Other City",
            exclusions=exclusions,
        ).reason
        is None
    )
    assert verdict("Operations Coordinator", exclusions=exclusions, applied=True).reason == (
        "exclude_applied"
    )


def test_hard_gates_and_the_seniority_exceptions() -> None:
    assert verdict("French speaking Coordinator").reason == "required_language"
    assert verdict("Coordinator", description="An active security clearance").reason == "credential"
    assert verdict("Coordinator", description="Overnight rotation").reason == "shift_pattern"
    assert verdict("Commission-Only Coordinator").reason == "pay_structure"
    assert verdict("Director of Operations").reason == "seniority"
    assert verdict("Senior Customer Advocate").reason is None
    assert verdict("Support Specialist").reason is None
    assert verdict("Senior Support Specialist").reason == "seniority"
    # The family is not explicitly senior in the title, so the description does not gate it.
    assert verdict("Support Specialist", description="Reports to a Director").reason is None
    assert verdict("Coordinator", description="Reports to a Director").reason == "seniority"


def test_min_salary_uses_the_highest_stated_yearly_figure() -> None:
    priced = {"hard_gates": {"min_salary": 60000}}
    low = verdict("Coordinator", description="Salary $40,000", config=priced)
    assert low.reason == "min_salary"
    spanned = verdict("Coordinator", description="$40,000-$80,000", config=priced)
    assert spanned.reason is None
    thousands = verdict("Coordinator", description="45k plus bonus", config=priced)
    assert thousands.reason == "min_salary"
    assert verdict("Coordinator", description="80k", config=priced).reason is None
    assert verdict("Coordinator", description="$25/hour", config=priced).reason is None
    assert verdict("Coordinator", description="salary 2026 review", config=priced).reason is None
    assert verdict("Coordinator", config=priced).reason is None
    assert verdict("Coordinator", description="Salary $40,000", config=GATES).reason is None


def test_location_rules_and_role_filters() -> None:
    config = {
        "search": {
            "location_rules": {
                "onsite_hybrid": ["Example City"],
                "remote": ["Canada"],
                "allow_travel_outside": False,
            }
        }
    }
    assert verdict("Coordinator", location="Canada", config=config).reason is None
    assert verdict("Coordinator", location="Example City", config=config).reason == "location_rule"
    assert (
        verdict("Coordinator", location="Example City", work_model="onsite", config=config).reason
        is None
    )
    assert (
        verdict("Coordinator", location="Other City", work_model="hybrid", config=config).reason
        == "location_rule"
    )
    # No work model: the location lists do not apply. Travel outside them does.
    unset = verdict("Coordinator", location="Other City", work_model=None, config=config)
    assert unset.reason is None
    assert (
        verdict(
            "Coordinator",
            location="Other City",
            work_model=None,
            description="Some travel",
            config=config,
        ).reason
        == "location_rule"
    )
    assert (
        verdict(
            "Coordinator",
            location="Example City",
            work_model=None,
            description="Some travel",
            config=config,
        ).reason
        is None
    )
    filters = {"role_families": ["operations"], "work_models": ["remote"], "locations": ["Canada"]}
    assert verdict("Clerk", role_filters=filters, config={}).reason == "role_filter"
    wrong_place = verdict(
        "Operations Coordinator", location="Example City", role_filters=filters, config={}
    )
    assert wrong_place.reason == "role_filter"
    assert (
        verdict(
            "Operations Coordinator",
            location="Canada",
            work_model="onsite",
            role_filters=filters,
            config={},
        ).reason
        == "role_filter"
    )
    # Greenhouse list rows have no work model, so a work-model constraint does not reject them.
    assert (
        verdict(
            "Operations Coordinator",
            location="Canada",
            work_model=None,
            role_filters=filters,
            config={},
        ).reason
        is None
    )


def test_schedule_flags_a_count_or_on_call_and_ignores_bare_weekends() -> None:
    flag = {"schedule_limits": {"weekend_days_per_month": 2, "over_limit": "flag"}}
    exclude = {"schedule_limits": {"weekend_days_per_month": 2, "over_limit": "exclude"}}
    kept = verdict("Coordinator", description="3 weekend days", config=flag)
    assert kept.reason is None and kept.schedule == "over_weekend_limit"
    assert verdict("Coordinator", description="3 weekend days", config=exclude).reason == (
        "schedule_limit"
    )
    bare = verdict("Coordinator", description="Weekends required", config=flag)
    assert bare.reason is None and bare.schedule is None
    on_call = verdict("Coordinator", description="An on-call rotation", config=flag)
    assert on_call.reason is None and on_call.schedule == "on_call"
    both = verdict("Coordinator", description="4 weekend days, on call", config=flag)
    assert both.schedule == "over_weekend_limit on_call"
    # A count over the cap with exclude wins over the on-call flag.
    assert (
        verdict("Coordinator", description="4 weekend days, on call", config=exclude).reason
        == "schedule_limit"
    )
    within = verdict("Coordinator", description="1 weekend day", config=flag)
    assert within.reason is None and within.schedule is None
    only = verdict("On-call Coordinator", config={})
    assert only.reason is None and only.schedule == "on_call"


def test_an_earlier_gate_wins(search: sqlite3.Connection, datadir: DataDir) -> None:
    """The posting is stored either way. The skip reason is the first match, once."""
    board = url("greenhouse", "examplerobotics")
    item = company("Example Robotics", "greenhouse", "examplerobotics")
    payload = one_greenhouse("examplerobotics", title="Senior Director")
    lines = watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config=GATES,
        exclusions={"companies": ["Example Robotics"], "exclude_applied": True},
    )
    assert any("skipped (excluded_company)" in line for line in lines)
    row = postings(search)[0]
    assert row["title"] == "Senior Director"
    assert row["schedule"] is None
    assert row["llm_output"] is None
    reasons = list(search.execute("SELECT reason FROM skips"))
    assert [r["reason"] for r in reasons] == ["excluded_company"]
    assert list(datadir.root.glob("ei_record_*")) == []
    # The same posting on the next run is not inserted or skipped again.
    again = watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config=GATES,
        exclusions={"companies": ["Example Robotics"]},
    )
    assert "1 new, 0 already recorded" not in "\n".join(again)
    assert "0 new, 1 already recorded" in again[-1]
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 1


def test_a_failed_board_writes_no_skip(search: sqlite3.Connection, datadir: DataDir) -> None:
    board = url("greenhouse", "examplerobotics")
    lines = watch.run(
        search,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today=DAY,
        fetcher=Scripted({board: FetchError("synthetic failure")}),
        config={"hard_gates": {"seniority_words": ["Coordinator"]}},
    )
    assert lines == ["watch: Example Robotics (greenhouse): FetchError; nothing recorded"]
    assert postings(search) == []
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0
    assert list(datadir.root.glob("ei_record_*")) == []


def test_a_flag_is_stored_and_a_later_run_can_skip(search: sqlite3.Connection) -> None:
    board = url("lever", "samplelogistics")
    payload = [
        {
            "id": "abc-123",
            "text": "Operations Coordinator",
            "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
            "categories": {"location": "Example City"},
            "workplaceType": "remote",
            "descriptionPlain": "Includes an on-call week.",
        }
    ]
    item = company("Sample Logistics", "lever", "samplelogistics")
    watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config={},
    )
    assert postings(search)[0]["schedule"] == "on_call"
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0
    lines = watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config={"hard_gates": {"seniority_words": ["Coordinator"]}},
    )
    assert any("skipped (seniority)" in line for line in lines)
    assert search.execute("SELECT reason FROM skips").fetchone()["reason"] == "seniority"
    # Already skipped, so a third run does not add another row.
    watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config={"hard_gates": {"seniority_words": ["Coordinator"]}},
    )
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 1


def test_exclude_applied_on_a_stored_posting(search: sqlite3.Connection) -> None:
    board = url("ashby", "exampleorg")
    payload = {
        "jobs": [
            {
                "id": "job-1",
                "title": "Operations Coordinator",
                "jobUrl": "https://jobs.ashbyhq.com/exampleorg/job-1",
                "location": "Example City",
                "workplaceType": "Remote",
                "descriptionPlain": "unpaid",
            }
        ]
    }
    item = company("Example Org", "ashby", "exampleorg")
    watch.run(search, [item], today=DAY, fetcher=Scripted({board: fetched(payload)}))
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    search_store.insert_application(
        search,
        posting_id,
        Application(
            Posting("Example Org", "Operations Coordinator", DAY, url="https://jobs.example/1"),
            Dating("exact", day=DAY),
            "company_site",
        ),
        "a" * 32,
        None,
    )
    # The description keyword matches only while the description is still in hand.
    # This recheck has no description, so the application is what records the skip.
    watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        exclusions={"description_keywords": ["unpaid"], "exclude_applied": True},
    )
    assert search.execute("SELECT reason FROM skips").fetchone()["reason"] == "exclude_applied"


def test_a_description_keyword_matches_only_with_description_text(
    search: sqlite3.Connection,
) -> None:
    board = url("greenhouse", "examplerobotics")
    watch.run(
        search,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today=DAY,
        fetcher=Scripted({board: fetched(one_greenhouse("examplerobotics", title="Unpaid Clerk"))}),
        exclusions={"description_keywords": ["unpaid"]},
    )
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0
