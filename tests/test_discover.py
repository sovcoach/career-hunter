"""Discovery queues companies from result URLs. Snippets stay out of the database."""

from __future__ import annotations

import http.client
import inspect
import json
import sqlite3
import urllib.parse
from pathlib import Path

import pytest
import yaml

from careerhunter import cli, db, discover, search_store
from careerhunter.platform_checks import SystemProbe
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
    capsys.readouterr()
    return data_env


def test_queries_stop_at_twelve_and_use_the_first_title_variant() -> None:
    config = {
        "search": {
            "lanes": [{"titles": ["One", ["Alpha", "Beta"], "Three", "Four"]}],
            "locations": ["Example City", "Sample Town"],
        },
        "discovery": {"career_hosts": ["jobs.example.com"]},
    }
    queries = discover.build_queries(config)
    assert len(queries) == 12
    assert queries[0] == "site:boards.greenhouse.io One Example City"
    assert any("Alpha" in query for query in queries)
    assert "Beta" not in "\n".join(queries)
    assert discover.build_queries({"search": {}}) == []
    bare = discover.build_queries({"search": {"target_titles": ["One"]}})
    assert bare[0] == "site:boards.greenhouse.io One"
    assert len(bare) == 4


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
    bad = _Talk(b"<html>", status=200)
    outcome = discover.run(conn, config, today="2026-11-02", transport=bad)
    assert outcome.notice == discover.MISS
    partial = _Talk(_body(["https://jobs.lever.co/freshlogistics/post-1"]), fail_at=3, status=500)
    outcome = discover.run(conn, config, today="2026-11-02", transport=partial)
    assert outcome.notice == discover.MISS
    assert partial.calls
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
        "4 career jobs.example.com - https://jobs.example.com/careers",
    ]
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
    assert not (inited / "logs" / "discover.log").exists()


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
    assert not (inited / "logs" / "discover.log").exists()


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
    assert not (inited / "logs" / "discover.log").exists()


def test_the_client_returns_a_redirect_and_does_not_follow_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    class Response:
        status = 302

        def read(self, amount: int) -> bytes:
            assert amount == discover.MAX_BODY + 1
            return b"go away"

    class Conn:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls.append(("open", host, str(port)))

        def request(
            self,
            method: str,
            path: str,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            assert headers == {"Accept": "application/json"}
            calls.append((method, path, ""))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            calls.append(("close", "", ""))

    monkeypatch.setattr(http.client, "HTTPConnection", Conn)
    status, body = discover.default_transport("http://127.0.0.1:8888/search?q=One&format=json")
    assert status == 302
    assert body == b"go away"
    assert calls == [
        ("open", "127.0.0.1", "8888"),
        ("GET", "/search?q=One&format=json", ""),
        ("close", "", ""),
    ]
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


def test_the_alert_query_stays_on_watcher_postings(search: sqlite3.Connection) -> None:
    text = inspect.getsource(search_store.pending_alert_ids)
    assert "p.source = 'watcher'" in text
    assert "discovery" not in text
    watcher, _inserted = search_store.upsert_company(search, "Example Robotics", source="watcher")
    other, _also = search_store.upsert_company(search, "Example Org", source="discovery")
    search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, llm_output) "
        "VALUES (?, 'Operations Coordinator', '2026-11-02', 'watcher', 'synthetic')",
        (watcher,),
    )
    watcher_id = int(search.execute("SELECT last_insert_rowid()").fetchone()[0])
    search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, llm_output) "
        "VALUES (?, 'Customer Success Specialist', '2026-11-02', 'discovery', 'synthetic')",
        (other,),
    )
    assert search_store.pending_alert_ids(search) == [watcher_id]


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


def _rows(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("error", BROKEN, ids=lambda e: type(e).__name__)
def test_a_broken_reply_from_the_client_is_a_miss(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(http.client, "HTTPConnection", broken_connection(error))
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
    assert outcome == discover.Outcome(discover.MISS, ())
    assert _rows(conn) == 0
    conn.close()


def test_a_result_without_a_url_is_skipped_and_the_good_one_kept(tmp_path: Path) -> None:
    body = json.dumps({"results": [{"url": _GOOD}, {"title": "t"}]}).encode()
    conn = _search_db(tmp_path)
    outcome = discover.run(conn, _SEARCH, today="2026-11-02", transport=_Talk(body))
    assert outcome.notice is None
    assert [row.url for row in outcome.rows] == ["https://boards.greenhouse.io/newrobotics"]
    assert _rows(conn) == 1
    conn.close()


def test_an_empty_result_list_is_a_normal_reply(tmp_path: Path) -> None:
    conn = _search_db(tmp_path)
    talk = _Talk(json.dumps({"results": []}).encode())
    outcome = discover.run(conn, _SEARCH, today="2026-11-02", transport=talk)
    assert outcome == discover.Outcome(None, ())
    assert len(talk.calls) == 4  # every query was sent; none was a miss
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
    assert captured.err == f"{discover.MISS}\n"
    assert _count(inited) == 0
    assert (inited / "logs" / "discover.success").is_file()
