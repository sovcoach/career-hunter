"""Scoring timeouts: counted in a row per posting, set aside after a limit, and released."""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from careerhunter import search_store
from careerhunter import watch as w
from careerhunter.filters import PostingText
from careerhunter.score import TIMED_OUT, UNREACHED, Score, ScoreError
from tests.fakes import FakeClock
from tests.test_watch import DAY, NAMES, OnBoard, entries, minutes, three_boards


def _posting(search: sqlite3.Connection, title: str = "Analyst") -> int:
    company = search.execute(
        "INSERT INTO companies (name, source) VALUES (?, 'manual')", (f"Example {title}",)
    ).lastrowid
    cur = search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source) "
        "VALUES (?, ?, '2026-09-01', 'watcher')",
        (company, title),
    )
    return int(cur.lastrowid or 0)


def _rows(search: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = search.execute(
        "SELECT posting_id, timeouts, last_timed_out_at, set_aside_at FROM score_timeouts "
        "ORDER BY posting_id"
    )
    return [tuple(r) for r in rows]


def test_the_second_timeout_in_a_row_sets_the_posting_aside(search: sqlite3.Connection) -> None:
    one = _posting(search)
    first = search_store.record_timeout(search, one, "2026-09-01T10:00:00Z", set_aside_after=2)
    assert not first
    assert _rows(search) == [(one, 1, "2026-09-01T10:00:00Z", None)]
    second = search_store.record_timeout(search, one, "2026-09-01T11:00:00Z", set_aside_after=2)
    assert second
    assert _rows(search) == [(one, 2, "2026-09-01T11:00:00Z", "2026-09-01T11:00:00Z")]
    assert search_store.set_aside_count(search) == 1
    # Already set aside: a later count does not say so again or move the stamp.
    again = search_store.record_timeout(search, one, "2026-09-01T12:00:00Z", set_aside_after=2)
    assert not again
    assert _rows(search)[0][3] == "2026-09-01T11:00:00Z"


def test_an_answered_call_clears_the_count(search: sqlite3.Connection) -> None:
    one = _posting(search)
    search_store.record_timeout(search, one, "2026-09-01T10:00:00Z", set_aside_after=2)
    search_store.clear_timeouts(search, one)
    assert _rows(search) == []
    assert not search_store.record_timeout(search, one, "2026-09-01T11:00:00Z", set_aside_after=2)


def test_releasing_clears_set_aside_postings_and_counts_only_those(
    search: sqlite3.Connection,
) -> None:
    one, two, three = _posting(search, "A"), _posting(search, "B"), _posting(search, "C")
    for _ in range(2):
        search_store.record_timeout(search, one, "2026-09-01T10:00:00Z", set_aside_after=2)
    search_store.record_timeout(search, two, "2026-09-01T10:00:00Z", set_aside_after=2)
    listed = search_store.set_aside_postings(search)
    assert [(int(r["id"]), r["company"], r["title"]) for r in listed] == [(one, "Example A", "A")]
    assert search_store.release_set_aside(search, [one, two, three]) == 1
    assert _rows(search) == []
    assert search_store.set_aside_count(search) == 0


def test_a_set_aside_unscored_posting_is_not_held(search: sqlite3.Connection) -> None:
    one = _posting(search)
    assert search_store.held_counts(search).unscored == 1
    search_store.record_timeout(search, one, "2026-09-01T10:00:00Z", set_aside_after=2)
    assert search_store.held_counts(search).unscored == 1
    search_store.record_timeout(search, one, "2026-09-01T11:00:00Z", set_aside_after=2)
    assert search_store.held_counts(search).unscored == 0


def test_score_call_stats_count_timeouts_apart(search: sqlite3.Connection) -> None:
    one = _posting(search)
    search.execute("INSERT INTO watch_runs (started_at) VALUES ('2026-09-01T10:00:00Z')")
    for outcome, ms in (("scored", 120000), ("timeout", 300006), ("timeout", 300010)):
        search_store.record_score_call(
            search, 1, one, "2026-09-01T10:00:00Z", "2026-09-01T10:05:00Z", ms, outcome
        )
    search_store.record_score_call(
        search, 1, one, "2026-09-01T10:00:00Z", "2026-09-01T10:00:01Z", 3, "unreachable"
    )
    stats = search_store.score_call_stats(search, "2026-09-01T00:00:00Z")
    assert stats == search_store.ScoreCallStats(
        (120000,), not_answered=1, timed_out=2, longest_draw_ms=120000
    )


def test_a_timeout_is_a_health_failure_kind(search: sqlite3.Connection) -> None:
    search_store.record_component(
        search, "lm_studio", failed="timeout", stamp="2026-09-01T10:00:00Z"
    )
    (health,) = [h for h in search_store.component_health(search) if h.component == "lm_studio"]
    assert (health.failures, health.last_failure) == (1, "timeout")


# The watcher: a timeout is about one posting. The run carries on, the posting goes to
# the back, it is set aside after TIMEOUTS_TO_SET_ASIDE in a row, and a run whose calls
# all timed out is an lm_studio failure.

LATER = dt.datetime(2099, 1, 1, 12, 0, tzinfo=dt.UTC)  # after every created_at


def _timeout() -> ScoreError:
    return ScoreError(TIMED_OUT, kind="timeout", type_name="DeadlineExceeded")


def _scorer(plan: dict[str, Any], calls: list[str]) -> Any:
    """Scores each title as planned: an exception is raised, anything else is scored."""

    def scorer(text: PostingText) -> Score | None:
        calls.append(text.company)
        outcome = plan.get(text.company)
        if isinstance(outcome, BaseException):
            raise outcome
        return Score(llm_output='{"gaps":[]}', gaps="[]", tier="high")

    return scorer


def _watch(
    search: sqlite3.Connection, plan: dict[str, Any], **kw: Any
) -> tuple[list[str], list[str]]:
    calls: list[str] = []
    options: dict[str, Any] = {
        "today": DAY,
        "fetcher": OnBoard(three_boards().mapping),
        "scorer": _scorer(plan, calls),
        "config": {"scoring": {"timeout_seconds": 300}},
        "now": lambda: LATER,
    }
    options.update(kw)
    lines = w.run(search, entries(*NAMES), **options)
    return calls, lines


def _stored(search: sqlite3.Connection) -> None:
    w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards())  # three unscored rows


def _outcomes(search: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in search.execute("SELECT outcome FROM score_calls ORDER BY id")]


def _lm_studio(search: sqlite3.Connection) -> tuple[int, str | None]:
    (health,) = [h for h in search_store.component_health(search) if h.component == "lm_studio"]
    return health.failures, health.last_failure


def test_a_timeout_does_not_stop_the_run(search: sqlite3.Connection) -> None:
    _stored(search)
    calls, lines = _watch(search, {"Example A": _timeout()})
    assert calls == list(NAMES)
    assert _outcomes(search) == ["timeout", "scored", "scored"]
    assert (
        "watch: Example A: 'Coordinator 1001' left unscored (model took longer than 300 s: "
        "DeadlineExceeded); moved to the back of the queue"
    ) in lines
    assert not any("postings left for the next run" in line for line in lines)
    # One call timed out and others were answered: no lm_studio failure.
    assert _lm_studio(search) == (0, None)


def test_the_posting_that_timed_out_goes_to_the_back(search: sqlite3.Connection) -> None:
    _stored(search)
    refused = ScoreError(UNREACHED, type_name="ConnectionRefusedError")
    calls, lines = _watch(search, {"Example A": _timeout(), "Example B": refused})
    assert calls == ["Example A", "Example B"]  # the refused connection stops the run
    assert any("(model unreachable: ConnectionRefusedError)" in line for line in lines)
    stamps = dict(
        search.execute(
            "SELECT c.name, p.score_attempted_at FROM postings p "
            "JOIN companies c ON c.id = p.company_id"
        ).fetchall()
    )
    # The timeout keeps this attempt's stamp; the refused one gets its old one back.
    assert stamps == {"Example A": "2099-01-01T12:00:00Z", "Example B": None, "Example C": None}
    calls, _lines = _watch(search, {})
    assert calls == ["Example B", "Example C", "Example A"]


def test_two_timeouts_in_a_row_set_the_posting_aside(search: sqlite3.Connection) -> None:
    _stored(search)
    assert w.TIMEOUTS_TO_SET_ASIDE == 2
    _watch(search, {"Example A": _timeout()})
    calls, lines = _watch(search, {"Example A": _timeout()})
    assert calls == ["Example A"]  # B and C were scored in the first run
    assert any(
        line.endswith(
            "; set aside after 2 timeouts in a row, not tried again until "
            "careerhunter rescore --set-aside"
        )
        for line in lines
    )
    assert search_store.set_aside_count(search) == 1
    calls, _lines = _watch(search, {})
    assert calls == []  # set aside: not sent again
    assert search_store.held_counts(search).unscored == 0
    (posting_id,) = [int(r["id"]) for r in search_store.set_aside_postings(search)]
    search_store.release_set_aside(search, [posting_id])
    calls, _lines = _watch(search, {})
    assert calls == ["Example A"]
    assert search.execute("SELECT count(*) FROM score_timeouts").fetchone()[0] == 0


def test_an_answered_call_between_timeouts_clears_the_count(search: sqlite3.Connection) -> None:
    _stored(search)
    _watch(search, {"Example A": _timeout()})
    _watch(search, {"Example A": ScoreError(UNREACHED)})  # not answered: no change
    rows = search.execute("SELECT timeouts, set_aside_at FROM score_timeouts").fetchall()
    assert [tuple(r) for r in rows] == [(1, None)]
    _watch(search, {})  # answered
    assert search.execute("SELECT count(*) FROM score_timeouts").fetchone()[0] == 0


def test_a_run_whose_calls_all_timed_out_is_an_lm_studio_failure(
    search: sqlite3.Connection,
) -> None:
    _stored(search)
    _watch(search, dict.fromkeys(NAMES, _timeout()))
    assert _outcomes(search) == ["timeout"] * 3
    assert _lm_studio(search) == (1, "timeout")


def test_a_single_call_that_timed_out_records_no_health(search: sqlite3.Connection) -> None:
    w.run(search, entries("Example A"), today=DAY, fetcher=three_boards())
    calls: list[str] = []
    w.run(
        search,
        entries("Example A"),
        today=DAY,
        fetcher=OnBoard(three_boards().mapping),
        scorer=_scorer({"Example A": _timeout()}, calls),
        now=lambda: LATER,
    )
    assert calls == ["Example A"]
    assert _lm_studio(search) == (0, None)


def test_a_timeout_after_the_budget_ran_out_is_a_budget_cut(search: sqlite3.Connection) -> None:
    _stored(search)
    clock = FakeClock()

    def scorer(text: PostingText) -> Score | None:
        clock.advance(601)
        raise _timeout()

    lines = w.run(
        search,
        entries(*NAMES),
        today=DAY,
        fetcher=OnBoard(three_boards().mapping),
        scorer=scorer,
        budget=minutes(clock, 10),
        config={"scoring": {"timeout_seconds": 60}},
        now=lambda: LATER,
    )
    assert _outcomes(search) == ["budget"]
    assert any("left for the next run (run budget reached)" in line for line in lines)
    assert search.execute("SELECT count(*) FROM score_timeouts").fetchone()[0] == 0
