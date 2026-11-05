"""The ATS boards that need more than one request: list pages, then one detail per new
posting. Synthetic boards and companies only; no sockets, no sleeps."""

from __future__ import annotations

import sqlite3
from typing import Any

from careerhunter import ats
from careerhunter import watch as w
from careerhunter.fetch import Fetched, FetchFailed, FetchTimeout, FetchTooLarge
from tests.fakes import FakeClock
from tests.test_watch import DAY, Scripted, company, fetched

SR_SLUG = "examplerobotics"
SR = company("Example Robotics", "smartrecruiters", SR_SLUG)
SR_API = f"https://api.smartrecruiters.com/v1/companies/{SR_SLUG}/postings"


def sr_list(offset: int) -> str:
    return f"{SR_API}?limit={ats.SMARTRECRUITERS_PAGE}&offset={offset}"


def sr_detail(ident: str) -> str:
    return f"{SR_API}/{ident}"


def sr_posting(
    ident: str,
    *,
    title: str = "Operations Coordinator",
    location: str = "Example City",
    model: str = "onsite",
    released: str = "2026-10-30T10:00:00.000Z",
) -> dict[str, object]:
    """One list row, in the Posting API's own fields."""
    return {
        "id": ident,
        "uuid": f"uuid-{ident}",
        "name": title,
        "refNumber": f"REF{ident[-4:]}",
        "releasedDate": released,
        "company": {"identifier": SR_SLUG, "name": "Example Robotics"},
        "location": {
            "city": location,
            "region": "",
            "country": "",
            "remote": model == "remote",
            "hybrid": model == "hybrid",
            "fullLocation": location,
        },
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
    }


def sr_page(rows: list[dict[str, object]], *, total: int, offset: int = 0) -> dict[str, object]:
    return {
        "offset": offset,
        "limit": ats.SMARTRECRUITERS_PAGE,
        "totalFound": total,
        "content": rows,
    }


def sr_ad(ident: str, description: str, pay: str | None = None) -> dict[str, object]:
    """One detail reply: the job ad sections hold HTML."""
    sections: dict[str, object] = {
        "companyDescription": {"title": "Company Description", "text": "<p>We build things.</p>"},
        "jobDescription": {"title": "Job Description", "text": f"<p>{description}</p>"},
        "qualifications": {"title": "Qualifications", "text": "<ul><li>Care</li></ul>"},
        "additionalInformation": {
            "title": "Additional Information",
            "text": f"<p>{pay}</p>" if pay else "",
        },
    }
    return {
        "id": ident,
        "name": "Operations Coordinator",
        "jobAd": {"sections": sections},
        "postingUrl": f"https://jobs.smartrecruiters.com/{SR_SLUG}/{ident}-operations-coordinator",
    }


def smartrecruiters_board(
    slug: str,
    *,
    title: str = "Operations Coordinator",
    location: str = "Example City",
    model: str = "onsite",
    description: str = "A friendly team.",
    pay: str | None = None,
) -> dict[str, object]:
    """A one-posting board: its list page and its detail, by URL."""
    assert slug == SR_SLUG
    ident = "744000000000001"
    row = sr_posting(ident, title=title, location=location, model=model)
    return {
        sr_list(0): sr_page([row], total=1),
        sr_detail(ident): sr_ad(ident, description, pay),
    }


def routes(mapping: dict[str, object]) -> dict[str, Fetched | Exception]:
    return {
        address: body if isinstance(body, (Fetched, Exception)) else fetched(body)
        for address, body in mapping.items()
    }


def stored(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        "SELECT title, location, work_model, url, requisition_id FROM postings ORDER BY id"
    )
    return [tuple(row) for row in rows]


# --- SmartRecruiters -------------------------------------------------------------------


def test_smartrecruiters_pages_then_details_are_parsed(search: sqlite3.Connection) -> None:
    ids = [f"7440000000000{n:02d}" for n in range(1, 4)]
    board = {
        sr_list(0): sr_page(
            [sr_posting(ids[0]), sr_posting(ids[1], model="remote", location="Example Region")],
            total=3,
        ),
        sr_list(2): sr_page([sr_posting(ids[2], model="hybrid")], total=3, offset=2),
        **{sr_detail(i): sr_ad(i, "A friendly team.") for i in ids},
    }
    fetcher = Scripted(routes(board))
    lines = w.run(search, [SR], today=DAY, fetcher=fetcher)
    assert fetcher.calls == [sr_list(0), sr_list(2), *(sr_detail(i) for i in ids)]
    assert stored(search) == [
        (
            "Operations Coordinator",
            "Example City",
            None,
            f"https://jobs.smartrecruiters.com/{SR_SLUG}/{ids[0]}",
            ids[0],
        ),
        (
            "Operations Coordinator",
            "Example Region",
            "remote",
            f"https://jobs.smartrecruiters.com/{SR_SLUG}/{ids[1]}",
            ids[1],
        ),
        (
            "Operations Coordinator",
            "Example City",
            "hybrid",
            f"https://jobs.smartrecruiters.com/{SR_SLUG}/{ids[2]}",
            ids[2],
        ),
    ]
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"
    assert "We build things" not in "\n".join(search.iterdump())  # text is never stored


def test_smartrecruiters_stored_postings_get_no_detail_request(search: sqlite3.Connection) -> None:
    ident = "744000000000001"
    first = Scripted(routes(smartrecruiters_board(SR_SLUG)))
    w.run(search, [SR], today=DAY, fetcher=first)
    again = Scripted(routes({sr_list(0): sr_page([sr_posting(ident)], total=1)}))
    lines = w.run(search, [SR], today=DAY, fetcher=again)
    assert again.calls == [sr_list(0)]
    assert "watch: Example Robotics (smartrecruiters): 0 new, 1 already recorded" in lines


def test_smartrecruiters_details_past_the_limit_wait_for_the_next_run(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    monkeypatch.setattr(ats, "DETAIL_LIMIT", 2)
    ids = [f"7440000000000{n:02d}" for n in range(1, 5)]
    board = {
        sr_list(0): sr_page([sr_posting(i) for i in ids], total=4),
        **{sr_detail(i): sr_ad(i, "A friendly team.") for i in ids},
    }
    lines = w.run(search, [SR], today=DAY, fetcher=Scripted(routes(board)))
    assert [row[4] for row in stored(search)] == ids[:2]
    assert (
        "watch: Example Robotics (smartrecruiters): 2 postings left for the next run (detail limit)"
    ) in lines
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"
    w.run(search, [SR], today=DAY, fetcher=Scripted(routes(board)))
    assert [row[4] for row in stored(search)] == ids


def test_a_detail_that_is_gone_or_bad_leaves_out_that_posting_only(
    search: sqlite3.Connection,
) -> None:
    ids = ["744000000000001", "744000000000002", "744000000000003"]
    board = {
        sr_list(0): sr_page([sr_posting(i) for i in ids], total=3),
        sr_detail(ids[0]): fetched(status=404, body=b"{}"),
        sr_detail(ids[1]): fetched(body=b"<html>not json</html>"),
        sr_detail(ids[2]): sr_ad(ids[2], "A friendly team."),
    }
    lines = w.run(search, [SR], today=DAY, fetcher=Scripted(routes(board)))
    assert [row[4] for row in stored(search)] == [ids[2]]
    who = "watch: Example Robotics (smartrecruiters): "
    assert f"{who}'Operations Coordinator': detail HTTP 404; not recorded" in lines
    assert f"{who}'Operations Coordinator': detail invalid JSON; not recorded" in lines
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"


def test_a_detail_429_stops_the_details_blocks_the_host_and_fails_the_board(
    search: sqlite3.Connection,
) -> None:
    ids = ["744000000000001", "744000000000002", "744000000000003"]
    board = {
        sr_list(0): sr_page([sr_posting(i) for i in ids], total=3),
        sr_detail(ids[0]): sr_ad(ids[0], "A friendly team."),
        sr_detail(ids[1]): fetched(status=429, body=b"", retry_after="30"),
    }
    other = company("Example Two", "smartrecruiters", "exampletwo")
    fetcher = Scripted(routes(board))
    lines = w.run(search, [SR, other], today=DAY, fetcher=fetcher)
    assert [row[4] for row in stored(search)] == [ids[0]]  # read before the stop: kept
    who = "watch: Example Robotics (smartrecruiters): "
    assert f"{who}2 postings left for the next run (details stopped: HTTP 429)" in lines
    assert f"{who}detail HTTP 429, retry-after 30; left for the next run" in lines
    assert (
        "watch: Example Two (smartrecruiters): api.smartrecruiters.com already returned "
        "HTTP 429 this run; left for the next run"
    ) in lines
    assert sr_detail(ids[2]) not in fetcher.calls
    assert lines[-1] == "watch: 0 boards fetched, 1 failed, 0 left"


def test_a_detail_transport_failure_stops_the_details_and_fails_the_board(
    search: sqlite3.Connection,
) -> None:
    ids = ["744000000000001", "744000000000002"]
    board = {
        sr_list(0): sr_page([sr_posting(i) for i in ids], total=2),
        sr_detail(ids[0]): FetchFailed("couldn't fetch api.smartrecruiters.com: OSError"),
    }
    lines = w.run(search, [SR], today=DAY, fetcher=Scripted(routes(board)))
    assert stored(search) == []
    assert (
        "watch: Example Robotics (smartrecruiters): 2 postings left for the next run "
        "(details stopped: FetchFailed)"
    ) in lines
    assert lines[-1] == "watch: 0 boards fetched, 1 failed, 0 left"


def test_details_cut_by_the_run_budget_leave_the_board_for_the_next_run(
    search: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    ids = ["744000000000001", "744000000000002"]
    board = routes(
        {
            sr_list(0): sr_page([sr_posting(i) for i in ids], total=2),
            sr_detail(ids[0]): sr_ad(ids[0], "A friendly team."),
        }
    )

    def fetcher(address: str) -> Fetched:
        if address == sr_detail(ids[1]):
            clock.advance(3600)
            raise FetchTimeout("fetching api.smartrecruiters.com ran out of time")
        found = board[address]
        assert isinstance(found, Fetched)
        return found

    from careerhunter.deadline import Deadline

    lines = w.run(search, [SR], today=DAY, fetcher=fetcher, budget=Deadline(600, clock))
    assert [row[4] for row in stored(search)] == [ids[0]]
    assert lines[-1] == "watch: 0 boards fetched, 0 failed, 1 left"
    assert search.execute("SELECT count(*) FROM watch_board_attempts").fetchone()[0] == 0


def test_a_bad_smartrecruiters_list_logs_and_stores_nothing(search: sqlite3.Connection) -> None:
    for body in ({"content": "nope", "totalFound": 1}, {"content": []}, [1, 2]):
        lines = w.run(search, [SR], today=DAY, fetcher=Scripted(routes({sr_list(0): body})))
        assert (
            "watch: Example Robotics (smartrecruiters): HTTP 200, unexpected JSON shape; "
            "nothing recorded"
        ) in lines
        assert search.execute("SELECT count(*) FROM postings").fetchone()[0] == 0
        assert search.execute("SELECT count(*) FROM companies").fetchone()[0] == 0


def test_a_bad_smartrecruiters_row_is_a_skip_line(search: sqlite3.Connection) -> None:
    rows: list[object] = ["not an object", {"id": "744000000000009"}, {"name": "No Id"}]
    lines = w.run(
        search, [SR], today=DAY, fetcher=Scripted(routes({sr_list(0): sr_page(rows, total=3)}))
    )  # type: ignore[arg-type]
    who = "watch: Example Robotics (smartrecruiters): "
    assert f"{who}a job was not an object; skipped" in lines
    assert f"{who}a job has no title; skipped" in lines
    assert f"{who}'No Id' has no usable id; skipped" in lines


def test_a_detail_without_sections_leaves_out_that_posting(search: sqlite3.Connection) -> None:
    ident = "744000000000001"
    board = {
        sr_list(0): sr_page([sr_posting(ident)], total=1),
        sr_detail(ident): {"id": ident, "jobAd": {}},
    }
    lines = w.run(search, [SR], today=DAY, fetcher=Scripted(routes(board)))
    assert stored(search) == []
    assert (
        "watch: Example Robotics (smartrecruiters): 'Operations Coordinator': detail "
        "unexpected JSON shape; not recorded"
    ) in lines


def test_smartrecruiters_pages_stop_at_the_page_limit(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    monkeypatch.setattr(ats, "MAX_PAGES", 2)
    board = {
        sr_list(0): sr_page([sr_posting("744000000000001")], total=9),
        sr_list(1): sr_page([sr_posting("744000000000002")], total=9, offset=1),
        **{sr_detail(i): sr_ad(i, "x") for i in ("744000000000001", "744000000000002")},
    }
    lines = w.run(search, [SR], today=DAY, fetcher=Scripted(routes(board)))
    assert len(stored(search)) == 2
    assert (
        "watch: Example Robotics (smartrecruiters): page limit reached (2 pages); later "
        "postings left for the next run"
    ) in lines


def test_a_smartrecruiters_board_over_the_cap_as_a_whole_stores_nothing(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    ident = "744000000000001"
    board = routes(smartrecruiters_board(SR_SLUG))
    detail = board[sr_detail(ident)]
    assert isinstance(detail, Fetched)
    listing = board[sr_list(0)]
    assert isinstance(listing, Fetched)
    cap = len(listing.body) + len(detail.body) - 1  # each reply fits; together they don't
    monkeypatch.setattr(ats, "BOARD_MAX_BYTES", cap)
    lines = w.run(search, [SR], today=DAY, fetcher=Scripted(board))
    assert any("board over the size cap" in line for line in lines)
    assert stored(search) == []
    assert lines[-1] == "watch: 0 boards fetched, 1 failed, 0 left"


def test_an_empty_smartrecruiters_board_is_fetched_with_nothing_new(
    search: sqlite3.Connection,
) -> None:
    lines = w.run(
        search, [SR], today=DAY, fetcher=Scripted(routes({sr_list(0): sr_page([], total=0)}))
    )
    assert "watch: Example Robotics (smartrecruiters): 0 new, 0 already recorded" in lines
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"


def test_a_too_large_detail_is_never_partly_recorded(search: sqlite3.Connection) -> None:
    ids = ["744000000000001", "744000000000002"]
    board = {
        sr_list(0): sr_page([sr_posting(i) for i in ids], total=2),
        sr_detail(ids[0]): sr_ad(ids[0], "A friendly team."),
        sr_detail(ids[1]): FetchTooLarge("the page is larger than 20 MB"),
    }
    lines = w.run(search, [SR], today=DAY, fetcher=Scripted(routes(board)))
    assert stored(search) == []
    assert any("board over the size cap" in line for line in lines)


# --- BambooHR --------------------------------------------------------------------------

BH_SLUG = "examplerobotics"
BH = company("Example Robotics", "bamboohr", BH_SLUG)
BH_LIST = f"https://{BH_SLUG}.bamboohr.com/careers/list"


def bh_detail(ident: str) -> str:
    return f"https://{BH_SLUG}.bamboohr.com/careers/{ident}/detail"


def bh_job(
    ident: str,
    *,
    title: str = "Operations Coordinator",
    city: str = "Example City",
    state: str | None = None,
    country: str | None = None,
) -> dict[str, object]:
    """One list row, in the careers list's own fields."""
    return {
        "id": ident,
        "jobOpeningName": title,
        "departmentId": "12",
        "departmentLabel": "Operations",
        "employmentStatusLabel": "Full-Time",
        "location": {"city": city or None, "state": state},
        "atsLocation": {"country": country, "state": None, "province": None, "city": None},
        "isRemote": None,
        "locationType": "0",
    }


def bh_list(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"meta": {"totalCount": len(rows)}, "result": rows}


def bh_opening(description: str, pay: str | None = None) -> dict[str, object]:
    html = f"<p>{description}</p>" + (f"<p>{pay}</p>" if pay else "")
    return {
        "meta": {},
        "result": {
            "formFields": {},
            "jobOpening": {
                "jobOpeningName": "Operations Coordinator",
                "description": html,
                "datePosted": "2026-10-30",
                "location": {"city": "Example City", "state": None},
                "jobOpeningShareUrl": f"https://{BH_SLUG}.bamboohr.com/careers/41",
                "compensation": None,
            },
        },
    }


def bamboohr_board(
    slug: str,
    *,
    title: str = "Operations Coordinator",
    location: str = "Example City",
    model: str = "onsite",
    description: str = "A friendly team.",
    pay: str | None = None,
) -> dict[str, object]:
    """A one-posting board: its list and its detail, by URL. BambooHR gives no work model."""
    assert slug == BH_SLUG
    return {
        BH_LIST: bh_list([bh_job("41", title=title, city=location)]),
        bh_detail("41"): bh_opening(description, pay),
    }


def test_bamboohr_list_then_details_are_parsed(search: sqlite3.Connection) -> None:
    board = {
        BH_LIST: bh_list(
            [
                bh_job("41"),
                bh_job("42", city="Example Town", state="Example State", country="Exampleland"),
            ]
        ),
        bh_detail("41"): bh_opening("A friendly team."),
        bh_detail("42"): bh_opening("A friendly team."),
    }
    fetcher = Scripted(routes(board))
    lines = w.run(search, [BH], today=DAY, fetcher=fetcher)
    assert fetcher.calls == [BH_LIST, bh_detail("41"), bh_detail("42")]
    assert stored(search) == [
        (
            "Operations Coordinator",
            "Example City",
            None,
            f"https://{BH_SLUG}.bamboohr.com/careers/41",
            "41",
        ),
        (
            "Operations Coordinator",
            "Example Town, Example State, Exampleland",
            None,
            f"https://{BH_SLUG}.bamboohr.com/careers/42",
            "42",
        ),
    ]
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"


def test_bamboohr_stored_postings_get_no_detail_request(search: sqlite3.Connection) -> None:
    w.run(search, [BH], today=DAY, fetcher=Scripted(routes(bamboohr_board(BH_SLUG))))
    again = Scripted(routes({BH_LIST: bh_list([bh_job("41")])}))
    w.run(search, [BH], today=DAY, fetcher=again)
    assert again.calls == [BH_LIST]


def test_an_unknown_bamboohr_company_is_a_bad_reply_and_stores_nothing(
    search: sqlite3.Connection,
) -> None:
    """BambooHR sends an unknown company to its own site; that page is not JSON."""
    page = fetched(body=b"<html>synthetic marketing page</html>")
    lines = w.run(search, [BH], today=DAY, fetcher=Scripted({BH_LIST: page}))
    assert ("watch: Example Robotics (bamboohr): HTTP 200, invalid JSON; nothing recorded") in lines
    assert lines[-1] == "watch: 0 boards fetched, 1 failed, 0 left"
    assert search.execute("SELECT count(*) FROM companies").fetchone()[0] == 0


def test_bad_bamboohr_payloads_log_and_store_nothing(search: sqlite3.Connection) -> None:
    for body in ({"meta": {}}, {"result": {"id": "41"}}, [1]):
        lines = w.run(search, [BH], today=DAY, fetcher=Scripted(routes({BH_LIST: body})))
        assert (
            "watch: Example Robotics (bamboohr): HTTP 200, unexpected JSON shape; nothing recorded"
        ) in lines
    board = {BH_LIST: bh_list([bh_job("41")]), bh_detail("41"): {"result": {}}}
    lines = w.run(search, [BH], today=DAY, fetcher=Scripted(routes(board)))
    assert (
        "watch: Example Robotics (bamboohr): 'Operations Coordinator': detail unexpected "
        "JSON shape; not recorded"
    ) in lines
    assert stored(search) == []


def test_bad_bamboohr_rows_are_skip_lines(search: sqlite3.Connection) -> None:
    rows: list[object] = ["x", {"id": "43"}, {"jobOpeningName": "No Id", "id": "a/b"}]
    lines = w.run(search, [BH], today=DAY, fetcher=Scripted(routes({BH_LIST: {"result": rows}})))
    who = "watch: Example Robotics (bamboohr): "
    assert f"{who}a job was not an object; skipped" in lines
    assert f"{who}a job has no title; skipped" in lines
    assert f"{who}'No Id' has no usable id; skipped" in lines


def test_a_bamboohr_slug_must_be_one_dns_label(search: sqlite3.Connection) -> None:
    assert ats.board_url("bamboohr", "example_robotics") is None
    assert ats.board_url("bamboohr", "-example") is None
    lines = w.run(
        search,
        [company("Example Robotics", "bamboohr", "example_robotics")],
        today=DAY,
        fetcher=Scripted({}),
    )
    assert (
        "watch: Example Robotics (bamboohr): ats_slug is not a single path segment; skipped"
    ) in lines


def test_a_bamboohr_board_over_the_cap_as_a_whole_stores_nothing(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    board = routes(bamboohr_board(BH_SLUG))
    sizes = [len(reply.body) for reply in board.values() if isinstance(reply, Fetched)]
    monkeypatch.setattr(ats, "BOARD_MAX_BYTES", sum(sizes) - 1)
    lines = w.run(search, [BH], today=DAY, fetcher=Scripted(board))
    assert any("board over the size cap" in line for line in lines)
    assert stored(search) == []
