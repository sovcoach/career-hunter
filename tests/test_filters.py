"""Filter decisions. Synthetic titles and places only; no network and no model."""

from __future__ import annotations

import sqlite3

import pytest

from careerhunter import filters, search_store, watch
from careerhunter.fetch import FetchError
from careerhunter.filters import PostingText, decide, has_phrase
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
    # The description is not checked by default, so a junior title is kept.
    assert verdict("Support Specialist", description="Reports to a Director").reason is None
    assert verdict("Coordinator", description="Reports to a Director").reason is None


PRICED = {"hard_gates": {"min_salary": 60000}}


def salary(description: str) -> str | None:
    return verdict("Coordinator", description=description, config=PRICED).reason


def test_min_salary_uses_the_top_of_the_highest_stated_range() -> None:
    assert salary("Salary $40,000") == "min_salary"
    assert salary("Salary range $40,000-$80,000") is None
    assert salary("Salary 45k plus bonus") == "min_salary"
    assert salary("Salary 80k") is None
    assert salary("salary 2026 review") is None
    assert verdict("Coordinator", config=PRICED).reason is None
    assert verdict("Coordinator", description="Salary $40,000", config=GATES).reason is None
    # Two ranges: the top of the higher one decides.
    assert salary("Pay range $40,000-$50,000 in one office, $55,000-$65,000 in another") is None


@pytest.mark.parametrize(
    "description",
    [
        "$40,000-$50,000",  # no pay word and no unit
        "45k",
        "We raised $50,000,000 in funding.",
        "Signing bonus of $5,000.",
        "A $10,000 signing bonus.",
        "Relocation support up to $8,000 is offered.",
        "Equity: $20,000 in RSUs.",
        "Pay includes a one-time bonus of $2,000.",
        "Generous 401k matching, salary negotiable.",
    ],
)
def test_a_figure_outside_a_pay_context_does_not_count(description: str) -> None:
    assert salary(description) is None


def test_bonus_figures_beside_pay_are_left_out_and_pay_still_counts() -> None:
    assert salary("Base salary $40,000, signing bonus $90,000") == "min_salary"
    assert salary("Salary $40,000 plus a $90,000 equity grant") == "min_salary"
    assert salary("Salary $90,000 plus annual bonus") is None


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("Pay range: $20 - $25 per hour", "min_salary"),  # below: top 52,000
        ("Pay range: $25 - $35 per hour", None),  # straddles: top 72,800
        ("Pay range: $30 - $40/hr", None),  # above
        ("Pay range: $20 \u2013 $25 per hour", "min_salary"),  # an en dash joins a range
        ("Hourly pay: $26 to $28.", "min_salary"),  # unit named once in the sentence
        ("$25/hour", "min_salary"),  # a single hourly figure: 52,000
        ("$30 an hour", None),  # 62,400
        ("Pay is 29 per hour", None),  # no currency mark, but a unit: 60,320
        ("$22.50/hr", "min_salary"),
    ],
)
def test_hourly_figures_become_yearly_at_forty_hours_a_week(
    description: str, reason: str | None
) -> None:
    assert salary(description) == reason


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("Salary $4,000 per month", "min_salary"),  # x 12 = 48,000
        ("Salary $5,500 monthly", None),  # 66,000
        ("$1,100 per week", "min_salary"),  # x 52 = 57,200
        ("$1,200 weekly", None),  # 62,400
        ("$220 per day", "min_salary"),  # x 260 = 57,200
        ("$240 daily", None),  # 62,400
        ("Salary $55,000 per year", "min_salary"),
        ("Salary range $50k-$70k annually", None),
        ("Pay 50-55k", "min_salary"),  # the k covers both ends
    ],
)
def test_each_pay_unit_is_converted_to_yearly(description: str, reason: str | None) -> None:
    assert salary(description) == reason


@pytest.mark.parametrize(
    "description",
    [
        "Salary: USD 40,000",
        "Salary US$40,000",
        "Salary $40,000 USD",
        "Salary €40,000",
        "Salary £40k",
        "Salary range EUR 30,000 - 40,000",
        "Salary $30,000 - $40,000 USD",  # a code on one end marks the whole range
        "Salary 40,000 GBP",
    ],
)
def test_a_figure_in_another_currency_is_ignored_so_the_posting_passes(description: str) -> None:
    assert salary(description) is None


@pytest.mark.parametrize(
    "description",
    [
        "Salary $40,000",
        "Salary C$40,000",
        "Salary CA$40,000",
        "Salary CAD 40,000",
        "Salary 40k CAD",
    ],
)
def test_cad_and_a_plain_dollar_sign_count(description: str) -> None:
    assert salary(description) == "min_salary"


def test_two_different_units_in_a_sentence_with_none_attached_count_for_nothing() -> None:
    assert salary("Paid hourly or monthly, $20 to start.") is None


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
    assert "0 new, 1 already recorded" in again[-2]
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
    assert lines == [
        "watch: Example Robotics (greenhouse): FetchError; nothing recorded",
        "watch: 0 boards fetched, 1 failed, 0 left",
    ]
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


# Whole-word phrase matching, shared by the seniority gate and, later, title lanes.


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("Senior Coordinator", "senior"),
        ("COORDINATOR, SENIOR", "Senior"),
        ("Team Lead", "Lead"),
        ("Lead (Night)", "Lead"),
        ("(Lead) Coordinator", "Lead"),
        ("Director's Office Coordinator", "Director"),
        ("Coordinator - Senior", "Senior"),
        ("Vice-President, Operations", "Vice President"),
        ("Vice_President of Operations", "vice president"),
        ("Vice \u2014 President", "Vice President"),
        ("Sr. Manager", "Sr."),
        ("Ingénieur principal", "Principal"),
        ("Engineer II", "II"),
    ],
)
def test_a_phrase_matches_as_whole_words(text: str, phrase: str) -> None:
    assert has_phrase(text, phrase)


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("Team Leader", "Lead"),
        ("Misleading Title", "Lead"),
        ("Srinivas Coordinator", "Sr"),
        ("Directors of Operations", "Director"),
        ("Seniority Analyst", "Senior"),
        ("Engineer III", "II"),
        ("Sr.Manager", "Sr."),
        ("Leadé Coordinator", "Lead"),
    ],
)
def test_a_phrase_never_matches_inside_a_longer_word(text: str, phrase: str) -> None:
    assert not has_phrase(text, phrase)


COMPOSED = "Ing\u00e9nieur"  # é as one code point
DECOMPOSED = "Inge\u0301nieur"  # e followed by a combining acute accent


@pytest.mark.parametrize(
    ("text", "phrase"),
    [(DECOMPOSED + " Lead", COMPOSED), (COMPOSED + " Lead", DECOMPOSED)],
    ids=["decomposed-title", "composed-title"],
)
def test_composed_and_decomposed_accents_match_alike(text: str, phrase: str) -> None:
    assert has_phrase(text, phrase)


def test_an_accent_mark_on_the_last_letter_makes_it_a_different_word() -> None:
    assert not has_phrase("Lead\u0301 Coordinator", "Lead")


def test_a_ligature_matches_its_letters() -> None:
    assert has_phrase("Pro\ufb01le Analyst", "Profile")


def test_folding_a_dotted_capital_i_is_stable() -> None:
    title = "\u0130nspector Lead"  # İ: casefolds to "i" and a combining dot above
    once = filters._fold(title)
    assert filters._fold(once) == once
    assert has_phrase(title, "\u0130nspector")  # the same word, folded the same way
    assert has_phrase(title, "Lead")
    assert not has_phrase(title, "Inspector")  # a plain I is a different letter here


def test_a_later_whole_word_counts_after_an_earlier_partial_one() -> None:
    assert has_phrase("Leadership Lead", "Lead")


def test_only_partial_words_do_not_match() -> None:
    assert not has_phrase("Leadership Leaders", "Lead")


@pytest.mark.parametrize("phrase", ["", "   ", "-", "_"])
def test_a_blank_phrase_matches_nothing(phrase: str) -> None:
    assert not has_phrase("Senior Coordinator", phrase)


# Seniority: whole words of the title; the description only when asked for.

DESCRIBED = {"hard_gates": {**GATES["hard_gates"], "seniority_in_description": True}}


def test_a_seniority_word_only_in_the_description_does_not_gate_by_default() -> None:
    assert verdict("Coordinator", description="Works with a Senior Director").reason is None


def test_the_description_gates_when_the_option_is_on() -> None:
    gated = verdict("Coordinator", description="Reports to a Director", config=DESCRIBED)
    assert gated.reason == "seniority"
    # A never-gated family is still not gated by the description alone.
    family = verdict("Support Specialist", description="Reports to a Director", config=DESCRIBED)
    assert family.reason is None
    # Its title still gates it.
    assert verdict("Senior Support Specialist", config=DESCRIBED).reason == "seniority"


@pytest.mark.parametrize("config", [GATES, DESCRIBED], ids=["title-only", "with-description"])
def test_a_seniority_word_never_matches_inside_a_longer_word(config: object) -> None:
    assert verdict("Seniority Data Coordinator", config=config).reason is None
    assert (
        verdict("Coordinator", description="Directors meet monthly", config=config).reason is None
    )
    assert verdict("Senior-level Coordinator", config=config).reason == "seniority"


@pytest.mark.parametrize("config", [GATES, DESCRIBED], ids=["title-only", "with-description"])
def test_title_exceptions_and_never_gated_families_match_whole_words(config: object) -> None:
    assert verdict("Senior Customer Advocate", config=config).reason is None
    assert verdict("SENIOR customer-advocate", config=config).reason is None
    assert verdict("Senior Support Specialist", config=config).reason == "seniority"


def test_a_short_exception_no_longer_exempts_a_title_that_merely_contains_it() -> None:
    gates = {
        "hard_gates": {
            "seniority_words": ["Senior"],
            "title_exceptions": ["Art"],
            "never_gated_families": ["Port"],
        }
    }
    # "Art" was a substring of "Partner"; it is not a word of this title.
    assert verdict("Senior Partner Coordinator", config=gates).reason == "seniority"
    assert verdict("Senior Art Coordinator", config=gates).reason is None
    # "Port" in "Support" no longer marks the never-gated family.
    described = {"hard_gates": {**gates["hard_gates"], "seniority_in_description": True}}
    assert (
        verdict("Support Coordinator", description="Senior team", config=described).reason
        == "seniority"
    )
    assert verdict("Port Coordinator", description="Senior team", config=described).reason is None
