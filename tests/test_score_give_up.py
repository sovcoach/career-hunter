"""A posting whose model replies don't parse is given up after PARSE_FAILURES_TO_GIVE_UP."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, db, search_store
from careerhunter import watch as w
from careerhunter.filters import PostingText
from careerhunter.score import Score, score_posting
from tests.test_score import RUBRIC, reply
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, postings, url

BOARD = url("greenhouse", "examplerobotics")
ENTRY = company("Example Robotics", "greenhouse", "examplerobotics")


def _run(search: sqlite3.Connection, content: str) -> list[str]:
    calls: list[str] = []

    def scorer(text: PostingText) -> Score | None:
        calls.append(text.title)
        return score_posting(text, RUBRIC, transport=lambda _a, _p: reply(content))

    lines = w.run(
        search,
        [ENTRY],
        today=DAY,
        fetcher=Scripted({BOARD: fetched(one_greenhouse("examplerobotics"))}),
        config=RUBRIC,
        scorer=scorer,
    )
    return [f"called {len(calls)}", *lines]


def _failures(search: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows = search.execute("SELECT failures, given_up_at IS NOT NULL FROM score_parse_failures")
    return [tuple(r) for r in rows]


def test_three_unparseable_replies_give_the_posting_up(search: sqlite3.Connection) -> None:
    assert w.PARSE_FAILURES_TO_GIVE_UP == 3
    cut_off = "<think>still thinking"  # a reasoning model that never closed its thinking
    for count in (1, 2):
        lines = _run(search, cut_off)
        assert lines[0] == "called 1"
        assert any("left unscored (unparseable reply)" in line for line in lines)
        assert _failures(search) == [(count, 0)]
        assert search_store.held_counts(search).unscored == 1
    lines = _run(search, cut_off)
    assert any("given up after 3 unparseable replies, not tried again" in line for line in lines)
    assert _failures(search) == [(3, 1)]
    assert search_store.parse_given_up_count(search) == 1
    assert search_store.held_counts(search).unscored == 0  # not held: given up

    lines = _run(search, '{"gaps":[]}')  # a good reply now: the posting is not sent again
    assert lines[0] == "called 0"
    assert postings(search)[0]["llm_output"] is None


def test_a_reply_that_parses_clears_the_count(search: sqlite3.Connection) -> None:
    _run(search, "no json at all")
    _run(search, "no json at all")
    assert _failures(search) == [(2, 0)]
    _run(search, '<think>fine</think>{"gaps":[]}')
    assert _failures(search) == []
    assert postings(search)[0]["llm_output"] == '{"gaps":[]}'


def test_a_model_that_does_not_answer_is_not_a_parse_failure(search: sqlite3.Connection) -> None:
    def down(text: PostingText) -> Score | None:
        return score_posting(text, RUBRIC, transport=lambda _a, _p: reply("{}", status=500))

    for _ in range(4):
        w.run(
            search,
            [ENTRY],
            today=DAY,
            fetcher=Scripted({BOARD: fetched(one_greenhouse("examplerobotics"))}),
            config=RUBRIC,
            scorer=down,
        )
    assert _failures(search) == []


def test_doctor_counts_given_up_postings_under_bot_refusals(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(3):
        _run(search, "not json")
    cli._doctor_refusals(cli._Report(), search)
    assert capsys.readouterr().out == (
        "bot refusals    WARN 1 posting got a model reply that did not parse 3 times; "
        "given up and never scored (see logs/watch.log)\n"
    )


def test_migration_0023_starts_empty(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search", [m for m in db.load_migrations("search") if m.version <= 22])
        db.migrate(conn, "search")
        assert conn.execute("SELECT count(*) FROM score_parse_failures").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO score_parse_failures (posting_id, failures, last_failed_at) "
                "VALUES (1, 0, '2026-11-01T00:00:00Z')"
            )
    finally:
        conn.close()
