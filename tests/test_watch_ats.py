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


def test_the_watch_command_asks_bamboohr_for_json_and_an_html_200_still_fails(
    data_env: Any, probe: Any, capsys: Any, monkeypatch: Any
) -> None:
    """The Accept header is a hint; the JSON parse is the gate (#35)."""
    import yaml

    from careerhunter import cli

    assert cli.main(["init"], probe=probe) == 0
    path = data_env / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["watchlist"] = [BH]
    path.write_text(yaml.safe_dump(doc))
    asked: list[tuple[str, object]] = []

    def fake_get(address: str, **kw: object) -> Fetched:
        asked.append((address, kw.get("accept")))
        return Fetched(address, 200, "text/html", b"<html>synthetic careers page</html>")

    monkeypatch.setattr(cli, "fetch_url", fake_get)
    monkeypatch.setattr(cli, "score_posting", lambda *a, **k: None)
    assert cli.main(["watch"], probe=probe) == 0
    err = capsys.readouterr().err
    assert asked == [(BH_LIST, "application/json")]
    assert "watch: Example Robotics (bamboohr): HTTP 200, invalid JSON; nothing recorded" in err
    assert "watch: 0 boards fetched, 1 failed, 0 left" in err
    conn = sqlite3.connect(data_env / "search.db")
    try:
        assert conn.execute("SELECT count(*) FROM postings").fetchone()[0] == 0
    finally:
        conn.close()


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


# --- Workday ---------------------------------------------------------------------------

WD = company("Example Robotics", "workday", "examplerobotics", ats_host="wd5", ats_site="External")
WD_BASE = "https://examplerobotics.wd5.myworkdayjobs.com/wday/cxs/examplerobotics/External"
WD_LIST = f"{WD_BASE}/jobs"


class Posted(Scripted):
    """Scripted GETs, plus POSTs answered by the request's offset."""

    def __init__(
        self,
        mapping: dict[str, Fetched | Exception],
        pages: dict[int, Fetched | Exception] | None = None,
    ) -> None:
        super().__init__(mapping)
        self.pages = pages or {}
        self.posts: list[tuple[str, object]] = []

    def post(self, address: str, payload: object) -> Fetched:
        self.posts.append((address, payload))
        assert address == WD_LIST
        assert isinstance(payload, dict)
        found = self.pages[payload["offset"]]
        if isinstance(found, Exception):
            raise found
        return found


def wd_path(n: int) -> str:
    return f"/job/Example-City/Operations-Coordinator_JR{n:05d}"


def wd_row(
    n: int,
    *,
    title: str = "Operations Coordinator",
    where: str = "Example City",
    posted: str = "Posted Today",
) -> dict[str, object]:
    """One list row, in the Workday jobs list's own fields."""
    return {
        "title": title,
        "externalPath": wd_path(n),
        "locationsText": where,
        "postedOn": posted,
        "bulletFields": [f"JR{n:05d}"],
    }


def wd_page(rows: list[dict[str, object]], total: int = 0) -> dict[str, object]:
    return {"total": total, "jobPostings": rows, "facets": [], "userAuthenticated": False}


def wd_info(
    description: str,
    *,
    location: str = "Example City",
    pay: str | None = None,
    posted: str = "Posted Today",
) -> dict[str, object]:
    html = f"<p>{description}</p>" + (f"<p>{pay}</p>" if pay else "")
    return {
        "jobPostingInfo": {
            "id": "abc",
            "title": "Operations Coordinator",
            "jobDescription": html,
            "location": location,
            "postedOn": posted,
            "startDate": "2026-10-30",
            "timeType": "Full time",
            "jobReqId": "JR00001",
            "externalUrl": "https://examplerobotics.wd5.myworkdayjobs.com/External/job/x",
        },
        "hiringOrganization": {"name": "Example Robotics Holdings Ltd", "url": ""},
        "userAuthenticated": False,
    }


def workday_board(
    slug: str,
    *,
    title: str = "Operations Coordinator",
    location: str = "Example City",
    model: str = "onsite",
    description: str = "A friendly team.",
    pay: str | None = None,
) -> Posted:
    """A one-posting board as a fetcher. Workday gives no work model."""
    return Posted(
        routes({f"{WD_BASE}{wd_path(1)}": wd_info(description, location=location, pay=pay)}),
        {0: fetched(wd_page([wd_row(1, title=title, where=location)], total=1))},
    )


def wd_run(
    conn: sqlite3.Connection, fetcher: Posted, items: list[object] | None = None, **kwargs: Any
) -> list[str]:
    return w.run(conn, items or [WD], today=DAY, fetcher=fetcher, poster=fetcher.post, **kwargs)


def test_workday_pages_then_details_are_parsed(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    monkeypatch.setattr(ats, "WORKDAY_PAGE", 2)
    fetcher = Posted(
        routes(
            {
                f"{WD_BASE}{wd_path(n)}": wd_info("A friendly team.", location=f"Place {n}")
                for n in (1, 2, 3)
            }
        ),
        {
            0: fetched(wd_page([wd_row(1), wd_row(2, where="2 Locations")], total=3)),
            2: fetched(wd_page([wd_row(3)])),  # total is 0 after the first page
        },
    )
    lines = wd_run(search, fetcher)
    assert [payload for _, payload in fetcher.posts] == [
        {"appliedFacets": {}, "limit": 2, "offset": 0, "searchText": ""},
        {"appliedFacets": {}, "limit": 2, "offset": 2, "searchText": ""},
    ]
    assert fetcher.calls == [f"{WD_BASE}{wd_path(n)}" for n in (1, 2, 3)]
    site = "https://examplerobotics.wd5.myworkdayjobs.com/External"
    assert stored(search) == [
        ("Operations Coordinator", f"Place {n}", None, f"{site}{wd_path(n)}", None)
        for n in (1, 2, 3)
    ]
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"
    row = search.execute("SELECT ats_type, ats_slug, ats_host, ats_site FROM companies").fetchone()
    assert tuple(row) == ("workday", "examplerobotics", "wd5", "External")
    keys = [r[0] for r in search.execute("SELECT board_key FROM watch_board_attempts")]
    assert keys == ["workday:examplerobotics:wd5:external"]


def test_workday_stored_postings_get_no_detail_request(search: sqlite3.Connection) -> None:
    wd_run(search, workday_board("examplerobotics"))
    again = Posted({}, {0: fetched(wd_page([wd_row(1)], total=1))})
    lines = wd_run(search, again)
    assert again.calls == []
    assert "watch: Example Robotics (workday): 0 new, 1 already recorded" in lines


def test_workday_pages_stop_at_the_page_limit(search: sqlite3.Connection, monkeypatch: Any) -> None:
    monkeypatch.setattr(ats, "WORKDAY_PAGE", 1)
    monkeypatch.setattr(ats, "MAX_PAGES", 2)
    fetcher = Posted(
        routes({f"{WD_BASE}{wd_path(n)}": wd_info("x") for n in (1, 2)}),
        {0: fetched(wd_page([wd_row(1)], total=5)), 1: fetched(wd_page([wd_row(2)]))},
    )
    lines = wd_run(search, fetcher)
    assert len(stored(search)) == 2
    assert (
        "watch: Example Robotics (workday): page limit reached (2 pages); later postings "
        "left for the next run"
    ) in lines


def _wd_custom(path: str, title: str, where: str) -> dict[str, object]:
    return {
        "title": title,
        "externalPath": path,
        "locationsText": where,
        "postedOn": "Posted Today",
    }


def test_a_retitled_and_moved_workday_posting_with_the_same_id_is_the_stored_one(
    search: sqlite3.Connection,
) -> None:
    """The key is the trailing `_<id>`; the title and location slugs are not (#35)."""
    wd_run(search, workday_board("examplerobotics"))
    moved = "/job/Example-Town/Senior-Operations-Coordinator_JR00001"
    again = Posted(
        routes({f"{WD_BASE}{moved}": wd_info("x", location="Example Town")}),
        {0: fetched(wd_page([_wd_custom(moved, "Senior Operations Coordinator", "Example Town")]))},
    )
    lines = wd_run(search, again)
    assert again.calls == []  # known by its key: no detail request
    assert "watch: Example Robotics (workday): 0 new, 1 already recorded" in lines
    site = "https://examplerobotics.wd5.myworkdayjobs.com/External"
    assert stored(search) == [
        ("Operations Coordinator", "Example City", None, f"{site}{wd_path(1)}", None)
    ]
    keys = [r[0] for r in search.execute("SELECT ats_key FROM postings")]
    assert keys == ["workday:examplerobotics:wd5:external:JR00001"]


def test_workday_postings_of_one_requisition_with_different_suffixes_stay_apart(
    search: sqlite3.Connection,
) -> None:
    """A -N suffix is kept: JREQ00009-1 and JREQ00009-2 are two postings (#35)."""
    paths = [f"/job/Example-City/Operations-Coordinator_JREQ00009-{n}" for n in (1, 2)]
    fetcher = Posted(
        routes({f"{WD_BASE}{p}": wd_info("x") for p in paths}),  # one jobReqId in both details
        {
            0: fetched(
                wd_page([_wd_custom(p, "Operations Coordinator", "Example City") for p in paths])
            )
        },
    )
    lines = wd_run(search, fetcher)
    assert "watch: Example Robotics (workday): 2 new, 0 already recorded" in lines
    keys = [r[0] for r in search.execute("SELECT ats_key FROM postings ORDER BY id")]
    assert keys == [
        "workday:examplerobotics:wd5:external:JREQ00009-1",
        "workday:examplerobotics:wd5:external:JREQ00009-2",
    ]


def test_a_board_key_held_by_another_company_is_an_identity_conflict(
    search: sqlite3.Connection,
) -> None:
    import pytest

    from careerhunter import search_store
    from careerhunter.models import Dating, Posting

    wd_run(search, workday_board("examplerobotics"))
    other, _ = search_store.upsert_company(search, "Sample Logistics", source="watcher")
    posting = Posting(
        company="Sample Logistics", title="Clerk", first_seen=DAY, location="", source="watcher"
    )
    with pytest.raises(search_store.IdentityConflict, match="board identity"):
        search_store.find_posting(
            search,
            other,
            posting,
            Dating(precision="day", day=DAY),
            ats_key="workday:examplerobotics:wd5:external:JR00001",
        )


def test_a_workday_posting_key_is_the_same_from_discovery_and_from_the_board() -> None:
    board = ats.Board("workday", "ExampleRobotics", "wd5", "External")
    ref = "/job/Example-City/Operations-Coordinator_JREQ00009-7"
    assert ats.posting_key(ats.PostingRef(board, ref)) == (
        "workday:examplerobotics:wd5:external:JREQ00009-7"
    )
    job = ats._workday_job(_wd_custom(ref, "Operations Coordinator", "Example City"), board, None)
    assert isinstance(job, ats.Job) and job.key == ats.posting_key(ats.PostingRef(board, ref))
    for path in ("/job/Example-City/Operations-Coordinator", "/job/Example-City/Clerk_JR%2001"):
        # no usable trailing id: the whole path is the id, as before
        assert ats.posting_key(ats.PostingRef(board, path)) == (
            f"workday:examplerobotics:wd5:external:{path}"
        )


def test_workday_paging_stops_at_a_repeated_page(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    """A board that ignores the offset serves the same full page again (#35)."""
    monkeypatch.setattr(ats, "WORKDAY_PAGE", 2)
    same = wd_page([wd_row(1), wd_row(2)], total=9)
    fetcher = Posted(
        routes({f"{WD_BASE}{wd_path(n)}": wd_info("x") for n in (1, 2)}),
        {0: fetched(same), 2: fetched(same), 4: fetched(wd_page([wd_row(3)]))},
    )
    lines = wd_run(search, fetcher)
    assert [payload["offset"] for _, payload in fetcher.posts] == [0, 2]  # type: ignore[index]
    assert fetcher.calls == [f"{WD_BASE}{wd_path(n)}" for n in (1, 2)]
    assert [row[3].rsplit("_", 1)[1] for row in stored(search)] == ["JR00001", "JR00002"]
    assert (
        "watch: Example Robotics (workday): repeated page at offset 2 (the same postings as "
        "the page before); later postings left for the next run"
    ) in lines
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"


def test_an_unknown_workday_site_or_tenant_fails_and_stores_nothing(
    search: sqlite3.Connection,
) -> None:
    for status in (404, 422):
        fetcher = Posted({}, {0: fetched(status=status, body=b"{}")})
        lines = wd_run(search, fetcher)
        assert f"watch: Example Robotics (workday): HTTP {status}; nothing recorded" in lines
        assert lines[-1] == "watch: 0 boards fetched, 1 failed, 0 left"
    assert search.execute("SELECT count(*) FROM companies").fetchone()[0] == 0


def test_a_workday_post_that_fails_in_transit_stores_nothing(search: sqlite3.Connection) -> None:
    fetcher = Posted({}, {0: FetchFailed("a POST was redirected; not followed")})
    lines = wd_run(search, fetcher)
    assert "watch: Example Robotics (workday): FetchFailed; nothing recorded" in lines
    assert stored(search) == []


def test_bad_workday_payloads_log_and_store_nothing(search: sqlite3.Connection) -> None:
    for body in ({"total": 1}, {"jobPostings": {}}, [1]):
        lines = wd_run(search, Posted({}, {0: fetched(body)}))
        assert (
            "watch: Example Robotics (workday): HTTP 200, unexpected JSON shape; nothing recorded"
        ) in lines
    fetcher = Posted(
        routes({f"{WD_BASE}{wd_path(1)}": {"nope": 1}}), {0: fetched(wd_page([wd_row(1)]))}
    )
    lines = wd_run(search, fetcher)
    assert (
        "watch: Example Robotics (workday): 'Operations Coordinator': detail unexpected JSON "
        "shape; not recorded"
    ) in lines
    assert stored(search) == []


def test_bad_workday_rows_are_skip_lines(search: sqlite3.Connection) -> None:
    rows: list[object] = [
        "x",
        {"externalPath": wd_path(1)},
        {"title": "No Path"},
        {"title": "Dot Path", "externalPath": "/job/../../wday/x"},
        {"title": "Query Path", "externalPath": "/job/a?b=c"},
        {"title": "Other Path", "externalPath": "/admin/x"},
    ]
    lines = wd_run(search, Posted({}, {0: fetched({"jobPostings": rows})}))
    who = "watch: Example Robotics (workday): "
    assert f"{who}a job was not an object; skipped" in lines
    assert f"{who}a job has no title; skipped" in lines
    for title in ("No Path", "Dot Path", "Query Path", "Other Path"):
        assert f"{who}{title!r} has no usable path; skipped" in lines


def test_workday_entries_need_a_usable_identity_and_a_post_client(
    search: sqlite3.Connection,
) -> None:
    items: list[object] = [
        company("No Site", "workday", "examplerobotics", ats_host="wd5"),
        company("Bad Host", "workday", "examplerobotics", ats_host="wdx", ats_site="External"),
        company("Bad Site", "workday", "examplerobotics", ats_host="wd5", ats_site="a/b"),
        company("Bad Tenant", "workday", "example_robotics", ats_host="wd5", ats_site="External"),
    ]
    fetcher = Posted({}, {})
    lines = wd_run(search, fetcher, items)
    assert lines[:4] == [
        "watch: No Site (workday): no ats_host or ats_site; skipped",
        "watch: Bad Host (workday): ats_slug, ats_host or ats_site is not usable; skipped",
        "watch: Bad Site (workday): ats_slug, ats_host or ats_site is not usable; skipped",
        "watch: Bad Tenant (workday): ats_slug, ats_host or ats_site is not usable; skipped",
    ]
    assert fetcher.posts == []
    lines = w.run(search, [WD], today=DAY, fetcher=Scripted({}))
    assert "watch: Example Robotics (workday): no POST client; skipped" in lines


def test_a_workday_company_whose_stored_site_differs_is_refused(
    search: sqlite3.Connection,
) -> None:
    wd_run(search, workday_board("examplerobotics"))
    moved = company(
        "Example Robotics", "workday", "examplerobotics", ats_host="wd5", ats_site="Careers"
    )
    fetcher = Posted({}, {0: fetched(wd_page([]))})
    lines = w.run(search, [moved], today=DAY, fetcher=fetcher, poster=_any_post(fetcher))
    assert (
        "watch: Example Robotics (workday): stored ats identity disagrees (ats_type=workday, "
        "ats_slug=examplerobotics, ats_host=wd5, ats_site=External); skipped"
    ) in lines


def _any_post(fetcher: Posted) -> Any:
    def post(address: str, payload: object) -> Fetched:
        found = fetcher.pages[0]
        assert isinstance(found, Fetched)
        return found

    return post


def test_an_approved_discovery_workday_board_is_watched(search: sqlite3.Connection) -> None:
    from careerhunter import db, search_store

    with db.transaction(search):
        search_store.upsert_company(
            search,
            "examplerobotics",
            ats_type="workday",
            ats_slug="examplerobotics",
            ats_host="wd5",
            ats_site="External",
            watchlist=1,
            source="discovery",
        )
    fetcher = workday_board("examplerobotics")
    lines = wd_run(search, fetcher, [{"name": "Unrelated", "ats_type": "icims"}])
    assert fetcher.posts and fetcher.posts[0][0] == WD_LIST
    assert "watch: examplerobotics (workday): 1 new, 0 already recorded" in lines


def test_a_workday_board_over_the_cap_as_a_whole_stores_nothing(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    fetcher = workday_board("examplerobotics")
    sizes = [
        len(r.body)
        for r in (*fetcher.pages.values(), *fetcher.mapping.values())
        if isinstance(r, Fetched)
    ]
    monkeypatch.setattr(ats, "BOARD_MAX_BYTES", sum(sizes) - 1)
    lines = wd_run(search, fetcher)
    assert any("board over the size cap" in line for line in lines)
    assert stored(search) == []


def test_a_client_without_a_poster_refuses_a_post() -> None:
    import pytest

    with pytest.raises(ats.BoardParseError, match="no POST client"):
        ats.Client(Scripted({})).post_json(WD_LIST, {})


def test_the_watch_command_posts_workday_lists_under_the_board_cap(
    data_env: Any, probe: Any, capsys: Any, monkeypatch: Any
) -> None:
    import yaml

    from careerhunter import cli

    assert cli.main(["init"], probe=probe) == 0
    path = data_env / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["watchlist"] = [WD]
    path.write_text(yaml.safe_dump(doc))
    board = workday_board("examplerobotics")
    caps: list[object] = []

    def fake_get(address: str, **kw: object) -> Fetched:
        caps.append(kw.get("max_bytes"))
        assert kw.get("accept") == "application/json"
        return board(address)

    def fake_post(address: str, payload: object, **kw: object) -> Fetched:
        caps.append(kw.get("max_bytes"))
        assert kw.get("budget") is not None
        return board.post(address, payload)

    monkeypatch.setattr(cli, "fetch_url", fake_get)
    monkeypatch.setattr(cli, "post_json", fake_post)
    monkeypatch.setattr(cli, "score_posting", lambda *a, **k: None)
    assert cli.main(["watch"], probe=probe) == 0
    assert "watch: Example Robotics (workday): 1 new, 0 already recorded" in (
        capsys.readouterr().err
    )
    assert caps == [w.BOARD_MAX_BYTES, w.BOARD_MAX_BYTES]
