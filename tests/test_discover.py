"""Discovery queues companies from result URLs. Snippets stay out of the database."""

from __future__ import annotations

import datetime as dt
import http.client
import inspect
import json
import sqlite3
import urllib.parse
from pathlib import Path

import pytest
import yaml

from careerhunter import ats, cli, db, discover, loopback, search_store
from careerhunter.platform_checks import SystemProbe
from tests.fakes import FakeClock, FakeServer, ManualWatchdog, Reply, use_example_watchlist
from tests.test_score import BROKEN, broken_connection

SNIPPET = "DO-NOT-LEAK-SNIPPET"
TITLE = "DO-NOT-LEAK-TITLE"
QUERY = "DO-NOT-LEAK-QUERY"
PATH = "/Volumes/ExampleDrive/ExampleFolder"
EXCLUSION = "DO-NOT-LEAK-EXCLUSION"
URLS = [
    "https://job-boards.greenhouse.io/examplerobotics/jobs/99",
    "https://boards.greenhouse.io/embed/job_board?for=examplerobotics",
    "https://job-boards.greenhouse.io/newrobotics/jobs/42",
    "https://boards.greenhouse.io/newrobotics",
    "https://jobs.lever.co/samplelogistics/abc",
    "https://jobs.lever.co/freshlogistics/post-1",
    "https://jobs.ashbyhq.com/exampleorg/role-9",
    "https://jobs.example.com/careers?utm=1#top",
    "https://evil.example/jobs/1",
    "http://jobs.example.com/careers",
]


def _body(urls: list[str]) -> bytes:
    results = [
        {"url": url, "title": TITLE, "content": f"{SNIPPET} {PATH}", "engine": "example"}
        for url in urls
    ]
    return json.dumps({"results": results, "query": QUERY}).encode()


class _Talk:
    def __init__(self, body: bytes, status: int = 200, fail_at: int | None = None) -> None:
        self.body = body
        self.status = status
        self.fail_at = fail_at
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        if self.fail_at is not None and len(self.calls) >= self.fail_at:
            if self.status == 0:
                raise OSError("down")
            return self.status, b""
        return 200, self.body


def _enable(root: Path, *, hosts: list[str] | None = None) -> None:
    path = root / "config.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["discovery"]["searxng_url"] = "http://127.0.0.1:8888/search"
    if hosts is not None:
        doc["discovery"]["career_hosts"] = hosts
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    exclusions = root / "exclusions.yaml"
    loaded = yaml.safe_load(exclusions.read_text(encoding="utf-8"))
    loaded["companies"].append(EXCLUSION)
    exclusions.write_text(yaml.safe_dump(loaded), encoding="utf-8")


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    use_example_watchlist(data_env / "config.yaml")
    capsys.readouterr()
    return data_env


HOSTS = (
    "greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "jobs.smartrecruiters.com",
    "bamboohr.com",
    "myworkdayjobs.com",
)


def test_queries_walk_every_title_variant_place_and_host_in_order() -> None:
    config = {
        "search": {
            "lanes": [
                {"name": "B", "priority": 2, "titles": ["Three"]},
                {"name": "A", "priority": 1, "titles": ["One", ["Alpha", "Beta"], "one"]},
            ],
            "location_rules": {"onsite_hybrid": ["Example City"], "remote": ["Example Region"]},
            "locations": ["Unused Town"],
        },
        "discovery": {"career_hosts": ["jobs.example.com"]},
    }
    queries = discover.build_queries(config)
    hosts = [*HOSTS, "jobs.example.com"]
    expected = [
        f"site:{host} {title} {place}"
        for title in ("One", "Alpha", "Beta", "Three")  # lane priority, every variant, once
        for place in ("Example City", "Example Region")
        for host in hosts
    ]
    assert queries == expected
    assert not any("Unused Town" in query for query in queries)
    assert discover.build_queries({"search": {}}) == []
    bare = discover.build_queries({"search": {"target_titles": ["One"]}})
    assert bare == [f"site:{host} One" for host in HOSTS]


def test_the_cursor_takes_the_next_slice_and_wraps() -> None:
    queries = [f"q{n}" for n in range(5)]
    assert discover.take(queries, 0, 2) == (["q0", "q1"], 2)
    assert discover.take(queries, 4, 2) == (["q4", "q0"], 1)
    assert discover.take(queries, 3, 12) == (["q3", "q4", "q0", "q1", "q2"], 3)  # once each
    assert discover.take(queries, 17, 1) == (["q2"], 3)  # the list got shorter: wrap first
    assert discover.take([], 3, 2) == ([], 0)


def test_the_cursor_covers_every_query_across_runs_and_wraps(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    config = {
        "discovery": {"searxng_url": "http://127.0.0.1:8888/search", "queries_per_run": 4},
        "search": {
            "target_titles": ["One", "Two"],
            "location_rules": {"onsite_hybrid": ["Example City"]},
        },
    }
    every = discover.build_queries(config)
    assert len(every) == 12
    asked: list[str] = []
    for _run in range(4):
        talk = _Talk(json.dumps({"results": []}).encode())
        assert discover.run(conn, config, today="2026-11-02", transport=talk).notice is None
        asked += [urllib.parse.parse_qs(urllib.parse.urlsplit(c).query)["q"][0] for c in talk.calls]
    assert asked[:12] == every  # three runs cover every combination once
    assert asked[12:] == every[:4]  # then it wraps
    conn.close()


def test_a_miss_leaves_the_cursor_where_it_was(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    config = {**_SEARCH, "discovery": {**_SEARCH["discovery"], "queries_per_run": 2}}
    ok = _Talk(json.dumps({"results": []}).encode())
    discover.run(conn, config, today="2026-11-02", transport=ok)
    discover.run(conn, config, today="2026-11-02", transport=_Talk(b"", status=0, fail_at=2))
    assert conn.execute("SELECT cursor FROM discovery_state").fetchone()[0] == 2
    _clear_backoff(conn)
    again = _Talk(json.dumps({"results": []}).encode())
    discover.run(conn, config, today="2026-11-02", transport=again)
    first = [c for c in ok.calls]
    assert again.calls[0] != first[0]  # it moved on after the first run
    assert conn.execute("SELECT cursor FROM discovery_state").fetchone()[0] == 4
    conn.close()


def test_no_titles_makes_no_request(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")

    def refuse(url: str) -> tuple[int, bytes]:
        raise AssertionError(url)

    outcome = discover.run(
        conn,
        {
            "discovery": {"searxng_url": "http://127.0.0.1:8888/search"},
            "search": {"locations": ["Example City"]},
        },
        today="2026-11-02",
        transport=refuse,
    )
    conn.close()
    assert outcome == discover.Outcome(None, ())


def test_the_search_url_stays_on_loopback() -> None:
    url = discover.request_url("http://127.0.0.1:8888/search", "site:jobs.lever.co One")
    parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert parsed["format"] == ["json"]
    assert parsed["q"] == ["site:jobs.lever.co One"]
    assert discover.request_url("http://127.0.0.1:8888", "One").startswith(
        "http://127.0.0.1:8888/search?"
    )
    for base in (
        "https://127.0.0.1/search",
        "http://example.com/search",
        "http://user@127.0.0.1/search",
        "http://10.0.0.1/search",
    ):
        with pytest.raises(discover.DiscoverError):
            discover.request_url(base, "One")


def test_urls_reduce_to_a_board_or_an_allowlisted_career_page() -> None:
    hosts = ["jobs.example.com"]
    board = discover.classify("https://job-boards.greenhouse.io/newrobotics/jobs/42", hosts)
    embed = discover.classify("https://boards.greenhouse.io/embed/job_board?for=newrobotics", hosts)
    assert board == embed
    assert board is not None
    assert board.ats_type == "greenhouse"
    assert board.ats_slug == "newrobotics"
    assert board.url == "https://boards.greenhouse.io/newrobotics"
    lever = discover.classify("https://jobs.lever.co/freshlogistics/post-1", hosts)
    ashby = discover.classify("https://jobs.ashbyhq.com/exampleorg/role-9", hosts)
    assert lever is not None and lever.ats_type == "lever" and lever.ats_slug == "freshlogistics"
    assert ashby is not None and ashby.ats_type == "ashby"
    career = discover.classify("https://jobs.example.com/careers?utm=1#top", hosts)
    assert career is not None
    assert career.kind == "career"
    assert career.url == "https://jobs.example.com/careers"
    assert career.suggested_name == "jobs.example.com"
    assert discover.classify("https://evil.example/jobs/1", hosts) is None
    assert discover.classify("http://jobs.example.com/careers", hosts) is None


def test_a_miss_writes_nothing(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    config = {
        "discovery": {"searxng_url": "http://127.0.0.1:8888/search", "career_hosts": []},
        "search": {"target_titles": ["One"], "locations": ["Example City"]},
    }
    down = _Talk(b"", status=0, fail_at=1)
    outcome = discover.run(conn, config, today="2026-11-02", transport=down)
    assert outcome.notice == discover.MISS
    assert outcome.rows == ()
    _clear_backoff(conn)
    bad = _Talk(b"<html>", status=200)
    outcome = discover.run(conn, config, today="2026-11-02", transport=bad)
    assert outcome.notice == discover.BAD_REPLY  # it answered, with no result list (#42)
    _clear_backoff(conn)
    partial = _Talk(_body(["https://jobs.lever.co/freshlogistics/post-1"]), fail_at=3, status=500)
    outcome = discover.run(conn, config, today="2026-11-02", transport=partial)
    assert outcome.notice == discover.BAD_REPLY  # HTTP 500 is an answer too
    assert partial.calls
    _clear_backoff(conn)
    huge = _Talk(b"x" * (discover.MAX_BODY + 1))
    outcome = discover.run(conn, config, today="2026-11-02", transport=huge)
    assert outcome.notice == discover.MISS
    off = discover.run(conn, {"discovery": {"searxng_url": None}}, today="2026-11-02")
    assert off.notice == discover.OFF
    assert off.rows == ()
    count = conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()
    conn.close()
    assert count is not None
    assert count[0] == 0


def test_the_command_queues_reduced_urls_and_drops_the_snippet(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(inited, hosts=["jobs.example.com"])
    talk = _Talk(_body(URLS))
    monkeypatch.setattr(discover, "default_transport", talk)
    assert cli.main(["discover"], probe=probe) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "1 board newrobotics greenhouse newrobotics",
        "2 board freshlogistics lever freshlogistics",
        "3 board exampleorg ashby exampleorg",
    ]  # a career page is not queued (#41); its host is only a site: host
    assert len(talk.calls) == 12
    assert all("format=json" in call for call in talk.calls)
    asked = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(call).query)["q"][0] for call in talk.calls
    ]
    assert any(query.startswith("site:jobs.example.com ") for query in asked)
    blob = captured.out + captured.err + "\n".join(talk.calls) + "\n".join(asked)
    for secret in (SNIPPET, TITLE, QUERY, PATH, EXCLUSION):
        assert secret not in blob
    conn = db.connect(inited / "search.db")
    stored = [
        " ".join("" if value is None else str(value) for value in row)
        for row in conn.execute("SELECT * FROM discovery_candidates ORDER BY id")
    ]
    companies = conn.execute("SELECT count(*) FROM companies").fetchone()
    postings = conn.execute("SELECT count(*) FROM postings").fetchone()
    conn.close()
    text = "\n".join(stored)
    for secret in (SNIPPET, TITLE, QUERY, PATH, EXCLUSION, "utm", "jobs/42"):
        assert secret not in text
    assert "https://boards.greenhouse.io/newrobotics" in text
    assert companies is not None and companies[0] == 0
    assert postings is not None and postings[0] == 0
    assert list(inited.glob("ei_record_*.db")) == []
    assert not (inited / "logs" / "watch.success").exists()
    later = dt.datetime(2026, 11, 2, 10, tzinfo=dt.UTC)  # past the last pass's claim
    monkeypatch.setattr(discover, "_utc_now", lambda: later)
    assert cli.main(["discover"], probe=probe) == 0
    again = capsys.readouterr()
    assert again.out == ""
    assert again.err == ""
    assert cli.main(["discover", "list"], probe=probe) == 0
    listed = capsys.readouterr()
    assert listed.out == captured.out


def test_a_null_url_makes_no_request(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(url: str) -> tuple[int, bytes]:
        raise AssertionError(url)

    monkeypatch.setattr(discover, "default_transport", refuse)
    assert cli.main(["discover"], probe=probe) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "discovery off\n"
    assert not (inited / "search.db").exists() or _count(inited) == 0
    stamp = inited / "logs" / "discover.success"
    assert stamp.is_file()
    assert stamp.stat().st_mode & 0o777 == 0o600
    log = inited / "logs" / "discover.log"
    assert log.read_text() == "discovery off\ndiscover: 0 queued\n"
    assert log.stat().st_mode & 0o777 == 0o600


def test_a_missing_config_writes_nothing(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (inited / "config.yaml").unlink()
    assert cli.main(["discover"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config.yaml is missing" in captured.err
    assert not (inited / "logs" / "discover.success").exists()


def _count(root: Path) -> int:
    conn = db.connect(root / "search.db")
    row = conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()
    conn.close()
    assert row is not None
    return int(row[0])


def test_a_missing_database_writes_nothing(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (inited / "search.db").unlink()
    assert cli.main(["discover"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: search.db is missing; run careerhunter init\n"
    assert not (inited / "logs" / "discover.success").exists()


def test_a_miss_on_the_command_exits_zero(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(inited, hosts=["jobs.example.com"])

    def down(url: str) -> tuple[int, bytes]:
        raise OSError(url)

    monkeypatch.setattr(discover, "default_transport", down)
    assert cli.main(["discover"], probe=probe) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{discover.MISS}\n"
    assert "site:" not in captured.err
    assert _count(inited) == 0
    stamp = inited / "logs" / "discover.success"
    assert stamp.is_file()
    assert stamp.stat().st_mode & 0o777 == 0o600
    log = inited / "logs" / "discover.log"
    assert log.read_text() == f"{discover.MISS}\ndiscover: 0 queued\n"
    assert "site:" not in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


def test_list_does_not_refresh_discover_success(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["discover"], probe=probe) == 0
    capsys.readouterr()
    path = inited / "logs" / "discover.success"
    path.write_text("2020-01-01T00:00:00Z\n")
    assert cli.main(["discover", "list"], probe=probe) == 0
    assert path.read_text() == "2020-01-01T00:00:00Z\n"
    assert cli.main(["discover"], probe=probe) == 0
    refreshed = path.read_text()
    assert refreshed != "2020-01-01T00:00:00Z\n"
    assert refreshed.strip().endswith("Z")
    # Only the two search runs wrote to the log; list wrote nothing.
    log = (inited / "logs" / "discover.log").read_text()
    assert log == "discovery off\ndiscover: 0 queued\n" * 2


def test_the_client_returns_a_redirect_and_does_not_follow_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(Reply(302, b"go away"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    status, body = discover.default_transport("http://127.0.0.1:8888/search?q=One&format=json")
    assert status == 302
    assert body == b"go away"
    assert [(host, address, port) for host, address, port, _t in server.opened] == [
        ("127.0.0.1", "127.0.0.1", 8888)
    ]
    assert server.requests == [
        ("GET", "/search?q=One&format=json", None, {"Accept": "application/json"})
    ]
    assert server.closes == 1
    with pytest.raises(discover.DiscoverError):
        discover.default_transport("http://example.com/search?q=One")


def _insert_candidate(
    conn: sqlite3.Connection,
    *,
    kind: str = "board",
    url: str = "https://boards.greenhouse.io/newrobotics",
    ats_type: str | None = "greenhouse",
    ats_slug: str | None = "newrobotics",
    name: str = "newrobotics",
    status: str = "pending",
) -> int:
    cur = conn.execute(
        "INSERT INTO discovery_candidates "
        "(kind, url, ats_type, ats_slug, suggested_name, status, first_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-11-02')",
        (kind, url, ats_type, ats_slug, name, status),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _company(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT name, ats_type, ats_slug, careers_url, watchlist, source FROM companies "
        "WHERE name = ?",
        (name,),
    ).fetchone()
    assert row is not None
    return row


def test_approve_fills_a_board_and_stores_a_career_page_unfetched(
    search: sqlite3.Connection,
) -> None:
    board = _insert_candidate(search)
    career = _insert_candidate(
        search,
        kind="career",
        url="https://jobs.example.com/careers",
        ats_type=None,
        ats_slug=None,
        name="jobs.example.com",
    )
    assert discover.approve(search, board).suggested_name == "newrobotics"
    assert discover.approve(search, career).kind == "career"
    queued = _company(search, "newrobotics")
    assert queued["ats_type"] == "greenhouse"
    assert queued["ats_slug"] == "newrobotics"
    assert queued["watchlist"] == 1
    assert queued["source"] == "discovery"
    page = _company(search, "jobs.example.com")
    assert page["careers_url"] == "https://jobs.example.com/careers"
    assert page["watchlist"] == 0
    assert page["source"] == "discovery"
    assert page["ats_type"] is None
    with pytest.raises(discover.DiscoverError, match="is approved"):
        discover.approve(search, board)


def test_a_career_page_fills_an_empty_url_and_refuses_a_different_one(
    search: sqlite3.Connection,
) -> None:
    search_store.upsert_company(search, "jobs.example.com", source="manual", watchlist=1)
    fill = _insert_candidate(
        search,
        kind="career",
        url="https://jobs.example.com/careers",
        ats_type=None,
        ats_slug=None,
        name="jobs.example.com",
    )
    discover.approve(search, fill)
    filled = _company(search, "jobs.example.com")
    assert filled["careers_url"] == "https://jobs.example.com/careers"
    assert filled["watchlist"] == 1
    assert filled["source"] == "manual"
    search_store.upsert_company(
        search,
        "jobs.example.org",
        careers_url="https://jobs.example.org/jobs",
        source="manual",
    )
    refused = _insert_candidate(
        search,
        kind="career",
        url="https://jobs.example.org/careers",
        ats_type=None,
        ats_slug=None,
        name="jobs.example.org",
    )
    with pytest.raises(discover.DiscoverError, match="disagrees with the stored company"):
        discover.approve(search, refused)
    kept = _company(search, "jobs.example.org")
    assert kept["careers_url"] == "https://jobs.example.org/jobs"
    assert kept["source"] == "manual"
    status = search.execute(
        "SELECT status FROM discovery_candidates WHERE id = ?", (refused,)
    ).fetchone()
    assert status is not None
    assert status["status"] == "pending"


def test_approve_keeps_an_existing_source_and_refuses_a_disagreement(
    search: sqlite3.Connection,
) -> None:
    search_store.upsert_company(search, "freshlogistics", source="manual")
    kept = _insert_candidate(
        search,
        url="https://jobs.lever.co/freshlogistics",
        ats_type="lever",
        ats_slug="freshlogistics",
        name="freshlogistics",
    )
    discover.approve(search, kept)
    row = _company(search, "freshlogistics")
    assert row["source"] == "manual"
    assert row["watchlist"] == 1
    assert row["ats_type"] == "lever"
    assert row["ats_slug"] == "freshlogistics"
    search_store.upsert_company(
        search,
        "exampleorg",
        ats_type="greenhouse",
        ats_slug="exampleorg",
        source="watcher",
        watchlist=1,
    )
    refused = _insert_candidate(
        search,
        url="https://jobs.ashbyhq.com/exampleorg",
        ats_type="ashby",
        ats_slug="exampleorg",
        name="exampleorg",
    )
    with pytest.raises(discover.DiscoverError, match="disagrees with the stored company"):
        discover.approve(search, refused)
    unchanged = _company(search, "exampleorg")
    assert unchanged["source"] == "watcher"
    assert unchanged["ats_type"] == "greenhouse"
    assert unchanged["ats_slug"] == "exampleorg"
    status = search.execute(
        "SELECT status FROM discovery_candidates WHERE id = ?", (refused,)
    ).fetchone()
    assert status is not None
    assert status["status"] == "pending"


def test_reject_inserts_no_company(search: sqlite3.Connection) -> None:
    candidate = _insert_candidate(
        search,
        url="https://boards.greenhouse.io/sampletown",
        ats_slug="sampletown",
        name="sampletown",
    )
    assert discover.reject(search, candidate).id == candidate
    assert search.execute("SELECT count(*) FROM companies").fetchone()[0] == 0
    status = search.execute(
        "SELECT status FROM discovery_candidates WHERE id = ?", (candidate,)
    ).fetchone()
    assert status is not None
    assert status["status"] == "rejected"
    with pytest.raises(discover.DiscoverError, match="is rejected"):
        discover.approve(search, candidate)
    with pytest.raises(discover.DiscoverError, match="was not found"):
        discover.reject(search, 99)


def test_approve_and_reject_leave_config_and_the_ei_file_alone(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ei = inited / "ei_record_2026-27.db"
    ei.write_bytes(b"synthetic-ei\n")
    config = (inited / "config.yaml").read_bytes()
    exclusions = (inited / "exclusions.yaml").read_bytes()
    conn = db.connect(inited / "search.db")
    board = _insert_candidate(conn)
    refused_name = "samplelogistics"
    search_store.upsert_company(
        conn, refused_name, ats_type="greenhouse", ats_slug="samplelogistics", source="manual"
    )
    refused = _insert_candidate(
        conn,
        url="https://jobs.lever.co/samplelogistics",
        ats_type="lever",
        ats_slug="samplelogistics",
        name=refused_name,
    )
    dropped = _insert_candidate(
        conn,
        url="https://jobs.ashbyhq.com/exampleorg",
        ats_type="ashby",
        ats_slug="exampleorg",
        name="exampleorg",
    )
    conn.close()
    assert cli.main(["discover", "approve", str(board)], probe=probe) == 0
    approved = capsys.readouterr()
    assert approved.out == "1 board newrobotics greenhouse newrobotics\n"
    assert approved.err == ""
    assert cli.main(["discover", "approve", str(refused)], probe=probe) == cli.EXIT_ERROR
    disagree = capsys.readouterr()
    assert disagree.out == ""
    assert disagree.err == (
        f"error: discovery candidate {refused} disagrees with the stored company\n"
    )
    assert cli.main(["discover", "reject", str(dropped)], probe=probe) == 0
    assert capsys.readouterr().out == "3 board exampleorg ashby exampleorg\n"
    assert cli.main(["discover", "approve"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: discover approve needs a candidate id\n"
    assert cli.main(["discover", "list", "1"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: discover list takes no candidate id\n"
    assert ei.read_bytes() == b"synthetic-ei\n"
    assert (inited / "config.yaml").read_bytes() == config
    assert (inited / "exclusions.yaml").read_bytes() == exclusions
    assert list(inited.glob("ei_record_*")) == [ei]
    assert not (inited / "logs" / "discover.success").exists()
    assert not (inited / "logs" / "discover.log").exists()
    conn = db.connect(inited / "search.db")
    companies = conn.execute("SELECT count(*) FROM companies").fetchone()
    applications = conn.execute("SELECT count(*) FROM applications").fetchone()
    pending = conn.execute(
        "SELECT count(*) FROM discovery_candidates WHERE status = 'pending'"
    ).fetchone()
    conn.close()
    assert companies is not None and companies[0] == 2
    assert applications is not None and applications[0] == 0
    assert pending is not None and pending[0] == 1


def test_the_alert_query_takes_watcher_and_discovery_postings_only(
    search: sqlite3.Connection,
) -> None:
    """Discovery postings alert directly (#41); a posting logged by hand never does."""
    text = inspect.getsource(search_store.pending_alerts)
    assert "p.source IN ('watcher', 'discovery')" in text
    watcher, _inserted = search_store.upsert_company(search, "Example Robotics", source="watcher")
    other, _also = search_store.upsert_company(search, "Example Org", source="discovery")
    search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, llm_output, fit) "
        "VALUES (?, 'Operations Coordinator', '2026-11-02', 'watcher', 'synthetic', 'high')",
        (watcher,),
    )
    watcher_id = int(search.execute("SELECT last_insert_rowid()").fetchone()[0])
    search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, llm_output, fit) "
        "VALUES (?, 'Customer Success Specialist', '2026-11-02', 'discovery', 'synthetic', "
        "'high')",
        (other,),
    )
    found_id = int(search.execute("SELECT last_insert_rowid()").fetchone()[0])
    search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, llm_output, fit) "
        "VALUES (?, 'Logged By Hand', '2026-11-02', 'manual', 'synthetic', 'high')",
        (other,),
    )
    assert search_store.pending_alert_ids(search) == [watcher_id, found_id]


def test_the_queue_has_no_snippet_column(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(discovery_candidates)")}
    assert {"title", "content", "snippet", "query"} & columns == set()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO discovery_candidates "
            "(kind, url, ats_type, ats_slug, suggested_name, status, first_seen) "
            "VALUES ('board', 'https://boards.greenhouse.io/x', 'greenhouse', NULL, "
            "'x', 'pending', '2026-11-02')"
        )
    conn.close()


def _search_db(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    return conn


_SEARCH = {
    "discovery": {"searxng_url": "http://127.0.0.1:8888/search", "career_hosts": []},
    "search": {"target_titles": ["One"], "locations": ["Example City"]},
}
_GOOD = "https://job-boards.greenhouse.io/newrobotics/jobs/42"


def _exists(board: object) -> ats.Confirmed:
    return ats.Confirmed(True)


def _clear_backoff(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE discovery_state SET failures = 0, backoff_until = NULL")


def _rows(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("error", BROKEN, ids=lambda e: type(e).__name__)
def test_a_broken_reply_from_the_client_is_a_miss(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(loopback, "LoopbackConnection", broken_connection(error))
    with pytest.raises(discover.DiscoverError) as exc:
        discover.default_transport("http://127.0.0.1:8888/search?q=One&format=json")
    assert str(exc.value) == discover.MISS


def test_a_transport_that_breaks_is_a_miss_that_stores_nothing(tmp_path: Path) -> None:
    def broken(url: str) -> tuple[int, bytes]:
        raise http.client.IncompleteRead(b"")

    conn = _search_db(tmp_path)
    outcome = discover.run(conn, _SEARCH, today="2026-11-02", transport=broken)
    assert outcome == discover.Outcome(discover.MISS, ())
    assert _rows(conn) == 0
    conn.close()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"\xff\xfe\x00{", id="not-utf8"),
        pytest.param(json.dumps({"error": "too many requests"}).encode(), id="error-object"),
        pytest.param(json.dumps({"results": "x"}).encode(), id="results-not-list"),
        pytest.param(json.dumps({"results": ["x"]}).encode(), id="result-not-object"),
        pytest.param(json.dumps({"results": [{"title": "t"}]}).encode(), id="no-url"),
        pytest.param(json.dumps({"results": [{"url": 5}]}).encode(), id="url-not-text"),
        pytest.param(
            json.dumps({"results": [{"title": "t"}, {"url": 5}, "x"]}).encode(), id="all-bad"
        ),
    ],
)
def test_a_reply_of_the_wrong_shape_is_a_miss_that_stores_nothing(
    tmp_path: Path, body: bytes
) -> None:
    conn = _search_db(tmp_path)
    outcome = discover.run(conn, _SEARCH, today="2026-11-02", transport=_Talk(body))
    assert outcome == discover.Outcome(discover.BAD_REPLY, ())
    assert _rows(conn) == 0
    conn.close()


def test_a_result_without_a_url_is_skipped_and_the_good_one_kept(tmp_path: Path) -> None:
    body = json.dumps({"results": [{"url": _GOOD}, {"title": "t"}]}).encode()
    conn = _search_db(tmp_path)
    outcome = discover.run(
        conn, _SEARCH, today="2026-11-02", transport=_Talk(body), confirm=_exists
    )
    assert outcome.notice is None
    assert [row.url for row in outcome.rows] == ["https://boards.greenhouse.io/newrobotics"]
    assert _rows(conn) == 1
    conn.close()


def test_an_empty_result_list_is_a_normal_reply(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    talk = _Talk(json.dumps({"results": []}).encode())
    outcome = discover.run(conn, _SEARCH, today="2026-11-02", transport=talk)
    assert outcome == discover.Outcome(None, ())
    assert len(talk.calls) == 6  # every query was sent; none was a miss
    conn.close()


def test_a_bug_in_our_own_parser_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bug(body: bytes) -> list[str] | None:
        raise RuntimeError("synthetic bug")

    monkeypatch.setattr(discover, "_result_urls", bug)
    conn = _search_db(tmp_path)
    with pytest.raises(RuntimeError, match="synthetic bug"):
        discover.run(conn, _SEARCH, today="2026-11-02", transport=_Talk(_body([_GOOD])))
    conn.close()


def test_a_reply_that_is_not_utf8_does_not_stop_the_command(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(inited, hosts=["jobs.example.com"])
    monkeypatch.setattr(discover, "default_transport", lambda url: (200, b"\xff\xfe\x00{"))
    assert cli.main(["discover"], probe=probe) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{discover.BAD_REPLY}\n"
    assert _count(inited) == 0
    assert (inited / "logs" / "discover.success").is_file()


def test_discovery_has_its_own_30_second_deadline(
    monkeypatch: pytest.MonkeyPatch, loopback_watchdog: ManualWatchdog
) -> None:
    clock = FakeClock()
    server = FakeServer(Reply(body=_body([_GOOD]), clock=clock, header_cost=31.0))
    monkeypatch.setattr(loopback, "_clock", clock)
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    with pytest.raises(discover.DiscoverError) as exc:
        discover.default_transport("http://127.0.0.1:8888/search?q=One&format=json")
    assert str(exc.value) == discover.MISS
    assert [a.seconds for a in loopback_watchdog.armed] == [30.0]
    assert [timeout for *_rest, timeout in server.opened] == [20.0]


def test_a_localhost_url_is_resolved_and_must_stay_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(Reply(body=_body([_GOOD])))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    monkeypatch.setattr(loopback, "_resolver", lambda host, port, timeout: ["::1"])
    status, _body_bytes = discover.default_transport("http://localhost:8888/search?q=One")
    assert status == 200
    assert [(host, address) for host, address, _p, _t in server.opened] == [("localhost", "::1")]
    monkeypatch.setattr(loopback, "_resolver", lambda host, port, timeout: ["93.184.216.34"])
    with pytest.raises(discover.DiscoverError):
        discover.default_transport("http://localhost:8888/search?q=One")
    assert len(server.opened) == 1  # the refused answer was never dialled


def test_a_search_run_logs_how_many_rows_it_queued(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(inited, hosts=["jobs.example.com"])
    monkeypatch.setattr(discover, "default_transport", _Talk(_body(URLS)))
    assert cli.main(["discover"], probe=probe) == 0
    queued = capsys.readouterr().out.splitlines()
    log = (inited / "logs" / "discover.log").read_text()
    assert log == f"discover: {len(queued)} queued\n"
    assert SNIPPET not in log and TITLE not in log


@pytest.mark.parametrize(
    ("setup", "line"),
    [
        (lambda root, mp: (root / "config.yaml").unlink(), "config.yaml is missing"),
        (
            lambda root, mp: (root / "search.db").unlink(),
            "search.db is missing; run careerhunter init",
        ),
        (
            lambda root, mp: mp.setattr(
                cli.db,
                "migrate",
                lambda conn, kind: (_ for _ in ()).throw(db.MigrationError("synthetic")),
            ),
            "MigrationError: synthetic",
        ),
    ],
    ids=["no-config", "no-database", "migrate"],
)
def test_a_search_run_that_cannot_start_exits_1_with_its_line_in_the_log(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    setup: object,
    line: str,
) -> None:
    _enable(inited)
    monkeypatch.setattr(discover, "default_transport", lambda url: pytest.fail(url))
    setup(inited, monkeypatch)  # type: ignore[operator]
    assert cli.main(["discover"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == f"error: {line}\n"
    assert (inited / "logs" / "discover.log").read_text() == f"discover: aborted: {line}\n"
    assert not (inited / "logs" / "discover.success").exists()


def test_a_search_run_without_a_log_sends_no_query(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(inited)
    monkeypatch.setattr(discover, "default_transport", lambda url: pytest.fail(url))
    (inited / "logs").mkdir(mode=0o700, exist_ok=True)
    (inited / "logs" / "discover.log").mkdir()
    assert cli.main(["discover"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: run log not writable: IsADirectoryError\n"
    assert not (inited / "logs" / "discover.success").exists()


def test_a_search_run_that_cannot_open_the_database_exits_1(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_open(path: object) -> object:
        raise sqlite3.OperationalError("cannot open")

    _enable(inited)
    monkeypatch.setattr(cli.db, "connect", cannot_open)
    assert cli.main(["discover"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: OperationalError: cannot open\n"
    log = (inited / "logs" / "discover.log").read_text()
    assert log == "discover: aborted: OperationalError: cannot open\n"


def test_a_search_run_with_an_invalid_config_exits_1(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (inited / "config.yaml").write_text("discovery: 5\n")
    assert cli.main(["discover"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err.startswith("error: ")
    log = (inited / "logs" / "discover.log").read_text()
    assert log.startswith("discover: aborted: ConfigError: ")


def test_an_unexpected_search_failure_leaves_its_traceback_in_the_log(
    inited: Path, probe: SystemProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic discover failure")

    monkeypatch.setattr(cli.discover, "run", boom)
    with pytest.raises(RuntimeError, match="synthetic discover failure"):
        cli.main(["discover"], probe=probe)
    log = (inited / "logs" / "discover.log").read_text()
    assert log.startswith("discover: aborted: traceback\nTraceback (most recent call last):")
    assert "RuntimeError: synthetic discover failure" in log
    assert not (inited / "logs" / "discover.success").exists()


def test_a_search_run_whose_log_fails_mid_run_exits_1(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from careerhunter.runlog import RunLog

    def refuse(fd: int, data: memoryview) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "RunLog", lambda path: RunLog(path, writer=refuse))
    assert cli.main(["discover"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: run log not writable: OSError\n"
    assert not (inited / "logs" / "discover.success").exists()


@pytest.mark.parametrize(
    ("missing", "err"),
    [
        ("config.yaml", "error: config.yaml is missing\n"),
        ("search.db", "error: search.db is missing; run careerhunter init\n"),
    ],
)
def test_list_without_its_files_exits_1_and_writes_no_log(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    missing: str,
    err: str,
) -> None:
    (inited / missing).unlink()
    assert cli.main(["discover", "list"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == err
    assert not (inited / "logs" / "discover.log").exists()


@pytest.mark.parametrize(
    ("address", "ats", "slug", "board_url"),
    [
        (
            "https://jobs.smartrecruiters.com/ExampleRobotics/744000000000001-operations-lead",
            "smartrecruiters",
            "ExampleRobotics",
            "https://jobs.smartrecruiters.com/ExampleRobotics",
        ),
        (
            "https://careers.smartrecruiters.com/ExampleRobotics",
            "smartrecruiters",
            "ExampleRobotics",
            "https://jobs.smartrecruiters.com/ExampleRobotics",
        ),
        (
            "https://examplerobotics.bamboohr.com/careers/41",
            "bamboohr",
            "examplerobotics",
            "https://examplerobotics.bamboohr.com/careers",
        ),
        (
            "https://ExampleRobotics.bamboohr.com/careers",
            "bamboohr",
            "examplerobotics",
            "https://examplerobotics.bamboohr.com/careers",
        ),
    ],
)
def test_smartrecruiters_and_bamboohr_urls_reduce_to_a_board(
    address: str, ats: str, slug: str, board_url: str
) -> None:
    hit = discover.classify(address, [])
    assert hit is not None
    assert (hit.kind, hit.ats_type, hit.ats_slug, hit.url) == ("board", ats, slug, board_url)
    assert hit.ats_host is None and hit.ats_site is None


@pytest.mark.parametrize(
    "address",
    [
        "https://examplefreight.wd5.myworkdayjobs.com/External",
        "https://examplefreight.wd5.myworkdayjobs.com/en-US/External",
        "https://examplefreight.wd5.myworkdayjobs.com/External/job/Example-City/Clerk_JR00042",
        "https://ExampleFreight.wd5.myworkdayjobs.com/en-US/External/job/Example-City/Clerk_JR1",
    ],
)
def test_workday_urls_reduce_to_tenant_host_and_site(address: str) -> None:
    hit = discover.classify(address, [])
    assert hit is not None
    assert (hit.kind, hit.ats_type, hit.ats_slug, hit.ats_host, hit.ats_site) == (
        "board",
        "workday",
        "examplefreight",
        "wd5",
        "External",
    )
    assert hit.url == "https://examplefreight.wd5.myworkdayjobs.com/External"
    assert hit.suggested_name == "examplefreight"


@pytest.mark.parametrize(
    "address",
    [
        "https://jobs.smartrecruiters.com/",
        "https://jobs.smartrecruiters.com/ExampleRobotics/not-a-posting",
        "https://jobs.smartrecruiters.com/ExampleRobotics/1/extra",
        "https://www.bamboohr.com/careers",
        "https://www.bamboohr.com/careers/41",
        "https://examplerobotics.bamboohr.com/",
        "https://examplerobotics.bamboohr.com/careers/abc",
        "https://examplerobotics.bamboohr.com/login.php",
        "https://a.b.bamboohr.com/careers",
        "https://www.myworkdayjobs.com/External",
        "https://examplefreight.myworkdayjobs.com/External",
        "https://examplefreight.wdx.myworkdayjobs.com/External",
        "https://examplefreight.wd5.myworkdayjobs.com/",
        "https://examplefreight.wd5.myworkdayjobs.com/en-US",
        "https://examplefreight.wd5.myworkdayjobs.com/wday/cxs/examplefreight/External/jobs",
        "https://examplefreight.wd5.myworkdayjobs.com/External/details/Clerk_1",
        "https://examplefreight.wd5.myworkdayjobs.com/External/job",
        "http://examplefreight.wd5.myworkdayjobs.com/External",
    ],
)
def test_other_urls_on_the_new_ats_hosts_are_dropped(address: str) -> None:
    assert discover.classify(address, []) is None


def test_new_ats_boards_are_queued_once_and_watched_ones_are_not(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    config = {
        "watchlist": [
            {
                "name": "Example Freight",
                "ats_type": "workday",
                "ats_slug": "ExampleFreight",
                "ats_host": "wd5",
                "ats_site": "External",
            }
        ]
    }
    hits = [
        discover.classify(address, [])
        for address in (
            "https://examplefreight.wd5.myworkdayjobs.com/External/job/Example-City/Clerk_JR1",
            "https://examplefreight.wd5.myworkdayjobs.com/Careers",
            "https://examplefreight.wd5.myworkdayjobs.com/en-US/Careers",
            "https://examplerobotics.bamboohr.com/careers/41",
            "https://jobs.smartrecruiters.com/ExampleRobotics/744000000000001",
        )
    ]
    found = discover._store(conn, config, [h for h in hits if h is not None], "2026-11-01", [])
    assert [(row.ats_type, row.ref) for row in found] == [
        ("workday", "examplefreight"),
        ("bamboohr", "examplerobotics"),
        ("smartrecruiters", "ExampleRobotics"),
    ]
    row = conn.execute(
        "SELECT ats_host, ats_site FROM discovery_candidates WHERE ats_type = 'workday'"
    ).fetchone()
    assert tuple(row) == ("wd5", "Careers")
    conn.close()


def test_approving_a_workday_board_stores_its_identity(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    hit = discover.classify("https://examplefreight.wd5.myworkdayjobs.com/External", [])
    assert hit is not None
    (queued,) = discover._store(conn, {}, [hit], "2026-11-01", [])
    discover.approve(conn, queued.id)
    row = conn.execute(
        "SELECT name, ats_type, ats_slug, ats_host, ats_site, watchlist, source FROM companies"
    ).fetchone()
    assert tuple(row) == (
        "examplefreight",
        "workday",
        "examplefreight",
        "wd5",
        "External",
        1,
        "discovery",
    )
    conn.close()


def test_approving_a_board_whose_stored_site_differs_is_refused(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    from careerhunter import search_store

    with db.transaction(conn):
        search_store.upsert_company(
            conn,
            "examplefreight",
            ats_type="workday",
            ats_slug="examplefreight",
            ats_host="wd5",
            ats_site="Careers",
        )
    hit = discover.classify("https://examplefreight.wd5.myworkdayjobs.com/External", [])
    assert hit is not None
    (queued,) = discover._store(conn, {}, [hit], "2026-11-01", [])
    with pytest.raises(discover.DiscoverError, match="disagrees"):
        discover.approve(conn, queued.id)
    conn.close()


# --- etiquette: spacing, backoff and stop signals (#40) --------------------------------

_ETIQUETTE = {
    "discovery": {
        "searxng_url": "http://127.0.0.1:8888/search",
        "queries_per_run": 3,
        "query_delay_seconds": 7,
        "interval_minutes": 60,
    },
    "search": {"target_titles": ["One"]},
}


class _Clock:
    def __init__(self) -> None:
        self.moment = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += dt.timedelta(**delta)


def _empty() -> _Talk:
    return _Talk(json.dumps({"results": []}).encode())


def _state(conn: sqlite3.Connection) -> tuple[object, ...]:
    row = conn.execute("SELECT cursor, failures, backoff_until FROM discovery_state").fetchone()
    return tuple(row)


def test_queries_are_spaced_by_the_configured_delay(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    pauses: list[float] = []
    talk = _empty()
    discover.run(conn, _ETIQUETTE, today="2026-11-02", transport=talk, sleep=pauses.append)
    assert len(talk.calls) == 3
    assert pauses == [7, 7]  # between queries, not before the first or after the last
    conn.close()


def test_failed_passes_back_off_exponentially_and_a_success_clears_it(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    clock = _Clock()
    down = _Talk(b"", status=0, fail_at=1)
    run = {"today": "2026-11-02", "now": clock}
    assert discover.run(conn, _ETIQUETTE, transport=down, **run).notice == discover.MISS
    assert _state(conn) == (0, 1, "2026-11-02T10:00:00Z")  # one interval, 60 minutes
    clock.advance(minutes=59)
    waiting = _empty()
    outcome = discover.run(conn, _ETIQUETTE, transport=waiting, **run)
    assert outcome == discover.Outcome("discovery backing off until 2026-11-02T10:00:00Z", ())
    assert waiting.calls == []
    clock.advance(minutes=1)
    assert discover.run(conn, _ETIQUETTE, transport=down, **run).notice == discover.MISS
    assert _state(conn) == (0, 2, "2026-11-02T12:00:00Z")  # 60 times 2, from 10:00
    clock.advance(hours=2)
    assert discover.run(conn, _ETIQUETTE, transport=_empty(), **run).notice is None
    assert _state(conn) == (3, 0, None)
    conn.close()


def test_the_backoff_doubles_from_one_interval_and_is_capped_at_a_day() -> None:
    assert dt.timedelta(hours=24) == discover.BACKOFF_CAP
    assert discover.backoff_for(1, 60) == dt.timedelta(hours=1)
    assert discover.backoff_for(2, 60) == dt.timedelta(hours=2)
    assert discover.backoff_for(5, 60) == dt.timedelta(hours=16)
    assert discover.backoff_for(6, 60) == dt.timedelta(hours=24)
    assert discover.backoff_for(99, 60) == dt.timedelta(hours=24)
    # With the default daily interval, a failed pass waits for the next normal one.
    assert discover.backoff_for(1, 1440) == discover.backoff_for(3, 1440) == dt.timedelta(days=1)


def test_clear_backoff_ends_it_and_keeps_the_rest(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    clock = _Clock()
    down = _Talk(b"", status=0, fail_at=1)
    discover.run(conn, _ETIQUETTE, transport=down, today="2026-11-02", now=clock)
    before = discover.clear_backoff(conn)
    assert before == discover.Backoff(1, "2026-11-02T10:00:00Z")
    assert _state(conn) == (0, 0, None)
    unproductive = conn.execute("SELECT unproductive FROM discovery_state").fetchone()[0]
    assert unproductive == 1  # not cleared: it counts passes that ran
    assert discover.clear_backoff(conn) == discover.Backoff(0, None)
    assert (
        discover.run(conn, _ETIQUETTE, transport=_empty(), today="2026-11-02", now=clock).notice
        is None
    )
    conn.close()


def test_the_cli_clears_the_backoff(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = db.connect(inited / "search.db")
    try:
        conn.execute(
            "UPDATE discovery_state SET failures = 3, backoff_until = '2026-11-03T09:00:00Z'"
        )
    finally:
        conn.close()
    assert cli.main(["discover", "clear-backoff"], probe=probe) == 0
    assert capsys.readouterr().out == (
        "discovery: backoff cleared (it was until 2026-11-03T09:00:00Z, after 3 failed "
        "passes in a row); the next pass runs when it is due\n"
    )
    assert cli.main(["discover", "clear-backoff"], probe=probe) == 0
    assert capsys.readouterr().out == "discovery: no backoff to clear\n"
    assert cli.main(["discover", "clear-backoff", "3"], probe=probe) == 1


def test_doctor_names_the_backoff_and_the_command_that_ends_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _search_db(tmp_path)
    now = dt.datetime(2026, 11, 2, 9, 30, tzinfo=dt.UTC)
    talk = _Talk(_engines([], ("engine one", "CAPTCHA")))
    discover.run(conn, _ETIQUETTE, transport=talk, today="2026-11-02", now=_Clock())
    cli._doctor_discovery_backoff(cli._Report(), conn, _ETIQUETTE, now)
    assert capsys.readouterr().out == (
        "discovery       WARN backing off until 2026-11-02T10:00:00Z after 1 failed pass in a "
        "row; engines throttled: engine one; to retry sooner, run: careerhunter discover "
        "clear-backoff\n"
    )
    later = dt.datetime(2026, 11, 2, 10, 30, tzinfo=dt.UTC)  # past it: no warning
    cli._doctor_discovery_backoff(cli._Report(), conn, _ETIQUETTE, later)
    assert capsys.readouterr().out == (
        "discovery       --  engines throttled in the last pass: engine one\n"
    )
    cli._doctor_discovery_backoff(cli._Report(), conn, {"discovery": {"searxng_url": None}}, now)
    assert capsys.readouterr().out == ""  # discovery off
    conn.close()


def _engines(results: list[dict[str, str]], *pairs: tuple[str, str]) -> bytes:
    return json.dumps(
        {"results": results, "unresponsive_engines": [list(pair) for pair in pairs]}
    ).encode()


def _suspended(conn: sqlite3.Connection) -> tuple[object, ...]:
    row = conn.execute("SELECT suspended_engines, suspended_at FROM discovery_state")
    return tuple(row.fetchone())


def test_a_429_from_searxng_stops_the_pass_and_starts_the_backoff(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    talk = _Talk(b"", status=429, fail_at=1)
    outcome = discover.run(conn, _ETIQUETTE, today="2026-11-02", transport=talk)
    assert outcome == discover.Outcome(discover.THROTTLED, ())
    assert len(talk.calls) == 1  # stopped at once
    assert _rows(conn) == 0
    assert _state(conn)[:2] == (0, 1)
    conn.close()


@pytest.mark.parametrize("error", ["CAPTCHA", "too many requests", "Suspended: CAPTCHA"])
def test_one_throttled_engine_does_not_stop_a_pass_that_gets_results(
    tmp_path: Path, error: str
) -> None:
    conn = _search_db(tmp_path)
    talk = _Talk(_engines([{"url": _GOOD}], ("Example Engine", error)))
    outcome = discover.run(conn, _ETIQUETTE, today="2026-11-02", transport=talk, confirm=_exists)
    assert outcome.notice is None
    assert len(talk.calls) == 3  # every query was asked
    assert "discovery: engines throttled or suspended: example engine" in outcome.notes
    assert _rows(conn) == 1
    assert _state(conn) == (3, 0, None)  # the cursor moved, no backoff
    assert _suspended(conn)[0] == '["example engine"]'
    conn.close()


def test_a_blocked_query_is_passed_over_while_others_answer(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    replies = [
        _engines([], ("example engine", "Suspended: too many requests")),  # blocked
        _engines([{"url": _GOOD}], ("example engine", "Suspended: too many requests")),
        _engines([]),  # answered, with nothing found
    ]
    calls: list[str] = []

    def talk(url: str) -> tuple[int, bytes]:
        calls.append(url)
        return 200, replies[len(calls) - 1]

    outcome = discover.run(conn, _ETIQUETTE, today="2026-11-02", transport=talk, confirm=_exists)
    assert outcome.notice is None and len(calls) == 3
    assert _rows(conn) == 1 and _state(conn)[1:] == (0, None)


@pytest.mark.parametrize("error", ["CAPTCHA", "too many requests", "Suspended: access denied"])
def test_when_every_engine_is_throttled_the_pass_stops_and_backs_off(
    tmp_path: Path, error: str
) -> None:
    conn = _search_db(tmp_path)
    talk = _Talk(_engines([], ("engine one", error), ("engine two", error)))
    outcome = discover.run(conn, _ETIQUETTE, today="2026-11-02", transport=talk)
    assert outcome.notice == discover.THROTTLED
    assert outcome.notes == ("discovery: engines throttled or suspended: engine one, engine two",)
    assert len(talk.calls) == discover.BLOCKED_IN_A_ROW  # stopped early
    assert _rows(conn) == 0
    assert _state(conn)[:2] == (0, 1)
    assert _suspended(conn)[0] == '["engine one", "engine two"]'
    (health,) = [h for h in search_store.component_health(conn) if h.component == "searxng"]
    assert health.last_failure == "throttled"
    conn.close()


def test_a_pass_with_no_throttled_engine_clears_the_record(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    discover.run(
        conn,
        _ETIQUETTE,
        today="2026-11-02",
        transport=_Talk(_engines([], ("engine one", "CAPTCHA"))),
    )
    assert _suspended(conn)[0] is not None
    conn.execute("UPDATE discovery_state SET failures = 0, backoff_until = NULL")
    discover.run(conn, _ETIQUETTE, today="2026-11-02", transport=_empty())
    assert _suspended(conn) == (None, None)


def test_an_engine_name_that_is_not_plain_text_is_unnamed(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    talk = _Talk(_engines([{"url": _GOOD}], ("<b>engine</b>", "CAPTCHA")))
    outcome = discover.run(conn, _ETIQUETTE, today="2026-11-02", transport=talk)
    assert "discovery: engines throttled or suspended: unnamed" in outcome.notes


def test_another_engine_error_is_not_a_stop(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    body = json.dumps(
        {"results": [{"url": _GOOD}], "unresponsive_engines": [["example engine", "timeout"]]}
    ).encode()
    outcome = discover.run(
        conn, _ETIQUETTE, today="2026-11-02", transport=_Talk(body), confirm=_exists
    )
    assert outcome.notice is None
    assert _rows(conn) == 1
    conn.close()


@pytest.mark.parametrize("value", [1, 31, "5", True])
def test_the_query_delay_is_checked(value: object) -> None:
    from careerhunter.config import discovery_query_delay_seconds, validate_config

    doc = {"discovery": {"query_delay_seconds": value}}
    assert any("discovery.query_delay_seconds" in p for p in validate_config(doc))
    assert discovery_query_delay_seconds(doc) == 5


def test_a_manual_run_right_after_another_pass_is_skipped(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(inited)
    talk = _Talk(json.dumps({"results": []}).encode())
    monkeypatch.setattr(discover, "default_transport", talk)
    assert cli.main(["discover"], probe=probe) == 0
    sent = len(talk.calls)
    capsys.readouterr()
    assert cli.main(["discover"], probe=probe) == 0  # the clock has not moved
    assert capsys.readouterr().err == f"{cli.BUSY}\n"
    assert len(talk.calls) == sent


def test_two_claims_inside_the_gap_cannot_both_win(tmp_path: Path) -> None:
    first = _search_db(tmp_path)
    second = db.connect(tmp_path / "search.db")
    moment = dt.datetime(2026, 11, 2, 9, tzinfo=dt.UTC)
    gap = dt.timedelta(minutes=60)
    assert discover.claim_pass(first, moment, gap) is True
    assert discover.claim_pass(second, moment + dt.timedelta(minutes=59), gap) is False
    assert discover.claim_pass(second, moment + dt.timedelta(minutes=60), gap) is True
    first.close()
    second.close()


def test_the_pauses_of_a_pass_must_fit_its_share_of_the_watch_run() -> None:
    from careerhunter.config import validate_config

    doc = {"discovery": {"queries_per_run": 40, "query_delay_seconds": 5}}
    assert any("must be at most 120 seconds" in p for p in validate_config(doc))
    doc = {"discovery": {"queries_per_run": 24, "query_delay_seconds": 5}}
    assert not any("discovery" in p for p in validate_config(doc))


def test_the_engine_record_must_be_a_json_array(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    for bad in ("not json", '{"a": 1}'):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE discovery_state SET suspended_engines = ?", (bad,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE discovery_state SET suspended_at = 'yesterday'")
    conn.close()
