"""Watching Greenhouse, Lever and Ashby boards. Synthetic fixtures only; no sockets."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, db, discover, search_store
from careerhunter import watch as w
from careerhunter.fetch import Fetched, FetchError
from careerhunter.models import Posting
from careerhunter.platform_checks import SystemProbe
from careerhunter.score import Score
from careerhunter.storage_guard import DataDir

DAY = "2026-11-01"


class Scripted:
    """A fetcher that never opens a socket. An unknown URL fails the test."""

    def __init__(self, mapping: dict[str, Fetched | Exception]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str) -> Fetched:
        self.calls.append(url)
        found = self.mapping[url]
        if isinstance(found, Exception):
            raise found
        return found


def url(ats: str, slug: str) -> str:
    found = w.board_url(ats, slug)
    assert found is not None
    return found


def company(
    name: str,
    ats: str | None = None,
    slug: str | None = None,
    **extra: object,
) -> dict[str, object]:
    item: dict[str, object] = {"name": name}
    if ats is not None:
        item["ats_type"] = ats
    if slug is not None:
        item["ats_slug"] = slug
    item.update(extra)
    return item


def fetched(
    payload: object | None = None,
    *,
    body: bytes | None = None,
    status: int = 200,
    retry_after: str | None = None,
) -> Fetched:
    raw = body if body is not None else json.dumps(payload).encode()
    return Fetched("https://boards.example/test", status, "application/json", raw, retry_after)


def one_greenhouse(
    slug: str, title: str = "Operations Coordinator", ident: int = 1001
) -> dict[str, object]:
    return {
        "jobs": [
            {
                "id": ident,
                "title": title,
                "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/{ident}",
                "location": {"name": "Example City"},
                "requisition_id": f"REQ-{ident}",
            }
        ]
    }


def postings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT c.name AS company, c.ats_type, c.ats_slug, c.watchlist, "
            "c.source AS company_source, p.title, p.location, p.work_model, p.schedule, "
            "p.url, p.requisition_id, p.first_seen, p.source, p.fit_rating, p.gaps, "
            "p.llm_output, p.scout_metadata, p.snapshot_path "
            "FROM postings p JOIN companies c ON c.id = p.company_id ORDER BY p.id"
        )
    )


def companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT name, ats_type, ats_slug, watchlist, source FROM companies ORDER BY name"
        )
    )


def assert_untouched(conn: sqlite3.Connection, datadir: DataDir) -> None:
    for sql in (
        "SELECT count(*) AS n FROM applications",
        "SELECT count(*) AS n FROM skips",
        "SELECT count(*) AS n FROM decision_history",
        "SELECT count(*) AS n FROM bot_tokens",
        "SELECT count(*) AS n FROM bot_refs",
        "SELECT count(*) AS n FROM bot_prompts",
        "SELECT count(*) AS n FROM bot_state",
    ):
        assert conn.execute(sql).fetchone()["n"] == 0
    assert list(datadir.root.glob("ei_record_*")) == []


def assert_watcher(row: sqlite3.Row) -> None:
    assert row["source"] == "watcher"
    assert row["first_seen"] == DAY
    assert row["schedule"] is None
    assert row["fit_rating"] is None
    assert row["gaps"] is None
    assert row["llm_output"] is None
    assert row["scout_metadata"] is None
    assert row["snapshot_path"] is None


def watched(
    conn: sqlite3.Connection,
    datadir: DataDir,
    items: list[object],
    fetcher: Scripted,
) -> list[str]:
    lines = w.run(conn, items, today=DAY, fetcher=fetcher)
    assert_untouched(conn, datadir)
    return lines


def test_board_url_is_built_only_from_a_safe_slug() -> None:
    assert (
        w.board_url("greenhouse", "examplerobotics")
        == "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs"
    )
    assert (
        w.board_url("lever", "samplelogistics")
        == "https://api.lever.co/v0/postings/samplelogistics?mode=json"
    )
    assert (
        w.board_url("ashby", "exampleorg")
        == "https://api.ashbyhq.com/posting-api/job-board/exampleorg"
    )
    assert (
        w.board_url("greenhouse", "example-robotics_1")
        == "https://boards-api.greenhouse.io/v1/boards/example-robotics_1/jobs"
    )
    assert w.board_url("workday", "example") is None
    assert w.board_url("icims", "example") is None


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "a/b",
        "a?x=1",
        "evil.example",
        "has space",
        "a#b",
        "..",
        "https://evil.example/a",
        "a\\b",
    ],
)
def test_an_unsafe_slug_builds_no_url(slug: str) -> None:
    for ats in ("greenhouse", "lever", "ashby"):
        assert w.board_url(ats, slug) is None


def test_greenhouse_board_normalizes_to_the_common_shape(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    payload = {
        "jobs": [
            {
                "id": 1001,
                "title": "Operations Coordinator",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/1001",
                "location": {"name": "Example City"},
                "requisition_id": "REQ-1001",
                "company_name": "Example Robotics",
            },
            {
                "id": 1002,
                "title": "Http Coordinator",
                "absolute_url": "http://boards.greenhouse.io/examplerobotics/jobs/1002",
                "location": {"name": "Example City"},
            },
            {
                "id": 1003,
                "title": "Script Coordinator",
                "absolute_url": "javascript:alert(1)",
                "requisition_id": 55,
            },
            {
                "id": 1004,
                "title": "Constructed Coordinator",
                "requisition_id": "",
                "location": "Example City",
            },
            {
                "id": 1005,
                "title": "Userinfo Coordinator",
                "absolute_url": "https://user:pass@boards.greenhouse.io/examplerobotics/jobs/1005",
            },
            {"title": "Bool Id", "id": True, "requisition_id": "REQ-BOOL"},
            {"title": "Slash Id", "id": "bad/id", "requisition_id": "REQ-SLASH"},
            {"id": 1006, "title": "   "},
            "not-a-job",
            {"title": "No Identity Role", "location": {"name": "Example City"}},
        ]
    }
    item = company(
        "Example Robotics",
        "greenhouse",
        "examplerobotics",
        careers_url="https://evil.example/careers",
    )
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    lines = watched(search, datadir, [item], fetcher)

    assert fetcher.calls == [url("greenhouse", "examplerobotics")]
    rows = postings(search)
    assert [
        (row["title"], row["location"], row["work_model"], row["url"], row["requisition_id"])
        for row in rows
    ] == [
        (
            "Operations Coordinator",
            "Example City",
            None,
            "https://boards.greenhouse.io/examplerobotics/jobs/1001",
            "REQ-1001",
        ),
        (
            "Http Coordinator",
            "Example City",
            None,
            "http://boards.greenhouse.io/examplerobotics/jobs/1002",
            None,
        ),
        (
            "Script Coordinator",
            "",
            None,
            "https://boards.greenhouse.io/examplerobotics/jobs/1003",
            None,
        ),
        (
            "Constructed Coordinator",
            "",
            None,
            "https://boards.greenhouse.io/examplerobotics/jobs/1004",
            None,
        ),
        (
            "Userinfo Coordinator",
            "",
            None,
            "https://boards.greenhouse.io/examplerobotics/jobs/1005",
            None,
        ),
        ("Bool Id", "", None, None, "REQ-BOOL"),
        ("Slash Id", "", None, None, "REQ-SLASH"),
    ]
    for row in rows:
        assert_watcher(row)
        assert row["company"] == "Example Robotics"
    stored = companies(search)
    assert len(stored) == 1
    assert stored[0]["ats_type"] == "greenhouse"
    assert stored[0]["ats_slug"] == "examplerobotics"
    assert stored[0]["watchlist"] == 1
    assert stored[0]["source"] == "watcher"
    assert "a job has no title; skipped" in "\n".join(lines)
    assert "a job was not an object; skipped" in "\n".join(lines)
    assert "No Identity Role" in "\n".join(lines)
    assert "7 new, 0 already recorded" in lines[-2]
    assert lines[-1] == "watch: 1 boards fetched, 0 failed"


def test_lever_board_normalizes_including_a_missing_work_model(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    payload = [
        {
            "id": "abc-123",
            "text": "Warehouse Clerk",
            "categories": {"location": "Example City", "team": "Ops"},
            "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
            "workplaceType": "hybrid",
        },
        {
            "id": "def-456",
            "text": "On Site Clerk",
            "categories": {"location": "Example City"},
            "workplaceType": "on-site",
        },
        {"id": "ghi-789", "text": "Unspecified Clerk", "workplaceType": "unspecified"},
        {"id": "jkl-012", "text": "Onsite Clerk", "workplaceType": "onsite"},
        {
            "id": "mno-345",
            "text": "No Workplace",
            "hostedUrl": "https://jobs.lever.co/samplelogistics/mno-345",
        },
        {"text": "Missing Id Clerk"},
        {"text": "   "},
        "not-a-job",
    ]
    # Role filters are applied when the run is given config. This test is normalization.
    item = company(
        "Sample Logistics",
        "lever",
        "samplelogistics",
        careers_url="https://example.org/jobs",
    )
    fetcher = Scripted({url("lever", "samplelogistics"): fetched(payload)})
    lines = watched(search, datadir, [item], fetcher)

    assert fetcher.calls == [url("lever", "samplelogistics")]
    rows = postings(search)
    assert [
        (row["title"], row["location"], row["work_model"], row["url"], row["requisition_id"])
        for row in rows
    ] == [
        (
            "Warehouse Clerk",
            "Example City",
            "hybrid",
            "https://jobs.lever.co/samplelogistics/abc-123",
            "abc-123",
        ),
        (
            "On Site Clerk",
            "Example City",
            "onsite",
            "https://jobs.lever.co/samplelogistics/def-456",
            "def-456",
        ),
        ("Unspecified Clerk", "", None, "https://jobs.lever.co/samplelogistics/ghi-789", "ghi-789"),
        ("Onsite Clerk", "", "onsite", "https://jobs.lever.co/samplelogistics/jkl-012", "jkl-012"),
        ("No Workplace", "", None, "https://jobs.lever.co/samplelogistics/mno-345", "mno-345"),
    ]
    for row in rows:
        assert_watcher(row)
        assert row["company"] == "Sample Logistics"
    joined = "\n".join(lines)
    assert "Missing Id Clerk" in joined
    assert "a job has no title; skipped" in joined
    assert "a job was not an object; skipped" in joined
    assert "5 new, 0 already recorded" in lines[-2]
    assert lines[-1] == "watch: 1 boards fetched, 0 failed"


def test_ashby_work_model_prefers_workplace_type(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    payload = {
        "jobs": [
            {
                "id": "uuid-1",
                "title": "Hybrid Coordinator",
                "location": "Example City",
                "isRemote": True,
                "workplaceType": "Hybrid",
                "jobUrl": "https://jobs.ashbyhq.com/exampleorg/uuid-1",
            },
            {"id": "uuid-2", "title": "Remote Only", "location": "Example City", "isRemote": True},
            {"id": "uuid-3", "title": "Office Role", "isRemote": False, "workplaceType": "OnSite"},
            {
                "id": "uuid-4",
                "title": "Odd Type",
                "isRemote": True,
                "workplaceType": "teleport",
            },
            {"id": "uuid-5", "title": "No Flag"},
            {"title": "   "},
            "not-a-job",
        ]
    }
    fetcher = Scripted({url("ashby", "exampleorg"): fetched(payload)})
    lines = watched(
        search,
        datadir,
        [company("Example Org", "ashby", "exampleorg", careers_url="https://evil.example/jobs")],
        fetcher,
    )

    assert fetcher.calls == [url("ashby", "exampleorg")]
    rows = postings(search)
    assert [
        (row["title"], row["location"], row["work_model"], row["url"], row["requisition_id"])
        for row in rows
    ] == [
        (
            "Hybrid Coordinator",
            "Example City",
            "hybrid",
            "https://jobs.ashbyhq.com/exampleorg/uuid-1",
            "uuid-1",
        ),
        (
            "Remote Only",
            "Example City",
            "remote",
            "https://jobs.ashbyhq.com/exampleorg/uuid-2",
            "uuid-2",
        ),
        ("Office Role", "", "onsite", "https://jobs.ashbyhq.com/exampleorg/uuid-3", "uuid-3"),
        ("Odd Type", "", None, "https://jobs.ashbyhq.com/exampleorg/uuid-4", "uuid-4"),
        ("No Flag", "", None, "https://jobs.ashbyhq.com/exampleorg/uuid-5", "uuid-5"),
    ]
    for row in rows:
        assert_watcher(row)
    joined = "\n".join(lines)
    assert "a job has no title; skipped" in joined
    assert "a job was not an object; skipped" in joined


def test_a_second_run_inserts_nothing(search: sqlite3.Connection, datadir: DataDir) -> None:
    payload = one_greenhouse("examplerobotics")
    item = company("Example Robotics", "greenhouse", "examplerobotics")
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    first = watched(search, datadir, [item], fetcher)
    again = watched(search, datadir, [item], fetcher)

    assert "1 new, 0 already recorded" in first[-2]
    assert "0 new, 1 already recorded" in again[-2]
    assert len(postings(search)) == 1
    assert len(companies(search)) == 1
    assert fetcher.calls == [url("greenhouse", "examplerobotics")] * 2


def test_one_board_failing_does_not_stop_the_run(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    marker = b'{"jobs":[{"title":"Do Not Store","id":1}],"note":"BODY-MARKER"}'
    fetcher = Scripted(
        {
            url("greenhouse", "examplefetch"): FetchError(
                "couldn't fetch boards-api.greenhouse.io: TimeoutError"
            ),
            url("greenhouse", "examplestatus"): fetched(body=marker, status=500),
            url("greenhouse", "examplejson"): fetched(body=b"{"),
            url("greenhouse", "examplelist"): fetched(body=b"[]"),
            url("lever", "exampleshape"): fetched(body=b'{"jobs":[]}'),
            url("ashby", "exampleorg"): fetched(
                {
                    "jobs": [
                        {
                            "id": "uuid-1",
                            "title": "Operations Coordinator",
                            "jobUrl": "https://jobs.ashbyhq.com/exampleorg/uuid-1",
                        }
                    ]
                }
            ),
        }
    )
    items: list[object] = [
        company("Example Fetch", "greenhouse", "examplefetch"),
        company("Example Status", "greenhouse", "examplestatus"),
        company("Example Json", "greenhouse", "examplejson"),
        company("Example List", "greenhouse", "examplelist"),
        company("Example Shape", "lever", "exampleshape"),
        company("Example Org", "ashby", "exampleorg"),
    ]
    lines = watched(search, datadir, items, fetcher)

    assert lines == [
        "watch: Example Fetch (greenhouse): FetchError; nothing recorded",
        "watch: Example Status (greenhouse): HTTP 500; nothing recorded",
        "watch: Example Json (greenhouse): HTTP 200, invalid JSON; nothing recorded",
        "watch: Example List (greenhouse): HTTP 200, unexpected JSON shape; nothing recorded",
        "watch: Example Shape (lever): HTTP 200, unexpected JSON shape; nothing recorded",
        "watch: Example Org (ashby): 1 new, 0 already recorded",
        "watch: 1 boards fetched, 5 failed",
    ]
    joined = "\n".join(lines)
    assert "BODY-MARKER" not in joined
    assert "TimeoutError" not in joined
    assert "Do Not Store" not in joined
    assert [row["name"] for row in companies(search)] == ["Example Org"]
    assert [row["title"] for row in postings(search)] == ["Operations Coordinator"]


def test_a_429_records_retry_after_and_does_not_parse_the_body(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    body = (
        b'{"jobs":[{"title":"Do Not Store","id":9,'
        b'"absolute_url":"https://boards.greenhouse.io/examplerobotics/jobs/9"}],'
        b'"note":"BODY-MARKER"}'
    )
    blocked = url("greenhouse", "exampleother")
    fetcher = Scripted(
        {
            url("greenhouse", "examplerobotics"): fetched(body=body, status=429, retry_after="120"),
            url("lever", "samplelogistics"): fetched(
                [
                    {
                        "id": "abc-123",
                        "text": "Warehouse Clerk",
                        "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
                    }
                ]
            ),
        }
    )
    lines = watched(
        search,
        datadir,
        [
            company("Example Robotics", "greenhouse", "examplerobotics"),
            company("Example Other", "greenhouse", "exampleother"),
            company("Sample Logistics", "lever", "samplelogistics"),
        ],
        fetcher,
    )

    assert blocked not in fetcher.calls
    assert fetcher.calls == [
        url("greenhouse", "examplerobotics"),
        url("lever", "samplelogistics"),
    ]
    assert lines == [
        "watch: Example Robotics (greenhouse): HTTP 429, retry-after 120; left for the next run",
        "watch: Example Other (greenhouse): "
        "boards-api.greenhouse.io already returned HTTP 429 this run; left for the next run",
        "watch: Sample Logistics (lever): 1 new, 0 already recorded",
        "watch: 1 boards fetched, 1 failed",
    ]
    assert "BODY-MARKER" not in "\n".join(lines)
    assert [row["title"] for row in postings(search)] == ["Warehouse Clerk"]
    assert [row["name"] for row in companies(search)] == ["Sample Logistics"]


def test_a_missing_or_blank_retry_after_is_logged_as_none(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    fetcher = Scripted(
        {
            url("greenhouse", "examplerobotics"): fetched(
                body=b'{"jobs":[{"title":"Do Not Store","id":1}]}', status=429
            ),
            url("lever", "samplelogistics"): fetched(
                body=b'[{"text":"Do Not Store","id":"abc"}]', status=429, retry_after="   "
            ),
        }
    )
    lines = watched(
        search,
        datadir,
        [
            company("Example Robotics", "greenhouse", "examplerobotics"),
            company("Sample Logistics", "lever", "samplelogistics"),
        ],
        fetcher,
    )
    assert lines == [
        "watch: Example Robotics (greenhouse): HTTP 429, retry-after none; left for the next run",
        "watch: Sample Logistics (lever): HTTP 429, retry-after none; left for the next run",
        "watch: 0 boards fetched, 2 failed",
    ]
    assert postings(search) == []
    assert companies(search) == []


def test_an_empty_board_still_records_the_company(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched({"jobs": []})})
    lines = watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )
    assert lines == [
        "watch: Example Robotics (greenhouse): 0 new, 0 already recorded",
        "watch: 1 boards fetched, 0 failed",
    ]
    assert postings(search) == []
    stored = companies(search)[0]
    assert stored["name"] == "Example Robotics"
    assert stored["ats_slug"] == "examplerobotics"
    assert stored["watchlist"] == 1


def test_posting_company_name_is_preferred_when_it_is_clear(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "First Role",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/1",
                "company_name": "Sample Holdings",
            },
            {
                "id": 2,
                "title": "Second Role",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/2",
                "company_name": "Sample Holdings",
            },
            {
                "id": 3,
                "title": "Third Role",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/3",
                "company_name": "Example Holdings",
            },
        ]
    }
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    lines = watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )

    joined = "\n".join(lines)
    assert joined.count("Sample Holdings") == 1
    assert (
        "posting company 'Example Holdings' differs from watchlist name 'Example Robotics'"
        in joined
    )
    by_name = {row["name"]: row for row in companies(search)}
    assert by_name["Example Robotics"]["ats_type"] == "greenhouse"
    assert by_name["Example Robotics"]["ats_slug"] == "examplerobotics"
    assert by_name["Example Robotics"]["watchlist"] == 1
    assert by_name["Example Robotics"]["source"] == "watcher"
    for name in ("Sample Holdings", "Example Holdings"):
        assert by_name[name]["ats_type"] is None
        assert by_name[name]["ats_slug"] is None
        assert by_name[name]["watchlist"] == 0
        assert by_name[name]["source"] == "watcher"
    assert [row["company"] for row in postings(search)] == [
        "Sample Holdings",
        "Sample Holdings",
        "Example Holdings",
    ]
    assert "3 new, 0 already recorded" in lines[-2]


@pytest.mark.parametrize("label", ["Confidential", " N/A ", "-", "not specified", "CONFIDENTIAL"])
def test_a_placeholder_company_uses_the_watchlist_name(
    search: sqlite3.Connection, datadir: DataDir, label: str
) -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Operations Coordinator",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/1",
                "company_name": label,
            }
        ]
    }
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    lines = watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )
    assert "differs" not in "\n".join(lines)
    assert [row["name"] for row in companies(search)] == ["Example Robotics"]
    assert postings(search)[0]["company"] == "Example Robotics"


def test_a_case_only_company_difference_is_not_a_mismatch(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Operations Coordinator",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/1",
                "company_name": "example robotics",
            }
        ]
    }
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    lines = watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )
    assert "differs" not in "\n".join(lines)
    assert [row["name"] for row in companies(search)] == ["Example Robotics"]
    assert postings(search)[0]["company"] == "Example Robotics"


@pytest.mark.parametrize(
    ("stored_type", "stored_slug"),
    [
        ("lever", None),
        ("lever", "examplerobotics"),
        ("greenhouse", "other"),
        (None, "other"),
    ],
)
def test_an_ats_disagreement_skips_the_company_without_writes(
    search: sqlite3.Connection,
    datadir: DataDir,
    stored_type: str | None,
    stored_slug: str | None,
) -> None:
    search_store.upsert_company(
        search,
        "Example Robotics",
        ats_type=stored_type,
        ats_slug=stored_slug,
        source="manual",
        watchlist=0,
    )
    fetcher = Scripted(
        {url("greenhouse", "examplerobotics"): fetched(one_greenhouse("examplerobotics"))}
    )
    lines = watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )
    assert fetcher.calls == [url("greenhouse", "examplerobotics")]
    assert postings(search) == []
    stored = companies(search)[0]
    assert stored["ats_type"] == stored_type
    assert stored["ats_slug"] == stored_slug
    assert stored["watchlist"] == 0
    assert stored["source"] == "manual"
    assert "disagrees" in lines[0]
    assert f"ats_type={stored_type or 'null'}" in lines[0]
    assert f"ats_slug={stored_slug or 'null'}" in lines[0]
    assert lines[-1] == "watch: 0 boards fetched, 1 failed"


@pytest.mark.parametrize(
    ("stored_type", "stored_slug"),
    [
        (None, None),
        ("greenhouse", None),
        (None, "examplerobotics"),
        ("greenhouse", "examplerobotics"),
    ],
)
def test_empty_ats_fields_are_filled_and_the_source_stays(
    search: sqlite3.Connection,
    datadir: DataDir,
    stored_type: str | None,
    stored_slug: str | None,
) -> None:
    search_store.upsert_company(
        search,
        "Example Robotics",
        ats_type=stored_type,
        ats_slug=stored_slug,
        source="manual",
        watchlist=0,
    )
    fetcher = Scripted(
        {url("greenhouse", "examplerobotics"): fetched(one_greenhouse("examplerobotics"))}
    )
    watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )
    stored = companies(search)[0]
    assert stored["ats_type"] == "greenhouse"
    assert stored["ats_slug"] == "examplerobotics"
    assert stored["watchlist"] == 1
    assert stored["source"] == "manual"
    assert len(postings(search)) == 1
    assert_watcher(postings(search)[0])


def test_a_weak_job_is_not_inserted_and_the_board_continues(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    payload = {
        "jobs": [
            {"title": "No Identity Role", "location": {"name": "Example City"}},
            {
                "id": 2,
                "title": "Operations Coordinator",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/2",
            },
        ]
    }
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    lines = watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )
    assert [row["title"] for row in postings(search)] == ["Operations Coordinator"]
    assert "neither a url nor a requisition id" in "\n".join(lines)
    assert "1 new, 0 already recorded" in lines[-2]


def test_an_identity_conflict_skips_that_job_and_continues(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    other_id, _inserted = search_store.upsert_company(search, "Other Example", source="manual")
    shared = "https://boards.greenhouse.io/examplerobotics/jobs/1001"
    search_store.create_posting(
        search,
        other_id,
        Posting(
            company="Other Example",
            title="Operations Coordinator",
            first_seen="2026-01-15",
            url=shared,
            source="manual",
        ),
    )
    payload = {
        "jobs": [
            {
                "id": 1001,
                "title": "Operations Coordinator",
                "absolute_url": shared,
                "requisition_id": "REQ-1001",
            },
            {
                "id": 1002,
                "title": "Support Specialist",
                "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/1002",
            },
        ]
    }
    fetcher = Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    lines = watched(
        search, datadir, [company("Example Robotics", "greenhouse", "examplerobotics")], fetcher
    )
    assert [(row["company"], row["title"], row["source"]) for row in postings(search)] == [
        ("Other Example", "Operations Coordinator", "manual"),
        ("Example Robotics", "Support Specialist", "watcher"),
    ]
    assert any("identity conflict on 'Operations Coordinator'" in line for line in lines)
    assert "continuing" in "\n".join(lines)
    assert "1 new, 0 already recorded" in lines[-2]


def test_a_padded_slug_is_fetched_and_careers_url_is_ignored(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    fetcher = Scripted(
        {url("greenhouse", "examplerobotics"): fetched(one_greenhouse("examplerobotics"))}
    )
    watched(
        search,
        datadir,
        [
            company(
                "Example Robotics",
                "greenhouse",
                " examplerobotics ",
                careers_url="https://evil.example/secret",
            )
        ],
        fetcher,
    )
    assert fetcher.calls == [url("greenhouse", "examplerobotics")]
    assert companies(search)[0]["ats_slug"] == "examplerobotics"


def test_unfetched_entries_make_no_request(search: sqlite3.Connection, datadir: DataDir) -> None:
    items: list[object] = [
        company("Workday Example", "workday", "example"),
        company("Icims Example", "icims", "example"),
        company("No Type"),
        {"name": "Numeric Type", "ats_type": 1, "ats_slug": "example"},
        company("No Slug", "ashby"),
        {"name": "Numeric Slug", "ats_type": "lever", "ats_slug": 3},
        company("Empty Slug", "lever", ""),
        company("Blank Slug", "ashby", "   "),
        company("Slash Slug", "greenhouse", "a/b"),
        company("Query Slug", "greenhouse", "a?b"),
        company("Host Slug", "lever", "api.lever.co"),
        company("Space Slug", "greenhouse", "has space"),
        "nope",
        {"name": "  "},
        {"name": 3},
    ]
    fetcher = Scripted({})
    lines = watched(search, datadir, items, fetcher)
    assert fetcher.calls == []
    assert companies(search) == []
    assert lines == [
        "watch: Workday Example (workday): not fetched yet; skipped",
        "watch: Icims Example (icims): not a board this command fetches; skipped",
        "watch: No Type: no ats_type; skipped",
        "watch: Numeric Type: no ats_type; skipped",
        "watch: No Slug (ashby): no ats_slug; skipped",
        "watch: Numeric Slug (lever): no ats_slug; skipped",
        "watch: Empty Slug (lever): no ats_slug; skipped",
        "watch: Blank Slug (ashby): no ats_slug; skipped",
        "watch: Slash Slug (greenhouse): ats_slug is not a single path segment; skipped",
        "watch: Query Slug (greenhouse): ats_slug is not a single path segment; skipped",
        "watch: Host Slug (lever): ats_slug is not a single path segment; skipped",
        "watch: Space Slug (greenhouse): ats_slug is not a single path segment; skipped",
        "watch: a watchlist entry has no name; skipped",
        "watch: a watchlist entry has no name; skipped",
        "watch: a watchlist entry has no name; skipped",
        "watch: 0 boards fetched, 0 failed",  # never attempted: in neither count
    ]


def test_an_empty_watchlist_does_nothing(search: sqlite3.Connection, datadir: DataDir) -> None:
    assert watched(search, datadir, [], Scripted({})) == ["watch: 0 boards fetched, 0 failed"]
    assert companies(search) == []


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    return data_env


def test_watch_command_records_the_example_boards_without_a_socket(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(board: str) -> Fetched:
        calls.append(board)
        if board == url("greenhouse", "examplerobotics"):
            return fetched(one_greenhouse("examplerobotics"))
        if board == url("lever", "samplelogistics"):
            return fetched(
                [
                    {
                        "id": "abc-123",
                        "text": "Operations Coordinator",
                        "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
                        "categories": {"location": "Canada"},
                        "workplaceType": "remote",
                    }
                ]
            )
        raise AssertionError(board)

    def local_score(posting: object, config: object, **kwargs: object) -> Score:
        return Score(llm_output='{"gaps":[]}', gaps="[]", tier="strong")

    monkeypatch.setattr(cli, "fetch_url", fake)
    monkeypatch.setattr(cli, "score_posting", local_score)
    monkeypatch.setattr(cli, "config_today", lambda doc, now=None: dt.date(2026, 11, 1))
    assert cli.main(["watch"], probe=probe) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "watch: Example Robotics (greenhouse): 1 new, 0 already recorded\n"
        "watch: Sample Logistics (lever): 1 new, 0 already recorded\n"
        "watch: Example Staffing: no ats_type; skipped\n"
        "watch: 2 boards fetched, 0 failed\n"
    )
    assert calls == [url("greenhouse", "examplerobotics"), url("lever", "samplelogistics")]
    conn = db.connect(inited / "search.db")
    try:
        assert_untouched(conn, DataDir(root=inited, mount_point=inited))
        rows = postings(conn)
        assert [(row["company"], row["title"], row["first_seen"]) for row in rows] == [
            ("Example Robotics", "Operations Coordinator", DAY),
            ("Sample Logistics", "Operations Coordinator", DAY),
        ]
        for row in rows:
            assert row["source"] == "watcher"
            assert row["first_seen"] == DAY
            assert row["fit_rating"] is None
            assert row["gaps"] == "[]"
            assert row["llm_output"] == '{"gaps":[]}'
            assert row["scout_metadata"] == '{"tier": "strong"}'
            assert row["snapshot_path"] is None
    finally:
        conn.close()
    success = inited / "logs" / "watch.success"
    assert success.stat().st_mode & 0o777 == 0o600
    assert success.read_text().strip().endswith("Z")
    log = inited / "logs" / "watch.log"
    assert log.stat().st_mode & 0o777 == 0o600
    assert log.read_text() == captured.err


def test_watch_requires_config(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (inited / "config.yaml").unlink()
    assert cli.main(["watch"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: config.yaml is missing\n"
    assert not (inited / "logs" / "watch.success").exists()


def test_watch_requires_the_database(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (inited / "search.db").unlink()
    assert cli.main(["watch"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: search.db is missing; run careerhunter init\n"
    assert not (inited / "logs" / "watch.success").exists()


def test_a_watch_that_raises_does_not_stamp(
    inited: Path, probe: SystemProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("synthetic watch failure")

    monkeypatch.setattr(cli.watch, "run", boom)
    with pytest.raises(RuntimeError, match="synthetic watch failure"):
        cli.main(["watch"], probe=probe)
    assert not (inited / "logs" / "watch.success").exists()


def _candidate(
    conn: sqlite3.Connection,
    *,
    url: str,
    ats_type: str | None,
    ats_slug: str | None,
    name: str,
    kind: str = "board",
) -> int:
    cur = conn.execute(
        "INSERT INTO discovery_candidates "
        "(kind, url, ats_type, ats_slug, suggested_name, status, first_seen) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (kind, url, ats_type, ats_slug, name, DAY),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def test_watch_fetches_an_approved_discovery_board_only(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    sentinel = b"do-not-rewrite\n"
    (datadir.root / "config.yaml").write_bytes(sentinel)
    (datadir.root / "exclusions.yaml").write_bytes(sentinel)
    ei = datadir.root / "ei_record_2026-27.db"
    ei.write_bytes(b"synthetic-ei\n")
    search_store.upsert_company(
        search,
        "Sample Seed",
        ats_type="greenhouse",
        ats_slug="sampleseed",
        watchlist=1,
        source="manual",
    )
    search_store.upsert_company(
        search,
        "Old Robotics",
        ats_type="greenhouse",
        ats_slug="oldrobotics",
        watchlist=1,
        source="watcher",
    )
    search_store.upsert_company(
        search,
        "Workday Example",
        ats_type="workday",
        ats_slug="workdayexample",
        watchlist=1,
        source="discovery",
    )
    search_store.upsert_company(
        search,
        "Bad Slug",
        ats_type="greenhouse",
        ats_slug="bad/slug",
        watchlist=1,
        source="discovery",
    )
    career = _candidate(
        search,
        kind="career",
        url="https://jobs.example.com/careers",
        ats_type=None,
        ats_slug=None,
        name="jobs.example.com",
    )
    board = _candidate(
        search,
        url="https://boards.greenhouse.io/newrobotics",
        ats_type="greenhouse",
        ats_slug="newrobotics",
        name="newrobotics",
    )
    same = _candidate(
        search,
        url="https://boards.greenhouse.io/examplerobotics",
        ats_type="greenhouse",
        ats_slug="examplerobotics",
        name="Example Robotics",
    )
    discover.approve(search, career)
    discover.approve(search, board)
    discover.approve(search, same)
    fetcher = Scripted(
        {
            url("greenhouse", "examplerobotics"): fetched(one_greenhouse("examplerobotics")),
            url("greenhouse", "newrobotics"): fetched(one_greenhouse("newrobotics", ident=1002)),
        }
    )
    lines = w.run(
        search,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today=DAY,
        fetcher=fetcher,
    )
    assert fetcher.calls == [
        url("greenhouse", "examplerobotics"),
        url("greenhouse", "newrobotics"),
    ]
    assert "sampleseed" not in "\n".join(fetcher.calls)
    assert "oldrobotics" not in "\n".join(fetcher.calls)
    stored = postings(search)
    assert [row["company"] for row in stored] == ["Example Robotics", "newrobotics"]
    assert {row["source"] for row in stored} == {"watcher"}
    search.execute("UPDATE postings SET llm_output = 'synthetic' WHERE source = 'watcher'")
    other, _inserted = search_store.upsert_company(search, "Example Org", source="discovery")
    search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, llm_output) "
        "VALUES (?, 'Customer Success Specialist', ?, 'discovery', 'synthetic')",
        (other, DAY),
    )
    alerted = search_store.pending_alert_ids(search)
    watcher_ids = [
        int(row["id"])
        for row in search.execute("SELECT id FROM postings WHERE source = 'watcher' ORDER BY id")
    ]
    discovery_ids = [
        int(row["id"])
        for row in search.execute("SELECT id FROM postings WHERE source = 'discovery'")
    ]
    assert alerted == watcher_ids
    assert discovery_ids
    assert not set(discovery_ids) & set(alerted)
    career_row = search.execute(
        "SELECT watchlist, source, careers_url FROM companies WHERE name = ?",
        ("jobs.example.com",),
    ).fetchone()
    assert career_row is not None
    assert career_row["watchlist"] == 0
    assert career_row["source"] == "discovery"
    assert career_row["careers_url"] == "https://jobs.example.com/careers"
    assert search.execute("SELECT count(*) FROM applications").fetchone()[0] == 0
    assert (datadir.root / "config.yaml").read_bytes() == sentinel
    assert (datadir.root / "exclusions.yaml").read_bytes() == sentinel
    assert ei.read_bytes() == b"synthetic-ei\n"
    assert list(datadir.root.glob("ei_record_*")) == [ei]
    assert any("newrobotics" in line for line in lines)


# Fault isolation: one company's failure is rolled back and logged, and the run goes on.

NAMES = ("Example A", "Example B", "Example C")


def board(name: str) -> str:
    return url("greenhouse", name.lower().replace(" ", ""))


def entries(*names: str) -> list[object]:
    return [company(name, "greenhouse", name.lower().replace(" ", "")) for name in names]


def jobs_for(name: str, *idents: int) -> Fetched:
    slug = name.lower().replace(" ", "")
    jobs = [one_greenhouse(slug, f"Coordinator {i}", i)["jobs"][0] for i in idents]  # type: ignore[index]
    return fetched({"jobs": jobs})


def three_boards() -> Scripted:
    return Scripted({board(name): jobs_for(name, 1001) for name in NAMES})


def table(conn: sqlite3.Connection, name: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY id")]  # noqa: S608


def stored_names(conn: sqlite3.Connection) -> list[str]:
    return sorted({str(row["company"]) for row in postings(conn)})


class Crashes:
    """Records every on_crash call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Exception]] = []

    def __call__(self, who: str, exc: Exception) -> None:
        self.calls.append((who, exc))


def test_a_company_that_raises_is_logged_and_the_others_still_run(
    search: sqlite3.Connection,
) -> None:
    import http.client

    error = http.client.IncompleteRead(b"")
    fetcher = three_boards()
    fetcher.mapping[board("Example B")] = error
    crashes = Crashes()
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=fetcher, on_crash=crashes)
    assert "watch: Example B: IncompleteRead; continuing" in lines
    assert stored_names(search) == ["Example A", "Example C"]
    assert lines[-1] == "watch: 2 boards fetched, 1 failed"
    assert crashes.calls == [("Example B", error)]
    assert not search.in_transaction


def test_the_same_failure_through_the_real_fetcher_is_a_failed_fetch(
    search: sqlite3.Connection,
) -> None:
    import http.client

    from careerhunter.fetch import fetch
    from tests.fakes import ManualWatchdog
    from tests.test_fetch import FakeResponse, FakeWeb, Raise, resolver

    routes: dict[str, FakeResponse | Raise] = {}
    for name in NAMES:
        body = jobs_for(name, 1001).body
        routes[board(name)] = FakeResponse(body=body)
    routes[board("Example B")] = Raise(http.client.IncompleteRead(b""))
    web = FakeWeb(routes)
    addresses = {"boards-api.greenhouse.io": ["93.184.216.34"]}

    def real(address: str) -> Fetched:
        return fetch(
            address,
            resolver=resolver(addresses),
            connector=web.connector,
            watchdog=ManualWatchdog(),
        )

    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=real)
    assert "watch: Example B (greenhouse): FetchFailed; nothing recorded" in lines
    assert stored_names(search) == ["Example A", "Example C"]
    assert lines[-1] == "watch: 2 boards fetched, 1 failed"


def _raising_for(
    monkeypatch: pytest.MonkeyPatch, who: str, error: Exception, *, after_real: bool = False
) -> None:
    real = search_store.find_or_create_posting

    def patched(conn: sqlite3.Connection, posting: Posting, dating: object) -> object:
        if posting.company == who:
            if after_real:
                real(conn, posting, dating)  # type: ignore[arg-type]
            raise error
        return real(conn, posting, dating)  # type: ignore[arg-type]

    monkeypatch.setattr(search_store, "find_or_create_posting", patched)


def test_an_integrity_error_is_isolated_to_its_company(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raising_for(monkeypatch, "Example B", sqlite3.IntegrityError("synthetic"))
    crashes = Crashes()
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards(), on_crash=crashes)
    assert "watch: Example B: IntegrityError; continuing" in lines
    assert stored_names(search) == ["Example A", "Example C"]
    assert [who for who, _exc in crashes.calls] == ["Example B"]
    assert not search.in_transaction


def test_any_other_database_error_rolls_back_and_stops_the_run(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raising_for(monkeypatch, "Example B", sqlite3.OperationalError("database is locked"))
    fetcher = three_boards()
    crashes = Crashes()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        w.run(search, entries(*NAMES), today=DAY, fetcher=fetcher, on_crash=crashes)
    assert board("Example C") not in fetcher.calls
    assert not search.in_transaction
    assert crashes.calls == []  # an abort is not a crash the run carries on from


def test_a_crash_inside_a_posting_transaction_is_rolled_back(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raising_for(monkeypatch, "Example B", RuntimeError("synthetic"), after_real=True)
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards())
    assert "watch: Example B: RuntimeError; continuing" in lines
    assert stored_names(search) == ["Example A", "Example C"]  # B's insert was undone
    assert not search.in_transaction


def test_a_transaction_left_open_outside_the_helper_is_rolled_back(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = w.filters.decide

    def opens_and_fails(text: Any, **kwargs: Any) -> Any:
        if text.company == "Example B":
            search.execute("BEGIN IMMEDIATE")
            raise RuntimeError("synthetic")
        return real(text, **kwargs)

    monkeypatch.setattr(w.filters, "decide", opens_and_fails)
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards())
    assert "watch: Example B: RuntimeError; continuing" in lines
    # Without the rollback, C would fail: cannot start a transaction within a transaction.
    assert stored_names(search) == ["Example A", "Example C"]
    assert not search.in_transaction


def test_a_failed_board_leaves_its_company_and_postings_as_they_were(
    search: sqlite3.Connection,
) -> None:
    from careerhunter.fetch import FetchTimeout

    w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards())
    before = (table(search, "companies"), table(search, "postings"))
    fetcher = three_boards()
    fetcher.mapping[board("Example B")] = FetchTimeout("synthetic")
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=fetcher)
    assert (table(search, "companies"), table(search, "postings")) == before
    assert "watch: Example B (greenhouse): FetchTimeout; nothing recorded" in lines
    assert not any(line.startswith("watch: Example B (greenhouse): 0 new") for line in lines)
    assert lines[-1] == "watch: 2 boards fetched, 1 failed"


def test_a_crash_midway_through_a_board_keeps_every_earlier_posting(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Scripted({board("Example B"): jobs_for("Example B", 1001, 1002)})
    w.run(search, entries("Example B"), today=DAY, fetcher=first)
    run_one = table(search, "postings")
    assert len(run_one) == 2
    real = search_store.find_or_create_posting
    seen: list[str] = []

    def second_new_job_fails(conn: sqlite3.Connection, posting: Posting, dating: Any) -> Any:
        seen.append(posting.title)
        if posting.title == "Coordinator 1004":
            raise RuntimeError("synthetic")
        return real(conn, posting, dating)

    monkeypatch.setattr(search_store, "find_or_create_posting", second_new_job_fails)
    again = Scripted({board("Example B"): jobs_for("Example B", 1001, 1002, 1003, 1004, 1005)})
    lines = w.run(search, entries("Example B"), today=DAY, fetcher=again)
    assert "watch: Example B: RuntimeError; continuing" in lines
    assert seen[-1] == "Coordinator 1004"  # stopped there; 1005 was never reached
    rows = table(search, "postings")
    assert rows[:2] == run_one  # every run-1 posting is byte for byte as it was
    assert [row["title"] for row in postings(search)] == [
        "Coordinator 1001",
        "Coordinator 1002",
        "Coordinator 1003",
    ]


def test_a_crash_inside_align_rolls_back_all_of_its_changes(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with db.transaction(search):
        search.execute(
            "INSERT INTO companies (name, watchlist, source) VALUES ('Example B', 0, 'manual')"
        )
    before = table(search, "companies")
    real = w._align

    def align_then_fail(conn: sqlite3.Connection, *args: Any) -> Any:
        real(conn, *args)  # fills ats_type, ats_slug and watchlist
        raise RuntimeError("synthetic")

    monkeypatch.setattr(w, "_align", align_then_fail)
    lines = w.run(search, entries("Example B"), today=DAY, fetcher=three_boards())
    assert "watch: Example B: RuntimeError; continuing" in lines
    assert table(search, "companies") == before
    assert postings(search) == []
    assert not search.in_transaction


def _unscored(search: sqlite3.Connection) -> None:
    """Run 1 stores one unscored posting for A and for B."""
    w.run(search, entries("Example A", "Example B"), today=DAY, fetcher=three_boards())
    assert len(postings(search)) == 2


def test_a_recheck_row_that_raises_is_isolated(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unscored(search)
    error = RuntimeError("synthetic")
    real = w.filters.decide

    def fails_for_b(text: Any, **kwargs: Any) -> Any:
        if text.company == "Example B":
            raise error
        return real(text, **kwargs)

    monkeypatch.setattr(w.filters, "decide", fails_for_b)
    crashes = Crashes()
    lines = w.run(
        search,
        [],
        today=DAY,
        fetcher=Scripted({}),
        exclusions={"companies": ["Example A"]},
        on_crash=crashes,
    )
    assert "watch: Example B: RuntimeError; continuing" in lines
    skipped = search.execute(
        "SELECT c.name FROM skips s JOIN postings p ON p.id = s.posting_id "
        "JOIN companies c ON c.id = p.company_id"
    ).fetchall()
    assert [row["name"] for row in skipped] == ["Example A"]  # A was still rechecked
    assert crashes.calls == [("Example B", error)]


def test_a_scoring_row_that_raises_is_isolated(search: sqlite3.Connection) -> None:
    _unscored(search)

    def scorer(text: Any) -> Score | None:
        if text.company == "Example B":
            raise RuntimeError("synthetic")
        return Score(llm_output="kept", gaps="[]", tier="strong")

    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), scorer=scorer)
    assert "watch: Example B: RuntimeError; continuing" in lines
    scored = {row["company"]: row["llm_output"] for row in postings(search)}
    assert scored == {"Example A": "kept", "Example B": None}


def test_a_refresh_that_raises_is_isolated_and_the_boards_still_run(
    search: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = RuntimeError("synthetic")

    def refresh(self: object) -> list[str]:
        raise error

    monkeypatch.setattr(w.Embedder, "refresh", refresh)
    crashes = Crashes()
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards(), on_crash=crashes)
    assert lines[0] == "watch: embedding refresh: RuntimeError; continuing"
    assert stored_names(search) == list(NAMES)
    assert crashes.calls == [("embedding refresh", error)]


def test_a_keyboard_interrupt_is_not_caught(search: sqlite3.Connection) -> None:
    def interrupted(address: str) -> Fetched:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        w.run(search, entries("Example A"), today=DAY, fetcher=interrupted)
