"""A posting URL found by discovery alerts directly (#41). Synthetic boards; no sockets."""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

import pytest

from careerhunter import ats, discover, lanes, search_store
from careerhunter import watch as w
from careerhunter.ats import Board, PostingRef
from careerhunter.fetch import Fetched, FetchFailed
from careerhunter.filters import PostingText
from careerhunter.score import Score
from tests.test_watch import DAY, Scripted, fetched, url
from tests.test_watch_ats import (
    Posted,
    bh_opening,
    routes,
    sr_ad,
    sr_posting,
    wd_info,
)

FITS = {
    "fit_rubric": {
        "gap_tiers": {
            "tiers": [{"name": "strong", "max_gaps": 1, "fit": "high"}],
            "exclude_from_gaps": 3,
        }
    }
}
LEVER_ID = "0b6a1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d"
LEVER = PostingRef(Board("lever", "freshlogistics"), LEVER_ID)
LEVER_API = f"https://api.lever.co/v0/postings/freshlogistics/{LEVER_ID}?mode=json"
LEVER_URL = f"https://jobs.lever.co/freshlogistics/{LEVER_ID}"
_MS = int(dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC).timestamp() * 1000)


def lever_posting(title: str = "Operations Coordinator", **extra: object) -> dict[str, object]:
    return {
        "id": LEVER_ID,
        "text": title,
        "hostedUrl": LEVER_URL,
        "categories": {"location": "Example City"},
        "createdAt": _MS,
        "descriptionPlain": "A friendly team.",
        **extra,
    }


def always_strong(posting: PostingText) -> Score:
    return Score(llm_output="{}", gaps="[]", tier="strong")


def found(*refs: PostingRef) -> Any:
    def discovery(conn: sqlite3.Connection) -> discover.Pass:
        return discover.Pass(["discover: 0 queued"], refs)

    return discovery


def run(conn: sqlite3.Connection, fetcher: Any, *refs: PostingRef, **kw: Any) -> list[str]:
    kw.setdefault("config", FITS)
    kw.setdefault("scorer", always_strong)
    return w.run(conn, [], today=DAY, fetcher=fetcher, discovery=found(*refs), **kw)


def stored(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        "SELECT p.title, p.url, p.source, p.fit, p.score_run_id, c.name, c.watchlist "
        "FROM postings p JOIN companies c ON c.id = p.company_id ORDER BY p.id"
    )
    return [tuple(row) for row in rows]


def test_a_posting_url_hit_alerts_with_no_approval(search: sqlite3.Connection) -> None:
    fetcher = Scripted({LEVER_API: fetched(lever_posting())})
    lines = run(search, fetcher, LEVER)
    assert fetcher.calls == [LEVER_API]
    ((title, address, source, fit, run_id, company, watchlist),) = stored(search)
    assert (title, address, source, fit) == (
        "Operations Coordinator",
        LEVER_URL,
        "discovery",
        "high",
    )
    assert (company, watchlist) == ("freshlogistics", 0)  # not watched, not approved
    assert search.execute("SELECT count(*) FROM discovery_candidates").fetchone()[0] == 0
    (alert,) = search_store.pending_alerts(search)
    assert (alert.run_id, alert.run_finished) == (run_id, True)  # in this run's digest
    assert "watch: freshlogistics (lever): 1 new, 0 already recorded" in lines


def test_a_stored_posting_is_not_fetched_again(search: sqlite3.Connection) -> None:
    run(search, Scripted({LEVER_API: fetched(lever_posting())}), LEVER)
    again = Scripted({})
    run(search, again, LEVER)
    assert again.calls == []
    assert len(stored(search)) == 1


def test_a_discovered_posting_goes_through_the_gates_and_the_age_gate(
    search: sqlite3.Connection,
) -> None:
    senior = Scripted({LEVER_API: fetched(lever_posting("Senior Operations Coordinator"))})
    run(search, senior, LEVER, config={**FITS, "hard_gates": {"seniority_words": ["Senior"]}})
    reasons = [str(r[0]) for r in search.execute("SELECT reason FROM skips")]
    assert reasons == ["seniority"]
    search.execute("DELETE FROM skips")
    search.execute("DELETE FROM postings")
    stale = int(dt.datetime(2026, 10, 1, tzinfo=dt.UTC).timestamp() * 1000)
    run(search, Scripted({LEVER_API: fetched(lever_posting(createdAt=stale))}), LEVER)
    assert [str(r[0]) for r in search.execute("SELECT reason FROM skips")] == ["too_old"]
    assert search_store.pending_alerts(search) == []


def test_an_off_lane_discovered_posting_below_the_threshold_is_held(
    search: sqlite3.Connection,
) -> None:
    config = {
        **FITS,
        "search": {
            "lanes": [{"name": "Support", "priority": 1, "titles": ["Support"]}],
            "off_lane_min_fit": "high",
        },
        "fit_rubric": {
            "gap_tiers": {
                "tiers": [{"name": "strong", "max_gaps": 1, "fit": "medium"}],
                "exclude_from_gaps": 3,
            }
        },
    }
    run(search, Scripted({LEVER_API: fetched(lever_posting())}), LEVER, config=config)
    pending = search_store.pending_alerts(search)
    assert len(pending) == 1 and pending[0].lane is None and pending[0].lane_checked
    assert lanes.alert_order(pending, config) == []  # off-lane and medium: held


def test_a_discovered_posting_with_no_fit_is_held(search: sqlite3.Connection) -> None:
    def unknown_tier(posting: PostingText) -> Score:
        return Score(llm_output="{}", gaps="[]", tier="renamed")

    run(search, Scripted({LEVER_API: fetched(lever_posting())}), LEVER, scorer=unknown_tier)
    assert search_store.pending_alerts(search) == []


@pytest.mark.parametrize(
    ("reply", "line"),
    [
        (fetched(status=404, body=b"{}"), "posting: HTTP 404; nothing recorded"),
        (fetched(body=b"<html>"), "posting: invalid JSON; nothing recorded"),
        (fetched({"id": LEVER_ID}), "posting: a job has no title; nothing recorded"),
        (FetchFailed("couldn't fetch api.lever.co: OSError"), "posting: FetchFailed; nothing"),
    ],
)
def test_a_posting_that_cannot_be_read_stores_nothing(
    search: sqlite3.Connection, reply: Fetched | Exception, line: str
) -> None:
    lines = run(search, Scripted({LEVER_API: reply}), LEVER)
    assert any(text.startswith(f"watch: freshlogistics (lever): {line}") for text in lines)
    assert stored(search) == []
    assert lines[-1] == "watch: 0 boards fetched, 0 failed, 0 left"


def test_a_429_on_a_posting_blocks_the_host_for_the_run(search: sqlite3.Connection) -> None:
    other = PostingRef(Board("lever", "freshlogistics"), "1b6a1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d")
    fetcher = Scripted({LEVER_API: fetched(status=429, body=b"", retry_after="9")})
    lines = run(search, fetcher, LEVER, other)
    assert fetcher.calls == [LEVER_API]
    assert (
        "watch: freshlogistics (lever): posting: api.lever.co already returned HTTP 429 this run"
    ) in lines


def test_postings_past_the_per_run_limit_are_left(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(w, "DISCOVERED_PER_RUN", 1)
    other = PostingRef(Board("lever", "freshlogistics"), "1b6a1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d")
    lines = run(search, Scripted({LEVER_API: fetched(lever_posting())}), LEVER, other)
    assert "watch: discovery: 1 postings left (limit 1)" in lines
    assert len(stored(search)) == 1


def test_a_discovery_crash_records_no_posting(search: sqlite3.Connection) -> None:
    def broken(conn: sqlite3.Connection) -> discover.Pass:
        raise RuntimeError("synthetic")

    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), discovery=broken)
    assert "watch: discovery: RuntimeError; continuing" in lines
    assert stored(search) == []


# --- one posting through each ATS's API -----------------------------------------------


def _greenhouse() -> tuple[PostingRef, dict[str, Fetched | Exception]]:
    job = {
        "id": 4242,
        "title": "Operations Coordinator",
        "absolute_url": "https://job-boards.greenhouse.io/newrobotics/jobs/4242",
        "location": {"name": "Example City"},
        "content": "&lt;p&gt;A friendly team.&lt;/p&gt;",
        "first_published": "2026-10-30T09:00:00-04:00",
    }
    api = "https://boards-api.greenhouse.io/v1/boards/newrobotics/jobs/4242"
    return PostingRef(Board("greenhouse", "newrobotics"), "4242"), {api: fetched(job)}


def _ashby() -> tuple[PostingRef, dict[str, Fetched | Exception]]:
    jobs = [
        {"id": "uuid-0", "title": "Other", "jobUrl": "https://jobs.ashbyhq.com/exampleorg/uuid-0"},
        {
            "id": "uuid-1",
            "title": "Operations Coordinator",
            "jobUrl": "https://jobs.ashbyhq.com/exampleorg/uuid-1",
            "location": "Example City",
            "publishedAt": "2026-10-30T16:21:55.393+00:00",
        },
    ]
    board = url("ashby", "exampleorg")
    return PostingRef(Board("ashby", "exampleorg"), "uuid-1"), {board: fetched({"jobs": jobs})}


def _smartrecruiters() -> tuple[PostingRef, dict[str, Fetched | Exception]]:
    ident = "744000000000007"
    detail = {**sr_posting(ident), **sr_ad(ident, "A friendly team.")}
    api = f"https://api.smartrecruiters.com/v1/companies/examplerobotics/postings/{ident}"
    return PostingRef(Board("smartrecruiters", "examplerobotics"), ident), routes({api: detail})


def _bamboohr() -> tuple[PostingRef, dict[str, Fetched | Exception]]:
    api = "https://examplerobotics.bamboohr.com/careers/41/detail"
    return PostingRef(Board("bamboohr", "examplerobotics"), "41"), routes(
        {api: bh_opening("A friendly team.")}
    )


def _workday() -> tuple[PostingRef, dict[str, Fetched | Exception]]:
    path = "/job/Example-City/Operations-Coordinator_JR00009"
    base = "https://examplerobotics.wd5.myworkdayjobs.com/wday/cxs/examplerobotics/External"
    board = Board("workday", "examplerobotics", "wd5", "External")
    return PostingRef(board, path), routes({f"{base}{path}": wd_info("A friendly team.")})


@pytest.mark.parametrize(
    ("make", "expected_url"),
    [
        (_greenhouse, "https://job-boards.greenhouse.io/newrobotics/jobs/4242"),
        (_ashby, "https://jobs.ashbyhq.com/exampleorg/uuid-1"),
        (_smartrecruiters, "https://jobs.smartrecruiters.com/examplerobotics/744000000000007"),
        (_bamboohr, "https://examplerobotics.bamboohr.com/careers/41"),
        (
            _workday,
            "https://examplerobotics.wd5.myworkdayjobs.com/External/job/Example-City/"
            "Operations-Coordinator_JR00009",
        ),
    ],
)
def test_one_posting_is_read_through_each_ats(
    search: sqlite3.Connection, make: Any, expected_url: str
) -> None:
    ref, replies = make()
    fetcher = Scripted(replies)
    run(search, fetcher, ref)
    ((title, address, source, fit, *_rest),) = stored(search)
    assert (title, address, source, fit) == (
        "Operations Coordinator",
        expected_url,
        "discovery",
        "high",
    )
    assert len(fetcher.calls) == 1  # fetched once


def test_an_ashby_posting_missing_from_its_board_stores_nothing(search: sqlite3.Connection) -> None:
    ref, replies = _ashby()
    gone = PostingRef(ref.board, "uuid-9")
    lines = run(search, Scripted(replies), gone)
    assert (
        "watch: exampleorg (ashby): posting: the posting is not on its board; nothing recorded"
        in lines
    )


# --- posting URLs in search results ---------------------------------------------------


@pytest.mark.parametrize(
    ("address", "ats_type", "slug", "ref"),
    [
        (
            "https://job-boards.greenhouse.io/newrobotics/jobs/4242",
            "greenhouse",
            "newrobotics",
            "4242",
        ),
        (
            "https://boards.greenhouse.io/newrobotics/jobs/4242?gh_src=x",
            "greenhouse",
            "newrobotics",
            "4242",
        ),
        (f"https://jobs.lever.co/freshlogistics/{LEVER_ID}", "lever", "freshlogistics", LEVER_ID),
        ("https://jobs.ashbyhq.com/exampleorg/uuid-1", "ashby", "exampleorg", "uuid-1"),
        (
            "https://jobs.smartrecruiters.com/ExampleRobotics/744000000000007-operations-lead",
            "smartrecruiters",
            "ExampleRobotics",
            "744000000000007",
        ),
        ("https://examplerobotics.bamboohr.com/careers/41", "bamboohr", "examplerobotics", "41"),
        (
            "https://examplerobotics.wd5.myworkdayjobs.com/en-US/External/job/Example-City/"
            "Clerk_JR00009",
            "workday",
            "examplerobotics",
            "/job/Example-City/Clerk_JR00009",
        ),
    ],
)
def test_posting_urls_name_their_posting(address: str, ats_type: str, slug: str, ref: str) -> None:
    found = discover.posting(address)
    assert found is not None
    assert (found.board.ats, found.board.slug, found.ref) == (ats_type, slug, ref)


@pytest.mark.parametrize(
    "address",
    [
        "https://boards.greenhouse.io/newrobotics",
        "https://boards.greenhouse.io/newrobotics/jobs/abc",
        "https://jobs.lever.co/freshlogistics",
        "https://jobs.smartrecruiters.com/ExampleRobotics",
        "https://examplerobotics.bamboohr.com/careers",
        "https://examplerobotics.wd5.myworkdayjobs.com/External",
        "https://jobs.example.com/careers/1",
    ],
)
def test_board_and_other_urls_name_no_posting(address: str) -> None:
    assert discover.posting(address) is None


def test_a_search_pass_hands_its_posting_urls_to_the_watch_run(tmp_path: Any) -> None:
    import json

    from careerhunter import db

    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    body = json.dumps(
        {"results": [{"url": f"https://jobs.lever.co/freshlogistics/{LEVER_ID}"}] * 2}
    ).encode()
    config = {
        "discovery": {"searxng_url": "http://127.0.0.1:8888/search", "queries_per_run": 1},
        "search": {"target_titles": ["One"]},
    }
    outcome = discover.run(conn, config, today="2026-11-02", transport=lambda u: (200, body))
    assert outcome.postings == (LEVER,)  # once, however often it was found
    conn.close()


def test_posting_urls_refuse_unsafe_references() -> None:
    assert ats.posting_url(PostingRef(Board("greenhouse", "x"), "12/../3")) is None
    assert ats.posting_url(PostingRef(Board("bamboohr", "x"), "abc")) is None
    wd = Board("workday", "x", "wd5", "External")
    assert ats.posting_url(PostingRef(wd, "/job/../wday")) is None
    assert ats.posting_url(PostingRef(Board("workday", "x"), "/job/a")) is None
    with pytest.raises(ats.BoardParseError):
        ats.read_posting(PostingRef(wd, "/admin"), ats.Client(Scripted({})))


def test_a_workday_posting_needs_no_post_client(search: sqlite3.Connection) -> None:
    ref, replies = _workday()
    fetcher = Posted(replies, {})
    w.run(search, [], today=DAY, fetcher=fetcher, discovery=found(ref), config=FITS)
    assert fetcher.posts == []
    assert len(stored(search)) == 1


def test_postings_the_run_budget_cannot_reach_are_left(search: sqlite3.Connection) -> None:
    from careerhunter.deadline import Deadline
    from tests.fakes import FakeClock

    fetcher = Scripted({})
    lines = run(search, fetcher, LEVER, budget=Deadline(0, FakeClock()))
    assert fetcher.calls == []
    assert "watch: discovery: 1 postings run budget reached; left for the next run" in lines
