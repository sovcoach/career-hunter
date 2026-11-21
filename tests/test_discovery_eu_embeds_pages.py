"""EU board hosts, embed URLs and further result pages in discovery (#46), and a Lever
board's region (search migration 0037) from discovery to the watcher. Synthetic boards
only."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from careerhunter import ats, db, discover, snapshots, watch
from careerhunter.ats import Board, Confirmed, PostingRef
from careerhunter.config import validate_config
from careerhunter.fetch import Fetched


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "search.db")
    db.migrate(connection, "search")
    yield connection
    connection.close()


def _json(url: str, body: object) -> Fetched:
    return Fetched(url, 200, "application/json", json.dumps(body).encode(), None)


class Recorder:
    """A fetcher that answers every URL with the same JSON and records the URLs."""

    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[str] = []

    def __call__(self, url: str) -> Fetched:
        self.calls.append(url)
        return _json(url, self.body)


# --- EU and embed hosts ------------------------------------------------------------------


def test_the_eu_greenhouse_board_host_is_a_greenhouse_board() -> None:
    hit = discover.classify("https://job-boards.eu.greenhouse.io/examplerobotics", [])
    assert hit is not None
    assert (hit.kind, hit.ats_type, hit.ats_slug, hit.ats_region) == (
        "board",
        "greenhouse",
        "examplerobotics",
        None,
    )
    ref = discover.posting("https://job-boards.eu.greenhouse.io/examplerobotics/jobs/4100")
    assert ref == PostingRef(Board("greenhouse", "examplerobotics"), "4100")


def test_an_eu_greenhouse_board_is_read_through_the_usual_api() -> None:
    assert ats.list_url(Board("greenhouse", "examplerobotics")) == (
        "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs?content=true"
    )
    link = "https://job-boards.eu.greenhouse.io/examplerobotics/jobs/4100"
    assert snapshots.ats_json_url(link) == (
        "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/4100"
    )


def test_the_eu_lever_host_is_a_lever_board_in_the_eu_region() -> None:
    hit = discover.classify("https://jobs.eu.lever.co/samplelogistics", [])
    assert hit is not None
    assert (hit.ats_type, hit.ats_slug, hit.ats_region) == ("lever", "samplelogistics", "eu")
    assert hit.url == "https://jobs.eu.lever.co/samplelogistics"
    ref = discover.posting("https://jobs.eu.lever.co/samplelogistics/abc-123")
    assert ref == PostingRef(Board("lever", "samplelogistics", region="eu"), "abc-123")
    assert ats.posting_url(ref) == "https://jobs.eu.lever.co/samplelogistics/abc-123"
    global_hit = discover.classify("https://jobs.lever.co/samplelogistics", [])
    assert global_hit is not None
    assert global_hit.ats_region is None


@pytest.mark.parametrize(
    ("url", "region"),
    [
        ("https://api.lever.co/v0/postings/samplelogistics?mode=iframe", None),
        ("https://api.eu.lever.co/v0/postings/samplelogistics?mode=iframe", "eu"),
        ("https://api.lever.co/v0/postings/samplelogistics", None),
    ],
)
def test_a_lever_iframe_embed_names_the_whole_board(url: str, region: str | None) -> None:
    hit = discover.classify(url, [])
    assert hit is not None
    assert (hit.ats_type, hit.ats_slug, hit.ats_region) == ("lever", "samplelogistics", region)
    assert discover.posting(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.lever.co/v0/postings",
        "https://api.lever.co/v1/postings/samplelogistics",
        "https://api.lever.co/v0/postings/samplelogistics/abc-123",
        "https://api.lever.co/v0/postings/sample.logistics",
    ],
)
def test_other_lever_api_paths_are_not_boards(url: str) -> None:
    assert discover.classify(url, []) is None


def test_an_ashby_embed_names_the_whole_board() -> None:
    url = "https://jobs.ashbyhq.com/example.labs/embed?version=2"
    hit = discover.classify(url, [])
    assert hit is not None
    assert (hit.ats_type, hit.ats_slug) == ("ashby", "example.labs")
    assert hit.url == "https://jobs.ashbyhq.com/example.labs"
    assert discover.posting(url) is None
    assert discover.classify("https://jobs.ashbyhq.com/bad slug/embed", []) is None


def test_one_lever_query_covers_both_regions() -> None:
    queries = discover.build_queries({"search": {"target_titles": ["Analyst"]}})
    assert "site:lever.co Analyst" in queries
    assert not any("jobs.lever.co" in query for query in queries)


# --- a Lever board's region in ats ---------------------------------------------------


def test_an_eu_lever_board_reads_from_the_eu_api_and_keeps_its_key() -> None:
    eu = Board("lever", "samplelogistics", region="eu")
    assert ats.list_url(eu) == "https://api.eu.lever.co/v0/postings/samplelogistics?mode=json"
    assert eu.key == Board("lever", "samplelogistics").key == "lever:samplelogistics"
    jobs = [{"id": "abc-123", "text": "Analyst", "categories": {"location": "Example City"}}]
    client = ats.Client(Recorder(jobs))
    (job,) = ats.read(eu, client).jobs
    assert isinstance(job, ats.Job)
    assert job.url == "https://jobs.eu.lever.co/samplelogistics/abc-123"
    assert job.key == "lever:samplelogistics:abc-123"


def test_an_eu_lever_posting_and_confirm_use_the_eu_api() -> None:
    eu = Board("lever", "samplelogistics", region="eu")
    posting = Recorder({"id": "abc-123", "text": "Analyst"})
    ats.read_posting(PostingRef(eu, "abc-123"), ats.Client(posting))
    assert posting.calls == [
        "https://api.eu.lever.co/v0/postings/samplelogistics/abc-123?mode=json"
    ]
    confirm = Recorder([])
    assert ats.confirm_board(eu, ats.Client(confirm)) == Confirmed(True)
    assert confirm.calls == [
        "https://api.eu.lever.co/v0/postings/samplelogistics?mode=json&limit=1"
    ]


@pytest.mark.parametrize(
    "board",
    [
        Board("lever", "samplelogistics", region="us"),
        Board("greenhouse", "examplerobotics", region="eu"),
        Board("ashby", "example.labs", region="eu"),
    ],
)
def test_a_region_only_lever_knows_is_not_usable(board: Board) -> None:
    assert ats.list_url(board) is None
    assert ats.posting_url(PostingRef(board, "1")) is None


def test_an_eu_lever_link_fetches_its_jd_from_the_eu_api() -> None:
    assert snapshots.ats_json_url("https://jobs.eu.lever.co/samplelogistics/abc-123") == (
        "https://api.eu.lever.co/v0/postings/samplelogistics/abc-123"
    )


# --- the region from discovery to the company ------------------------------------------


def test_discovery_stores_and_approves_an_eu_lever_board(conn: sqlite3.Connection) -> None:
    hit = discover.classify("https://jobs.eu.lever.co/samplelogistics/abc-123", [])
    assert hit is not None
    (queued,) = discover._store(conn, {}, [hit], "2026-11-01", [])
    stored = conn.execute("SELECT ats_type, ats_region FROM discovery_candidates").fetchone()
    assert tuple(stored) == ("lever", "eu")
    discover.approve(conn, queued.id)
    company = conn.execute(
        "SELECT ats_type, ats_slug, ats_region, watchlist FROM companies"
    ).fetchone()
    assert tuple(company) == ("lever", "samplelogistics", "eu", 1)


def test_discovery_confirms_an_eu_lever_board_with_its_region(conn: sqlite3.Connection) -> None:
    boards: list[Board] = []

    def confirm(board: Board) -> Confirmed:
        boards.append(board)
        return Confirmed(True)

    body = json.dumps({"results": [{"url": "https://jobs.eu.lever.co/samplelogistics"}]})
    discover.run(
        conn,
        {
            "discovery": {"searxng_url": "http://127.0.0.1:8888/search", "result_pages": 1},
            "search": {"target_titles": ["Analyst"]},
        },
        today="2026-11-02",
        transport=lambda url: (200, body.encode()),
        confirm=confirm,
        sleep=lambda seconds: None,
    )
    assert boards == [Board("lever", "samplelogistics", region="eu")]


def test_an_approved_eu_board_is_watched_in_its_region(conn: sqlite3.Connection) -> None:
    hit = discover.classify("https://jobs.eu.lever.co/samplelogistics", [])
    assert hit is not None
    (queued,) = discover._store(conn, {}, [hit], "2026-11-01", [])
    discover.approve(conn, queued.id)
    items = watch._with_discovery_boards(conn, [])
    assert items == [
        {
            "name": "samplelogistics",
            "ats_type": "lever",
            "ats_slug": "samplelogistics",
            "ats_region": "eu",
        }
    ]
    entry = watch._entry(items[0])
    assert entry is not None
    board = entry.board()
    assert board is not None
    assert ats.list_url(board) == "https://api.eu.lever.co/v0/postings/samplelogistics?mode=json"


def test_the_watcher_fills_a_region_and_refuses_one_that_disagrees(
    conn: sqlite3.Connection,
) -> None:
    eu = Board("lever", "samplelogistics", region="eu")
    with db.transaction(conn):
        assert watch._align(conn, "Sample Logistics", Board("lever", "samplelogistics")).ok
    with db.transaction(conn):
        assert watch._align(conn, "Sample Logistics", eu).ok
    region = conn.execute("SELECT ats_region FROM companies").fetchone()[0]
    assert region == "eu"
    with db.transaction(conn):
        refused = watch._align(conn, "Sample Logistics", Board("lever", "samplelogistics"))
    assert not refused.ok
    assert refused.stored_region == "eu"


# --- config ------------------------------------------------------------------------------


def _entry_problems(entry: dict[str, Any]) -> list[str]:
    return [p for p in validate_config({"watchlist": [entry]}) if "watchlist[0]" in p]


def test_config_accepts_ats_region_eu_on_lever_entries_only() -> None:
    lever = {"name": "Sample Logistics", "ats_type": "lever", "ats_slug": "samplelogistics"}
    assert _entry_problems({**lever, "ats_region": "eu"}) == []
    assert any("must be one of ['eu']" in p for p in _entry_problems({**lever, "ats_region": "us"}))
    greenhouse = {"name": "Example", "ats_type": "greenhouse", "ats_slug": "examplerobotics"}
    assert any(
        "ats_region is for ats_type lever only" in p
        for p in _entry_problems({**greenhouse, "ats_region": "eu"})
    )


# --- further result pages -----------------------------------------------------------------


class Pages:
    """SearXNG by page: `pages[n]` answers page n of every query; past the list, an
    empty result. Records each request's page."""

    def __init__(self, *pages: object) -> None:
        self.pages = pages
        self.asked: list[int] = []

    def __call__(self, url: str) -> tuple[int, bytes]:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        page = int(query.get("pageno", ["1"])[0])
        self.asked.append(page)
        found = self.pages[page - 1] if page <= len(self.pages) else []
        if isinstance(found, int):
            return found, b""
        urls = found if isinstance(found, list) else []
        return 200, json.dumps({"results": [{"url": u} for u in urls]}).encode()


def _paged(conn: sqlite3.Connection, talk: Pages, pages: int = 2) -> discover.Outcome:
    pauses: list[float] = []
    outcome = discover.run(
        conn,
        {
            "discovery": {
                "searxng_url": "http://127.0.0.1:8888/search",
                "queries_per_run": 1,
                "query_delay_seconds": 4,
                "result_pages": pages,
            },
            "search": {"target_titles": ["Analyst"]},
        },
        today="2026-11-02",
        transport=talk,
        confirm=lambda board: Confirmed(True),
        sleep=pauses.append,
    )
    talk.pauses = pauses  # type: ignore[attr-defined]
    return outcome


A = "https://job-boards.greenhouse.io/examplerobotics/jobs/1"
B = "https://jobs.lever.co/samplelogistics/abc-123"
C = "https://jobs.ashbyhq.com/example.labs/xyz"


def test_page_one_sends_no_page_number() -> None:
    url = discover.request_url("http://127.0.0.1:8888/search", "site:lever.co Analyst")
    assert "pageno" not in url
    second = discover.request_url("http://127.0.0.1:8888/search", "site:lever.co Analyst", 2)
    assert second.endswith("&pageno=2")


def test_a_second_page_adds_its_new_results(conn: sqlite3.Connection) -> None:
    talk = Pages([A], [A, B])
    outcome = _paged(conn, talk)
    assert talk.asked == [1, 2]
    assert talk.pauses == [4]  # type: ignore[attr-defined]
    slugs = {row[0] for row in conn.execute("SELECT ats_slug FROM discovery_candidates")}
    assert slugs == {"examplerobotics", "samplelogistics"}
    assert [p.board.slug for p in outcome.postings] == ["examplerobotics", "samplelogistics"]
    assert not any("paging" in note for note in outcome.notes)


def test_three_pages_are_read_when_each_has_something_new(conn: sqlite3.Connection) -> None:
    talk = Pages([A], [B], [C])
    _paged(conn, talk, pages=3)
    assert talk.asked == [1, 2, 3]


def test_a_page_with_nothing_new_stops_paging_and_says_so(conn: sqlite3.Connection) -> None:
    talk = Pages([A, B], [B, A], [C])
    outcome = _paged(conn, talk, pages=3)
    assert talk.asked == [1, 2]
    assert "discovery: query 1 page 2 had no new results; stopped paging it" in outcome.notes
    assert not any("Analyst" in note for note in outcome.notes)  # never the query itself


def test_an_empty_page_stops_paging(conn: sqlite3.Connection) -> None:
    talk = Pages([A], [], [C])
    outcome = _paged(conn, talk, pages=3)
    assert talk.asked == [1, 2]
    assert any("page 2 had no new results" in note for note in outcome.notes)


def test_a_failed_page_stops_paging_but_keeps_the_pass(conn: sqlite3.Connection) -> None:
    talk = Pages([A], 500, [C])
    outcome = _paged(conn, talk, pages=3)
    assert talk.asked == [1, 2]
    assert outcome.notice is None
    assert "discovery: query 1 page 2: discovery miss: bad-reply; stopped paging it" in (
        outcome.notes
    )
    assert conn.execute("SELECT failures FROM discovery_state").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()[0] == 1


def test_rate_limiting_on_a_later_page_stops_the_pass(conn: sqlite3.Connection) -> None:
    talk = Pages([A], 429)
    outcome = _paged(conn, talk)
    assert outcome.notice == discover.THROTTLED
    assert conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT failures FROM discovery_state").fetchone()[0] == 1


def test_one_result_page_reads_only_the_first(conn: sqlite3.Connection) -> None:
    talk = Pages([A], [B])
    _paged(conn, talk, pages=1)
    assert talk.asked == [1]
