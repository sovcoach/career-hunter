"""Local scoring. A fake transport stands in for the model; no socket is opened."""

from __future__ import annotations

import http.client
import json
import sqlite3
from typing import Any

import pytest

from careerhunter import watch
from careerhunter.fetch import Fetched
from careerhunter.filters import PostingText
from careerhunter.score import (
    DEFAULT_URL,
    Score,
    ScoreError,
    default_transport,
    require_loopback,
    score_posting,
)
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, postings, url

RUBRIC = {
    "fit_rubric": {
        "gap_tiers": {
            "tiers": [
                {"name": "strong", "max_gaps": 1},
                {"name": "possible", "max_gaps": 2},
            ],
            "exclude_from_gaps": 3,
            "boundary": {"at_gaps": 1, "all_minor": "strong", "any_core": "possible"},
            "exempt_requirements": ["degree"],
        }
    },
    "search": {
        "lanes": [{"name": "operations", "priority": 1, "titles": ["Operations Coordinator"]}],
        "location_rules": {"remote": ["Canada"]},
    },
    "hard_gates": {"seniority_words": ["Senior"], "min_salary": None},
    "notes": "DO-NOT-LEAK-NOTES",
    "telegram": {"chat_id": 424242},
    "fit_weights_sentinel": "DO-NOT-LEAK-WEIGHTS",
}


def posting() -> PostingText:
    return PostingText(
        "Example Robotics",
        "Operations Coordinator",
        "Example City",
        work_model="remote",
        description="A synthetic posting.",
    )


def reply(content: str, status: int = 200) -> tuple[int, bytes]:
    body = {"choices": [{"message": {"content": content}}]}
    return status, json.dumps(body).encode()


def test_only_a_loopback_http_url_is_accepted() -> None:
    host, port, path = require_loopback(DEFAULT_URL)
    assert (host, port, path) == ("127.0.0.1", 1234, "/v1/chat/completions")
    assert require_loopback("http://localhost/v1")[0] == "localhost"
    assert require_loopback("http://[::1]:9/v1")[:2] == ("::1", 9)
    for bad in (
        "https://127.0.0.1/v1",
        "http://example.com/v1",
        "http://10.0.0.1/v1",
        "http://user:secret@127.0.0.1/v1",
    ):
        with pytest.raises(ScoreError):
            require_loopback(bad)


def test_the_prompt_uses_only_the_rubric_and_the_posting() -> None:
    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["url"] = url.encode()
        seen["body"] = payload
        return reply('{"gaps":[]}')

    result = score_posting(posting(), RUBRIC, transport=transport)
    assert result is not None and result.tier == "strong" and result.gaps == "[]"
    assert result.excluded is False
    text = seen["body"].decode()
    assert seen["url"] == DEFAULT_URL.encode()
    assert "operations" in text and "Canada" in text and "Senior" in text
    assert "A synthetic posting." in text
    assert "DO-NOT-LEAK-NOTES" not in text
    assert "DO-NOT-LEAK-WEIGHTS" not in text
    assert "424242" not in text
    assert "local-model" in text


def test_gap_tiers_name_the_tier_and_a_high_count_skips() -> None:
    def run(content: str) -> Score | None:
        return score_posting(posting(), RUBRIC, transport=lambda url, body: reply(content))

    empty = run('{"gaps":[]}')
    assert empty is not None and empty.tier == "strong" and empty.excluded is False
    minor = run('{"gaps":[{"name":"spreadsheets","kind":"minor","type":"skill"}]}')
    assert minor is not None and minor.tier == "strong"
    assert json.loads(minor.gaps) == [{"name": "spreadsheets", "kind": "minor", "type": "skill"}]
    core = run('```json\n{"gaps":[{"name":"French","kind":"core","type":"language"}]}\n```')
    assert core is not None and core.tier == "possible"
    two = run('{"gaps":[{"name":"French","kind":"core"},{"name":"Excel","kind":"minor"}]}')
    assert two is not None and two.tier == "possible"
    exempt = run(
        '{"gaps":[{"name":"a degree","kind":"core","type":"degree"},'
        '{"name":"Excel","kind":"minor","type":"skill"}]}'
    )
    assert exempt is not None and exempt.tier == "strong"
    assert json.loads(exempt.gaps) == [{"name": "Excel", "kind": "minor", "type": "skill"}]
    over = run(
        '{"gaps":[{"name":"a","kind":"core"},{"name":"b","kind":"core"},{"name":"c","kind":"minor"}]}'
    )
    assert over is not None and over.excluded and over.tier is None
    assert score_posting(posting(), RUBRIC, transport=lambda url, body: reply("nope")) is None
    assert score_posting(posting(), RUBRIC, transport=lambda url, body: reply("{}", 302)) is None

    def down(url: str, body: bytes) -> tuple[int, bytes]:
        raise OSError("synthetic")

    assert score_posting(posting(), RUBRIC, transport=down) is None


def test_a_non_loopback_url_is_not_requested() -> None:
    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        raise AssertionError("transport called")

    missed = score_posting(posting(), RUBRIC, url="http://example.com/v1", transport=transport)
    assert missed is None
    assert score_posting(posting(), {"fit_rubric": {}}, transport=transport) is None


def test_the_default_transport_posts_once_and_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []

    class Response:
        status = 302

        def read(self, amount: int) -> bytes:
            return b"go away"

    class Conn:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls.append(("open", host, port))

        def request(
            self,
            method: str,
            path: str,
            body: bytes | None = None,
            headers: Any = None,
        ) -> None:
            calls.append((method, path, len(body or b"")))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            calls.append(("close", "", 0))

    monkeypatch.setattr(http.client, "HTTPConnection", Conn)
    status, body = default_transport(DEFAULT_URL, b"{}")
    assert status == 302 and body == b"go away"
    assert calls == [
        ("open", "127.0.0.1", 1234),
        ("POST", "/v1/chat/completions", 2),
        ("close", "", 0),
    ]


def test_a_refused_connection_stays_a_score_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Conn:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            pass

        def request(self, *args: object, **kwargs: object) -> None:
            raise ConnectionRefusedError("synthetic")

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", Conn)
    with pytest.raises(ScoreError):
        default_transport(DEFAULT_URL, b"{}")


def test_watch_scores_a_kept_posting_and_leaves_a_failure_unscored(
    search: sqlite3.Connection,
) -> None:
    board = url("greenhouse", "examplerobotics")
    item = company("Example Robotics", "greenhouse", "examplerobotics")
    payload = one_greenhouse("examplerobotics")

    def good(text: PostingText) -> Score:
        assert text.description is None  # Greenhouse list rows have no description
        return Score(llm_output="model text", gaps="[]", tier="strong")

    lines = watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config=RUBRIC,
        scorer=good,
    )
    assert "left unscored" not in "\n".join(lines)
    row = postings(search)[0]
    assert row["fit_rating"] is None
    assert row["gaps"] == "[]"
    assert row["llm_output"] == "model text"
    assert row["scout_metadata"] == '{"tier": "strong"}'
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0

    other = url("lever", "samplelogistics")
    second = company("Sample Logistics", "lever", "samplelogistics")

    def mixed(text: PostingText) -> Score | None:
        if text.company == "Sample Logistics":
            raise OSError("synthetic")
        return Score(llm_output="kept", gaps="[]", tier="possible")

    later = watch.run(
        search,
        [item, second],
        today=DAY,
        fetcher=Scripted(
            {
                board: fetched(payload),
                other: fetched(
                    [
                        {
                            "id": "abc-123",
                            "text": "Operations Coordinator",
                            "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
                            "categories": {"location": "Canada"},
                            "workplaceType": "remote",
                        }
                    ]
                ),
            }
        ),
        config=RUBRIC,
        scorer=mixed,
    )
    rows = {row["company"]: row for row in postings(search)}
    # The first posting was already scored, so this run does not score it again.
    assert rows["Example Robotics"]["llm_output"] == "model text"
    assert rows["Sample Logistics"]["llm_output"] is None
    assert rows["Sample Logistics"]["fit_rating"] is None
    assert any("left unscored" in line for line in later)


def test_a_later_run_scores_waiting_rows_before_the_fetch(search: sqlite3.Connection) -> None:
    board = url("ashby", "exampleorg")
    item = company("Example Org", "ashby", "exampleorg")
    payload = {
        "jobs": [
            {
                "id": "job-1",
                "title": "Operations Coordinator",
                "jobUrl": "https://jobs.ashbyhq.com/exampleorg/job-1",
                "location": "Canada",
                "workplaceType": "Remote",
                "descriptionPlain": "Needs French.",
            }
        ]
    }
    seen: list[str | None] = []

    def first(text: PostingText) -> None:
        seen.append(text.description)
        return None

    watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        scorer=first,  # type: ignore[arg-type]
    )
    assert seen == ["Needs French."]
    assert postings(search)[0]["llm_output"] is None
    order: list[str] = []

    def second(text: PostingText) -> Score:
        order.append("score")
        assert text.description is None
        return Score(
            llm_output="raw",
            gaps='[{"name":"a","kind":"core"},{"name":"b","kind":"core"},{"name":"c","kind":"minor"}]',
            tier=None,
            excluded=True,
        )

    def fetcher(address: str) -> Fetched:
        order.append("fetch")
        return fetched(payload)

    lines = watch.run(search, [item], today=DAY, fetcher=fetcher, scorer=second)
    assert order[0] == "score"
    assert "fetch" in order
    assert any("skipped (gap_tier)" in line for line in lines)
    row = postings(search)[0]
    assert row["llm_output"] == "raw"
    assert row["scout_metadata"] is None
    assert row["fit_rating"] is None
    assert search.execute("SELECT reason FROM skips").fetchone()["reason"] == "gap_tier"
