"""The discovery queue (#41): confirmed boards only, exclusions honoured, gone boards never
queued again, career pages never queued. Synthetic boards and companies; no sockets."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import ats, db, discover
from careerhunter.ats import Board, Confirmed
from careerhunter.cli import _confirmer as real_confirmer  # before conftest replaces it
from careerhunter.fetch import Fetched, FetchFailed
from tests.test_watch import Scripted, fetched
from tests.test_watch_ats import Posted

CONFIG = {
    "discovery": {
        "searxng_url": "http://127.0.0.1:8888/search",
        "queries_per_run": 1,
        "career_hosts": ["jobs.example.com"],
    },
    "search": {"target_titles": ["One"]},
}
NEW = "https://job-boards.greenhouse.io/newrobotics/jobs/42"


def answer(*urls: str) -> Any:
    body = json.dumps({"results": [{"url": address} for address in urls]}).encode()
    return lambda url: (200, body)


class Asked:
    """A confirm that answers from a table and records every board it was asked about."""

    def __init__(self, table: dict[str, Confirmed | Exception] | None = None) -> None:
        self.table = table or {}
        self.boards: list[Board] = []

    def __call__(self, board: Board) -> Confirmed:
        self.boards.append(board)
        found = self.table.get(board.slug, Confirmed(True))
        if isinstance(found, Exception):
            raise found
        return found


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    connection = db.connect(tmp_path / "search.db")
    db.migrate(connection, "search")
    yield connection
    connection.close()


def rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT kind, ats_type, ats_slug, status FROM discovery_candidates ORDER BY id"
        )
    ]


def run(conn: sqlite3.Connection, confirm: Any, *urls: str, **kw: Any) -> discover.Outcome:
    conn.execute("UPDATE discovery_state SET cursor = 0, failures = 0, backoff_until = NULL")
    return discover.run(
        conn, CONFIG, today="2026-11-02", transport=answer(*urls), confirm=confirm, **kw
    )


def test_a_confirmed_board_is_queued(conn: sqlite3.Connection) -> None:
    asked = Asked()
    outcome = run(conn, asked, NEW)
    assert [board.slug for board in asked.boards] == ["newrobotics"]
    assert rows(conn) == [("board", "greenhouse", "newrobotics", "pending")]
    assert outcome.notes == ()


def test_an_excluded_company_is_never_queued_or_asked_about(conn: sqlite3.Connection) -> None:
    asked = Asked()
    outcome = run(conn, asked, NEW, exclusions={"companies": ["New Robotics"]})
    assert asked.boards == []  # matched on the slug, before any request
    assert rows(conn) == []
    assert outcome.notes == ("discover: newrobotics (greenhouse): excluded; not queued",)


def test_a_company_excluded_under_a_name_unlike_its_slug_is_not_queued(
    conn: sqlite3.Connection,
) -> None:
    """The board API's own name for the company is checked after the request."""
    asked = Asked({"newrobotics": Confirmed(True, ("Example Holdings Inc.",))})
    outcome = run(conn, asked, NEW, exclusions={"companies": ["example holdings inc"]})
    assert [board.slug for board in asked.boards] == ["newrobotics"]
    assert rows(conn) == []
    assert outcome.notes == ("discover: newrobotics (greenhouse): excluded; not queued",)


def test_a_board_that_does_not_exist_is_gone_and_never_queued_again(
    conn: sqlite3.Connection,
) -> None:
    asked = Asked({"newrobotics": Confirmed(False)})
    outcome = run(conn, asked, NEW)
    assert rows(conn) == [("board", "greenhouse", "newrobotics", "gone")]
    assert outcome.rows == ()
    assert outcome.notes == (
        "discover: newrobotics (greenhouse): the board does not exist; never queued",
    )
    again = Asked()
    outcome = run(conn, again, NEW)
    assert again.boards == []  # not even asked again
    assert rows(conn) == [("board", "greenhouse", "newrobotics", "gone")]
    assert discover.due(conn, notified_before="2026-12-01T00:00:00Z") == []
    assert discover.pending(conn) == []


def test_an_unconfirmed_board_is_not_queued_and_is_asked_about_next_time(
    conn: sqlite3.Connection,
) -> None:
    for reply, kind in (
        (FetchFailed("couldn't fetch boards-api.greenhouse.io: OSError"), "FetchFailed"),
        (ats.BoardHTTPError("https://boards-api.example/x", 500, None), "HTTP 500"),
        (ats.BoardParseError("invalid JSON"), "BoardParseError"),
    ):
        asked = Asked({"newrobotics": reply})
        outcome = run(conn, asked, NEW)
        assert rows(conn) == []
        assert outcome.notes == (
            f"discover: newrobotics (greenhouse): not confirmed ({kind}); not queued",
        )
    asked = Asked()
    run(conn, asked, NEW)
    assert rows(conn) == [("board", "greenhouse", "newrobotics", "pending")]


def test_a_smartrecruiters_company_with_no_postings_is_dropped_not_gone(
    conn: sqlite3.Connection,
) -> None:
    hit = "https://jobs.smartrecruiters.com/ExampleRobotics/744000000000001-clerk"
    asked = Asked({"ExampleRobotics": Confirmed(None)})
    outcome = run(conn, asked, hit)
    assert rows(conn) == []
    assert outcome.notes == (
        "discover: ExampleRobotics (smartrecruiters): no postings to confirm it; not queued",
    )


def test_a_career_page_is_never_queued(conn: sqlite3.Connection) -> None:
    asked = Asked()
    outcome = run(conn, asked, "https://jobs.example.com/careers")
    assert rows(conn) == []
    assert asked.boards == []
    assert outcome.notes == ()


def test_without_confirm_nothing_is_queued(conn: sqlite3.Connection) -> None:
    outcome = run(conn, None, NEW)
    assert rows(conn) == []
    assert outcome.notes == (
        "discover: newrobotics (greenhouse): not confirmed this pass; not queued",
    )


def test_boards_past_the_confirm_limit_wait_for_a_later_pass(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discover, "CONFIRM_PER_PASS", 1)
    other = "https://jobs.lever.co/freshlogistics/abc"
    asked = Asked()
    outcome = run(conn, asked, NEW, other)
    assert [board.slug for board in asked.boards] == ["newrobotics"]
    assert rows(conn) == [("board", "greenhouse", "newrobotics", "pending")]
    assert outcome.notes == (
        "discover: freshlogistics (lever): not confirmed this pass; not queued",
    )


def test_the_watch_runs_pass_logs_its_queue_notes(
    data_env: Path, probe: Any, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    import yaml

    from careerhunter import cli

    assert cli.main(["init"], probe=probe) == 0
    path = data_env / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["watchlist"] = []
    doc["discovery"]["searxng_url"] = "http://127.0.0.1:8888/search"
    path.write_text(yaml.safe_dump(doc))
    (data_env / "exclusions.yaml").write_text("companies:\n  - newrobotics\n")
    monkeypatch.setattr(discover, "default_transport", lambda url, **kw: answer(NEW)(url))
    fetched_urls: list[str] = []

    def no_posting(address: str, **kw: object) -> Fetched:
        fetched_urls.append(address)
        return fetched(status=404, body=b"{}")

    monkeypatch.setattr(cli, "fetch_url", no_posting)  # the posting the hit names
    capsys.readouterr()
    assert cli.main(["watch"], probe=probe) == 0
    err = capsys.readouterr().err
    assert "discover: newrobotics (greenhouse): excluded; not queued" in err
    assert fetched_urls == ["https://boards-api.greenhouse.io/v1/boards/newrobotics/jobs/42"]


# --- one board-API request per board ---------------------------------------------------


def _confirm(board: Board, replies: dict[str, Fetched | Exception], **kw: Any) -> Confirmed:
    fetcher = Scripted(replies)
    return ats.confirm_board(board, ats.Client(fetcher, **kw))


def test_greenhouse_is_confirmed_with_its_board_name() -> None:
    api = "https://boards-api.greenhouse.io/v1/boards/newrobotics"
    board = Board("greenhouse", "newrobotics")
    reply = fetched({"name": "New Robotics Ltd", "content": "<p>x</p>"})
    assert _confirm(board, {api: reply}) == Confirmed(True, ("New Robotics Ltd",))
    assert _confirm(board, {api: fetched(status=404, body=b"{}")}) == Confirmed(False)
    with pytest.raises(ats.BoardHTTPError):
        _confirm(board, {api: fetched(status=503, body=b"")})
    with pytest.raises(ats.BoardParseError):
        _confirm(board, {api: fetched([1])})


def test_lever_and_ashby_are_confirmed_by_their_lists() -> None:
    lever = "https://api.lever.co/v0/postings/freshlogistics?mode=json&limit=1"
    assert _confirm(Board("lever", "freshlogistics"), {lever: fetched([])}) == Confirmed(True)
    gone = {lever: fetched(status=404, body=b"")}
    assert _confirm(Board("lever", "freshlogistics"), gone) == Confirmed(False)
    ashby = "https://api.ashbyhq.com/posting-api/job-board/exampleorg"
    assert _confirm(Board("ashby", "exampleorg"), {ashby: fetched({"jobs": []})}) == Confirmed(True)
    gone = {ashby: fetched(status=404, body=b"")}
    assert _confirm(Board("ashby", "exampleorg"), gone) == Confirmed(False)


def test_smartrecruiters_gives_its_company_names_and_cannot_tell_an_empty_list() -> None:
    api = "https://api.smartrecruiters.com/v1/companies/ExampleRobotics/postings?limit=1"
    board = Board("smartrecruiters", "ExampleRobotics")
    row = {"id": "1", "company": {"identifier": "ExampleRobotics", "name": "Example Robotics"}}
    assert _confirm(board, {api: fetched({"content": [row], "totalFound": 1})}) == Confirmed(
        True, ("Example Robotics",)
    )
    assert _confirm(board, {api: fetched({"content": [], "totalFound": 0})}) == Confirmed(None)


def test_bamboohr_redirected_off_its_subdomain_is_gone() -> None:
    api = "https://examplerobotics.bamboohr.com/careers/list"
    board = Board("bamboohr", "examplerobotics")
    home = Fetched("https://www.bamboohr.com/", 200, "text/html", b"<html>")
    assert _confirm(board, {api: home}) == Confirmed(False)
    listed = Fetched(api, 200, "application/json", b'{"meta": {}, "result": []}')
    assert _confirm(board, {api: listed}) == Confirmed(True)


def test_workday_404_and_422_are_gone() -> None:
    board = Board("workday", "examplerobotics", "wd5", "External")
    for status in (404, 422):
        fetcher = Posted({}, {0: fetched(status=status, body=b"{}")})
        client = ats.Client(fetcher, poster=fetcher.post)
        assert ats.confirm_board(board, client) == Confirmed(False)
    fetcher = Posted({}, {0: fetched({"total": 0, "jobPostings": []})})
    assert ats.confirm_board(board, ats.Client(fetcher, poster=fetcher.post)) == Confirmed(True)
    assert fetcher.posts[0][1] == {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}


def test_a_board_it_cannot_build_a_url_for_is_refused() -> None:
    with pytest.raises(ats.BoardParseError):
        ats.confirm_board(Board("greenhouse", "a/b"), ats.Client(Scripted({})))


def test_the_real_confirmer_asks_through_the_board_capped_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from careerhunter import cli
    from careerhunter.deadline import Deadline

    caps: list[object] = []
    api = "https://boards-api.greenhouse.io/v1/boards/newrobotics"

    def fake_get(address: str, **kw: object) -> Fetched:
        caps.append((kw.get("max_bytes"), kw.get("budget") is not None, kw.get("accept")))
        assert address == api
        return fetched({"name": "New Robotics", "content": ""})

    monkeypatch.setattr(cli, "fetch_url", fake_get)
    confirm = real_confirmer(Deadline(150))
    assert confirm(Board("greenhouse", "newrobotics")) == Confirmed(True, ("New Robotics",))
    assert caps == [(ats.BOARD_MAX_BYTES, True, "application/json")]


# --- every pass makes progress on new boards -------------------------------------------


def _board_hits(count: int) -> list[str]:
    return [f"https://jobs.lever.co/examplecorp{n:02d}" for n in range(count)]


def test_board_hits_over_the_limit_in_the_same_order_each_pass_reach_every_board(
    conn: sqlite3.Connection,
) -> None:
    hits = _board_hits(discover.CONFIRM_PER_PASS + 3)
    first = Asked()
    run(conn, first, *hits)
    assert len(first.boards) == discover.CONFIRM_PER_PASS
    second = Asked()
    run(conn, second, *hits)  # the same hits, in the same order
    assert len(second.boards) == 3  # queued boards are left out before the limit counts
    assert len(rows(conn)) == discover.CONFIRM_PER_PASS + 3


def test_gone_rejected_and_excluded_boards_do_not_count_against_the_limit(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discover, "CONFIRM_PER_PASS", 1)
    hits = _board_hits(4)
    run(conn, Asked({"examplecorp00": Confirmed(False)}), *hits)  # gone
    conn.execute("UPDATE discovery_candidates SET status = 'gone'")
    run(conn, Asked(), *hits)  # examplecorp01 queued
    conn.execute(
        "UPDATE discovery_candidates SET status = 'rejected' WHERE ats_slug = 'examplecorp01'"
    )
    asked = Asked()
    run(conn, asked, *hits, exclusions={"companies": ["examplecorp02"]})
    assert [board.slug for board in asked.boards] == ["examplecorp03"]


def test_boards_that_keep_failing_cannot_take_the_limit_every_pass(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discover, "CONFIRM_PER_PASS", 1)
    hits = _board_hits(3)
    failing = Asked(
        {
            "examplecorp00": FetchFailed("couldn't fetch api.lever.co: OSError"),
            "examplecorp01": FetchFailed("couldn't fetch api.lever.co: OSError"),
        }
    )
    for cursor in range(3):  # the cursor moves on every pass
        conn.execute("UPDATE discovery_state SET failures = 0, backoff_until = NULL")
        discover.run(
            conn,
            CONFIG,
            today="2026-11-02",
            transport=answer(*hits),
            confirm=failing,
        )
        assert conn.execute("SELECT cursor FROM discovery_state").fetchone()[0] == cursor + 1
    assert rows(conn) == [("board", "lever", "examplecorp02", "pending")]
