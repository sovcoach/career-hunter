"""A posting's location at intake (Batch R): JSON-LD, the ATS's JSON, a `Location:`
line, a location given by hand, and "remote" read from the title. Synthetic only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, db, filters, jobdesc, locations, search_store, watch
from careerhunter.fetch import Fetched
from careerhunter.filters import PostingText
from careerhunter.models import Dating, Posting
from tests import test_bot, test_log
from tests.conftest import FakeProbe
from tests.test_bot import AD_URL, SHELL_URL, Env
from tests.test_log import DAY, Pages, _rows
from tests.test_watch import Scripted, company, fetched, url

env = test_bot.env  # the bot fixture, shared with test_bot
ready = test_log.ready  # an initialised data folder with an active claim
pages = test_log.pages  # the page fetcher, never a socket

RULES = {"onsite_hybrid": ["Example City"], "remote": ["Exampleland"]}


# --- a Location: line ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("About us\nLocation: Example Town, Region\nDuties", "Example Town, Region"),
        ("Job location - Example Town", "Example Town"),
        ("Work Location: Example Town.", "Example Town"),
        ("  Based in: Example Town ", "Example Town"),
        ("Locations: Example Town; Sample Village", "Example Town; Sample Village"),
        ("Location-based services are our business.", None),
        ("Our location is great.", None),
        ("We are based in Example Town.", None),
        ("", None),
        (None, None),
    ],
)
def test_a_location_line_is_read_only_at_the_start_of_a_line(
    text: str | None, found: str | None
) -> None:
    assert locations.from_text(text) == found


def test_a_long_location_line_is_cut() -> None:
    found = locations.from_text("Location: " + "Example " * 40)
    assert found is not None
    assert len(found) <= locations.MAX_LOCATION


# --- JSON-LD ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "found"),
    [
        (
            {
                "jobLocation": {
                    "address": {"addressLocality": "Example Town", "addressRegion": "ER"}
                }
            },
            "Example Town, ER",
        ),
        (
            {
                "jobLocationType": "TELECOMMUTE",
                "applicantLocationRequirements": {"@type": "Country", "name": "Exampleland"},
            },
            "Remote, Exampleland",
        ),
        (
            {
                "jobLocationType": ["TELECOMMUTE"],
                "applicantLocationRequirements": [{"name": "Exampleland"}, {"name": "Sampleland"}],
                "jobLocation": {"address": {"addressLocality": "Example Town"}},
            },
            "Remote, Exampleland, Sampleland, Example Town",
        ),
        ({"jobLocationType": "TELECOMMUTE"}, "Remote"),
        (
            {"jobLocation": [{"address": {"addressCountry": {"name": "Exampleland"}}}]},
            "Exampleland",
        ),
        ({"jobLocation": {"address": {"addressCountry": "Exampleland"}}}, "Exampleland"),
        ({"title": "No place"}, None),
    ],
)
def test_json_ld_gives_the_address_remote_and_the_countries(
    job: dict[str, Any], found: str | None
) -> None:
    assert locations.from_json_ld(job) == found


# --- the ATS's JSON ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "data", "found"),
    [
        (
            "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/1",
            {"id": 1, "title": "Analyst", "location": {"name": "Example Town"}},
            "Example Town",
        ),
        (
            "https://api.lever.co/v0/postings/samplelogistics/abc",
            {
                "id": "abc",
                "text": "Analyst",
                "categories": {"location": "Example Town"},
                "workplaceType": "remote",
            },
            "Remote, Example Town",
        ),
        (
            "https://api.ashbyhq.com/posting-api/job-board/example.labs",
            {"id": "x", "title": "Analyst", "location": "Example Town"},
            "Example Town",
        ),
        (
            "https://examplecorp.wd5.myworkdayjobs.com/wday/cxs/examplecorp/Careers/job/x",
            {"jobPostingInfo": {"location": "Example Town"}},
            "Example Town",
        ),
        ("https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/1", {"id": 1}, None),
        ("https://example.com/other", {"location": "Example Town"}, None),
    ],
)
def test_the_ats_json_location_is_read_like_the_board(
    endpoint: str, data: object, found: str | None
) -> None:
    assert jobdesc._ats_location(endpoint, data) == found


def test_one_ats_request_gives_the_jd_and_the_location() -> None:
    link = "https://job-boards.greenhouse.io/examplerobotics/jobs/77"
    api = "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/77"
    calls: list[str] = []

    def fetcher(address: str) -> Fetched:
        calls.append(address)
        if address == link:
            return Fetched(address, 200, "text/html", b"<p>No JSON-LD here.</p>")
        body = {
            "id": 77,
            "title": "Analyst",
            "content": "Duties.",
            "location": {"name": "Example Town"},
        }
        return Fetched(address, 200, "application/json", json.dumps(body).encode())

    got = jobdesc.fetched_with_location(link, fetcher)
    assert calls == [link, api]
    assert got.location == "Example Town"
    assert got.jd is not None
    assert got.jd.text == "Duties."


# --- careerhunter log ----------------------------------------------------------------------


def test_log_takes_a_location_by_hand(
    ready: Path,
    probe: FakeProbe,
    pages: Pages,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.main(
            [
                "log",
                AD_URL,
                "--method",
                "email",
                "--date",
                DAY,
                "--location",
                "  Sample   Village ",
            ],
            probe=probe,
        )
        == 0
    )
    assert "Location: Sample Village" in capsys.readouterr().out.splitlines()
    assert _rows(ready / "search.db", "SELECT location FROM postings") == [("Sample Village",)]


def test_log_refuses_a_location_too_long(
    ready: Path,
    probe: FakeProbe,
    pages: Pages,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        ["log", AD_URL, "--method", "email", "--date", DAY, "--location", "x" * 200],
        probe=probe,
    )
    assert code == cli.EXIT_ERROR
    assert "longer than 120 characters" in capsys.readouterr().err


def test_log_reads_a_location_line_when_the_page_has_none(
    ready: Path,
    probe: FakeProbe,
    pages: Pages,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pages.body = (
        b'<script type="application/ld+json">{"@type": "JobPosting", "title": "Analyst", '
        b'"hiringOrganization": {"name": "Example Robotics"}, '
        b'"description": "Duties.\\nLocation: Example Town"}</script>'
    )
    assert cli.main(["log", AD_URL, "--method", "email", "--date", DAY], probe=probe) == 0
    assert "Location: Example Town" in capsys.readouterr().out.splitlines()


# --- the bot's /log ------------------------------------------------------------------------


def _location(env: Env) -> list[str]:
    conn = db.connect_read_only(env.datadir.search_db_path)
    try:
        return [str(r[0]) for r in conn.execute("SELECT location FROM postings ORDER BY id")]
    finally:
        conn.close()


def test_a_labelled_location_goes_anywhere_after_the_link(env: Env) -> None:
    env.say(
        f"/log {SHELL_URL} | location: Sample Village | email | 2026-10-21 "
        "| Sample Logistics | Synthetic Clerk"
    )
    assert "Location: Sample Village" in env.api.texts()[-2]
    assert _location(env) == ["Sample Village"]


def test_a_labelled_location_wins_over_the_page(env: Env) -> None:
    env.say(f"/log {AD_URL} | email | 2026-10-21 | LOCATION:  Sample Village")
    assert _location(env) == ["Sample Village"]


def test_a_bare_log_keeps_its_location_through_the_buttons(env: Env) -> None:
    env.say(f"/log {AD_URL} | location: Sample Village")
    env.press("Email")
    env.press("Today")
    assert _location(env) == ["Sample Village"]


@pytest.mark.parametrize(
    ("line", "refusal"),
    [
        ("| email | 2026-10-21 | location:", "The location after location: is empty."),
        ("| location: A | email | 2026-10-21 | location: B", "Give one location only."),
    ],
)
def test_a_bad_location_field_is_refused(env: Env, line: str, refusal: str) -> None:
    env.say(f"/log {AD_URL} {line}")
    assert env.api.texts()[-1].startswith(refusal)
    assert env.counts()["search.postings"] == 0


# --- "remote" in the title ----------------------------------------------------------------


def _decide(title: str, location: str, work_model: str | None = None) -> str | None:
    return filters.decide(
        PostingText(
            company="Example Robotics", title=title, location=location, work_model=work_model
        ),
        config={"search": {"location_rules": RULES}},
        exclusions={},
        role_filters=None,
        applied=False,
    ).reason


def test_remote_in_the_title_is_read_as_remote() -> None:
    assert _decide("Analyst - Exampleland Wide - Remote", "Sample Village") is None
    assert _decide("Analyst (Remote)", "Exampleland") is None
    assert _decide("Analyst", "Sample Village") == "location_rule"  # no remote anywhere
    assert _decide("Remote Analyst", "Sample Village") == "location_rule"  # no region named


def test_a_board_work_model_still_wins_over_the_title() -> None:
    assert _decide("Analyst - Remote", "Sample Village", work_model="onsite") == "location_rule"


def test_role_filters_see_the_title_remote_too() -> None:
    verdict = filters.decide(
        PostingText(company="Example Robotics", title="Analyst - Remote", location="Exampleland"),
        config={"search": {"location_rules": RULES}},
        exclusions={},
        role_filters={"work_models": ["onsite"]},
        applied=False,
    )
    assert verdict.reason == "role_filter"


# --- the watcher: a board job with no location -------------------------------------------


def test_a_board_job_with_no_location_takes_its_description_line(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    board = url("greenhouse", "examplerobotics")
    job = {
        "id": 5,
        "title": "Analyst",
        "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/5",
        "location": {"name": ""},
        "content": "&lt;p&gt;Duties.&lt;/p&gt;&lt;p&gt;Location: Example City&lt;/p&gt;",
    }
    lines = watch.run(
        conn,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today="2026-11-02",
        fetcher=Scripted({board: fetched({"jobs": [job]})}),
        config={"search": {"location_rules": RULES}},
    )
    assert any("'Analyst': location read from its description" in line for line in lines)
    (row,) = conn.execute("SELECT location FROM postings").fetchall()
    assert row[0] == "Example City"
    conn.close()


# --- doctor's count ----------------------------------------------------------------------


def test_doctor_counts_postings_with_location_unknown(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    for n, place in enumerate(["", "Example City", "  "]):
        search_store.find_or_create_posting(
            conn,
            Posting(
                company="Example Robotics",
                title=f"Analyst {n}",
                first_seen="2026-11-02",
                location=place,
                url=f"https://jobs.example.com/{n}",
                source="watcher",
            ),
            Dating("exact", day="2026-11-02"),
        )
    assert search_store.location_unknown_count(conn) == 2
    conn.close()
