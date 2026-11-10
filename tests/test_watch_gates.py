"""Gates on every board the watcher fetches (#32). Synthetic boards only; no sockets."""

from __future__ import annotations

import sqlite3

import pytest

from careerhunter import watch as w
from careerhunter.fetch import FetchTooLarge
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url
from tests.test_watch_ats import Posted, bamboohr_board, smartrecruiters_board, workday_board

GREENHOUSE = company("Example Robotics", "greenhouse", "examplerobotics")


def _skip_reasons(conn: sqlite3.Connection) -> list[str]:
    return [str(row["reason"]) for row in conn.execute("SELECT reason FROM skips ORDER BY id")]


def test_greenhouse_is_fetched_with_its_descriptions(search: sqlite3.Connection) -> None:
    board = url("greenhouse", "examplerobotics")
    assert board.endswith("/jobs?content=true")
    payload = one_greenhouse("examplerobotics")
    payload["jobs"][0]["content"] = (  # type: ignore[index]
        "&lt;p&gt;Fluency in &lt;b&gt;French&lt;/b&gt; is required.&lt;/p&gt;"
    )
    lines = w.run(
        search,
        [GREENHOUSE],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config={"hard_gates": {"required_languages": ["French"]}},
    )
    assert any("skipped (required_language)" in line for line in lines)
    assert _skip_reasons(search) == ["required_language"]
    # The description is used in memory only; no column holds it.
    dump = "\n".join(search.iterdump())
    assert "Fluency in" not in dump


def test_an_oversized_board_logs_stores_nothing_and_counts_as_failed(
    search: sqlite3.Connection,
) -> None:
    board = url("greenhouse", "examplerobotics")
    lines = w.run(
        search,
        [GREENHOUSE],
        today=DAY,
        fetcher=Scripted({board: FetchTooLarge("the page is larger than 20 MB")}),
    )
    assert (
        "watch: Example Robotics (greenhouse): board over the size cap "
        "(the page is larger than 20 MB); nothing recorded"
    ) in lines
    assert lines[-1] == "watch: 0 boards fetched, 1 failed, 0 left"
    for table in ("companies", "postings", "skips"):
        assert search.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0  # noqa: S608
    # The attempt is kept, so the board goes behind the others next run.
    stamps = list(search.execute("SELECT board_key FROM watch_board_attempts"))
    assert [row["board_key"] for row in stamps] == ["greenhouse:examplerobotics"]
    run = search.execute("SELECT boards_fetched, boards_failed FROM watch_runs").fetchone()
    assert (run["boards_fetched"], run["boards_failed"]) == (0, 1)


# --- every gate on every ATS ---------------------------------------------------------

MATRIX_CONFIG = {
    "hard_gates": {
        "min_salary": 60000,
        "required_languages": ["French"],
        "credentials": ["security clearance"],
        "shift_patterns": ["overnight"],
        "pay_structures": ["commission only"],
        "seniority_words": ["Senior"],
    },
    "search": {"location_rules": {"onsite_hybrid": ["Example City"], "remote": ["Example Region"]}},
}
MATRIX_EXCLUSIONS = {"description_keywords": ["unpaid"]}
_LEVER_MODEL = {"onsite": "on-site", "remote": "remote", "hybrid": "hybrid"}
_ASHBY_MODEL = {"onsite": "OnSite", "remote": "Remote", "hybrid": "Hybrid"}


def _board(
    ats: str,
    *,
    title: str = "Operations Coordinator",
    location: str = "Example City",
    model: str = "onsite",
    description: str = "A friendly team.",
    pay: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """One job on a board of `ats`, in that board's own fields, and every URL the board
    is read from with its payload. `pay` goes where that board keeps pay: Greenhouse
    content, a Lever list, the Ashby salary summary, a SmartRecruiters ad section, the
    BambooHR or Workday description. A Workday board is a `Posted` fetcher under ""."""
    slug = "examplerobotics"
    item = company("Example Robotics", ats, slug)
    if ats == "greenhouse":
        html = f"<p>{description}</p>" + (f"<ul><li>{pay}</li></ul>" if pay else "")
        escaped = html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        job: dict[str, object] = {
            "id": 1001,
            "title": title,
            "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/1001",
            "location": {"name": location},
            "content": escaped,
        }
        return item, {url(ats, slug): {"jobs": [job]}}
    if ats == "lever":
        job = {
            "id": "abc-123",
            "text": title,
            "hostedUrl": f"https://jobs.lever.co/{slug}/abc-123",
            "categories": {"location": location},
            "workplaceType": _LEVER_MODEL[model],
            "descriptionPlain": description,
        }
        if pay:
            job["lists"] = [{"text": "Compensation", "content": f"<li>{pay}</li>"}]
        return item, {url(ats, slug): [job]}
    if ats == "workday":
        board = workday_board(
            slug, title=title, location=location, model=model, description=description, pay=pay
        )
        return {**item, "ats_host": "wd5", "ats_site": "External"}, {"": board}
    if ats == "bamboohr":
        return item, bamboohr_board(
            slug, title=title, location=location, model=model, description=description, pay=pay
        )
    if ats == "smartrecruiters":
        return item, smartrecruiters_board(
            slug, title=title, location=location, model=model, description=description, pay=pay
        )
    job = {
        "id": "uuid-1",
        "title": title,
        "jobUrl": f"https://jobs.ashbyhq.com/{slug}/uuid-1",
        "location": location,
        "workplaceType": _ASHBY_MODEL[model],
        "descriptionPlain": description,
    }
    if pay:
        job["compensation"] = {"scrapeableCompensationSalarySummary": pay}
    return item, {url(ats, slug): {"jobs": [job]}}


def _reason(conn: sqlite3.Connection, ats: str, config: object = None, **job: str) -> str | None:
    item, routes = _board(ats, **job)
    posted = routes.get("")
    fetcher = (
        posted
        if isinstance(posted, Posted)
        else Scripted({address: fetched(body) for address, body in routes.items()})
    )
    w.run(
        conn,
        [item],
        today=DAY,
        fetcher=fetcher,
        poster=posted.post if isinstance(posted, Posted) else None,
        config=config or MATRIX_CONFIG,  # type: ignore[arg-type]
        exclusions=MATRIX_EXCLUSIONS,
    )
    reasons = _skip_reasons(conn)
    assert len(reasons) <= 1
    return reasons[0] if reasons else None


ATS = ("greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr", "workday")
GATE_CASES: dict[str, tuple[dict[str, str], str | None]] = {
    "kept": ({}, None),
    "description_keyword": ({"description": "An unpaid trial week."}, "excluded_description"),
    "language": ({"description": "Fluent French is required."}, "required_language"),
    "credential": ({"description": "You must hold security clearance."}, "credential"),
    "shift": ({"description": "An overnight rotation."}, "shift_pattern"),
    "pay_structure": ({"description": "This is a commission only role."}, "pay_structure"),
    "seniority": ({"title": "Senior Operations Coordinator"}, "seniority"),
    "salary_in_description": ({"description": "Salary $40,000 per year."}, "min_salary"),
    "salary_in_pay_field": ({"pay": "$40K - $50K"}, "min_salary"),
    "salary_above": ({"pay": "$70K - $90K"}, None),
    "salary_foreign": ({"pay": "£40K - £50K"}, None),
    "onsite_elsewhere": ({"location": "Other City"}, "location_rule"),
}


@pytest.mark.parametrize("ats", ATS)
@pytest.mark.parametrize("case", sorted(GATE_CASES))
def test_every_gate_runs_on_every_ats(search: sqlite3.Connection, ats: str, case: str) -> None:
    job, expected = GATE_CASES[case]
    if case == "salary_in_pay_field" and ats != "ashby":
        job = {"pay": "Salary $40,000 - $50,000"}  # a list or HTML row, not a summary
    if case == "salary_above" and ats != "ashby":
        job = {"pay": "Salary $70,000 - $90,000"}
    if case == "salary_foreign" and ats != "ashby":
        job = {"pay": "Salary £40,000 - £50,000"}
    assert _reason(search, ats, **job) == expected


@pytest.mark.parametrize("ats", ATS)
def test_seniority_in_the_description_runs_on_every_ats(
    search: sqlite3.Connection, ats: str
) -> None:
    config = {"hard_gates": {"seniority_words": ["Senior"], "seniority_in_description": True}}
    assert _reason(search, ats, config, description="A Senior role in all but name.") == (
        "seniority"
    )


@pytest.mark.parametrize(
    ("ats", "job", "expected"),
    [
        # Remote: Greenhouse infers it from the text; Lever, Ashby and SmartRecruiters give it.
        ("greenhouse", {"location": "Remote - Other Region"}, "location_rule"),
        ("lever", {"location": "Other Region", "model": "remote"}, "location_rule"),
        ("ashby", {"location": "Other Region", "model": "remote"}, "location_rule"),
        ("greenhouse", {"location": "Remote - Example Region"}, None),
        ("bamboohr", {"location": "Remote - Other Region"}, "location_rule"),
        ("bamboohr", {"location": "Remote - Example Region"}, None),
        ("workday", {"location": "Remote - Other Region"}, "location_rule"),
        ("workday", {"location": "Remote - Example Region"}, None),
        ("lever", {"location": "Example Region", "model": "remote"}, None),
        ("ashby", {"location": "Example Region", "model": "remote"}, None),
        ("smartrecruiters", {"location": "Other Region", "model": "remote"}, "location_rule"),
        ("smartrecruiters", {"location": "Example Region", "model": "remote"}, None),
        ("smartrecruiters", {"location": "Other City", "model": "hybrid"}, "location_rule"),
        ("smartrecruiters", {"location": "", "model": "remote"}, None),
        # Hybrid is checked like on-site.
        ("lever", {"location": "Other City", "model": "hybrid"}, "location_rule"),
        ("ashby", {"location": "Other City", "model": "hybrid"}, "location_rule"),
        # Blank location: Greenhouse, BambooHR and Workday give no work model, so it is empty; the
        # others give one.
        ("greenhouse", {"location": ""}, "empty_location"),
        ("bamboohr", {"location": ""}, "empty_location"),
        ("workday", {"location": ""}, "empty_location"),
        ("lever", {"location": "", "model": "remote"}, None),
        ("ashby", {"location": "", "model": "remote"}, None),
    ],
)
def test_location_rules_on_every_ats(
    search: sqlite3.Connection, ats: str, job: dict[str, str], expected: str | None
) -> None:
    assert _reason(search, ats, **job) == expected


def test_lever_closing_text_and_salary_description_reach_the_gates(
    search: sqlite3.Connection,
) -> None:
    for field, reason in (
        ("additionalPlain", "required_language"),
        ("salaryDescriptionPlain", "min_salary"),
    ):
        search.execute("DELETE FROM skips")
        search.execute("DELETE FROM postings")
        item, routes = _board("lever")
        ((board, payload),) = routes.items()
        text = "Fluent French is required." if reason == "required_language" else "Salary $40,000"
        payload[0][field] = text  # type: ignore[index]
        w.run(
            search,
            [item],
            today=DAY,
            fetcher=Scripted({board: fetched(payload)}),
            config=MATRIX_CONFIG,
        )
        assert _skip_reasons(search) == [reason]


def test_lever_salary_range_is_not_read(search: sqlite3.Connection) -> None:
    """Its interval values are not documented, so a range there decides nothing."""
    item, routes = _board("lever")
    ((board, payload),) = routes.items()
    payload[0]["salaryRange"] = {  # type: ignore[index]
        "currency": "CAD",
        "interval": "per-year-salary",
        "min": 30000,
        "max": 40000,
    }
    w.run(
        search, [item], today=DAY, fetcher=Scripted({board: fetched(payload)}), config=MATRIX_CONFIG
    )
    assert _skip_reasons(search) == []
