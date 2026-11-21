"""A posting whose board page keeps failing to re-read is set aside after
REREADS_TO_SET_ASIDE tries (Batch R, search migration 0036), not retried forever and
not counted as gone, until `careerhunter rescore --set-aside`. Synthetic only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careerhunter import cli, db, search_store
from careerhunter import watch as w
from careerhunter.ats import BoardParseError
from careerhunter.fetch import FetchError
from tests.conftest import FakeProbe
from tests.test_fit import FITS, _waiting
from tests.test_gate_before_scoring import _lever, _single, _skips
from tests.test_rescore import Recorder, titles
from tests.test_watch import DAY, Scripted, fetched


def _failures(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT posting_id, failures, reason, set_aside_at IS NOT NULL FROM reread_failures"
        )
    ]


def _run(conn: sqlite3.Connection, answer: object) -> tuple[list[str], Recorder, Scripted]:
    fetcher = Scripted({_single(conn, "Coordinator"): answer})  # type: ignore[dict-item]
    scorer = Recorder()
    lines = w.run(conn, [], today=DAY, fetcher=fetcher, scorer=scorer, config=FITS)
    return lines, scorer, fetcher


def test_rereads_that_keep_failing_are_set_aside_after_two(search: sqlite3.Connection) -> None:
    assert w.REREADS_TO_SET_ASIDE == 2
    posting = _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    lines, _, _ = _run(search, fetched({}, status=403))
    assert (
        "watch: Examplea (lever): 'Coordinator': HTTP 403; "
        "description not read; left for the next run"
    ) in lines
    assert _failures(search) == [(posting, 1, "http-403", 0)]
    lines, scorer, _ = _run(search, fetched({}, status=403))
    assert scorer.calls == []
    assert (
        "watch: Examplea (lever): 'Coordinator': HTTP 403; set aside after 2 failed "
        "re-reads (http-403), not counted as gone, until careerhunter rescore --set-aside"
    ) in lines
    assert _failures(search) == [(posting, 2, "http-403", 1)]
    assert _skips(search) == {}  # never counted as gone
    assert search_store.held_counts(search).unscored == 0  # not waiting either
    _, scorer, fetcher = _run(search, fetched({}, status=403))
    assert fetcher.calls == []  # not read again
    assert scorer.calls == []


def test_a_reread_that_works_clears_the_count(search: sqlite3.Connection) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    _run(search, fetched({}, status=503))
    assert [row[2] for row in _failures(search)] == ["http-5xx"]
    _, scorer, _ = _run(search, _lever("Plan synthetic deliveries."))
    assert titles(scorer) == ["Coordinator"]
    assert _failures(search) == []


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (FetchError("synthetic"), "fetch-error"),
        (fetched({}, status=401), "http-4xx"),
        (fetched(body=b"<html>"), "parse"),
    ],
)
def test_each_failure_has_its_reason(
    search: sqlite3.Connection, answer: object, reason: str
) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    _run(search, answer)
    assert [row[2] for row in _failures(search)] == [reason]


def test_gone_and_throttled_are_not_failures(search: sqlite3.Connection) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    _run(search, fetched({}, status=429))
    assert _failures(search) == []
    _run(search, fetched({}, status=404))
    assert _failures(search) == []
    assert _skips(search) == {"Coordinator": "gone"}


def test_a_parse_error_from_the_board_is_counted(search: sqlite3.Connection) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    _run(search, BoardParseError("synthetic"))
    assert [row[2] for row in _failures(search)] == ["parse"]


def test_doctor_names_them_and_rescore_set_aside_releases_them(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    posting = _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    for _ in range(2):
        _run(search, fetched({}, status=403))
    capsys.readouterr()
    cli._doctor_rereads(cli._Report(), search)
    assert capsys.readouterr().out.strip() == (
        "re-reads        WARN 1 posting set aside after failed re-reads (HTTP 403); "
        "retry: careerhunter rescore --set-aside"
    )
    (row,) = search_store.set_aside_postings(search)
    assert (int(row["id"]), row["aside"]) == (posting, "http-403")
    assert search_store.release_set_aside(search, [posting]) == 1
    assert _failures(search) == []
    cli._doctor_rereads(cli._Report(), search)
    assert capsys.readouterr().out == ""
    _, scorer, _ = _run(search, _lever("Plan synthetic deliveries."))
    assert titles(scorer) == ["Coordinator"]


def test_the_rescore_command_lists_why_a_posting_was_set_aside(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    conn = db.connect(data_env / "search.db")
    try:
        posting = _waiting(conn, "Coordinator", "2026-11-01T07:00:00Z")
        for _ in range(2):
            _run(conn, fetched({}, status=403))
    finally:
        conn.close()
    capsys.readouterr()
    assert cli.main(["rescore", "--set-aside", "--dry-run"], probe=probe) == 0
    out = capsys.readouterr().out
    assert f"{posting:5} Examplea — Coordinator: set aside after failed re-reads (http-403)" in out
    assert "1 set-aside posting would be released." in out
