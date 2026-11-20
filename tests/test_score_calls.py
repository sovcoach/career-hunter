"""Each scoring call's end and duration in score_calls, and doctor's line about them.

Synthetic boards and a fake model only; no network.
"""

from __future__ import annotations

import copy
import datetime as dt
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from careerhunter import cli, search_store, watch
from careerhunter.deadline import Deadline
from careerhunter.filters import PostingText
from careerhunter.score import Score, ScoreError
from tests.test_score import RUBRIC
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

START = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.UTC)


class Ticks:
    """A monotonic clock that a scorer moves forward by hand."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _run(
    search: sqlite3.Connection,
    scorer: Callable[[PostingText], Score | None],
    ticks: Ticks,
    *,
    budget: Deadline | None = None,
) -> list[str]:
    board = url("greenhouse", "examplerobotics")
    return watch.run(
        search,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today=DAY,
        fetcher=Scripted({board: fetched(one_greenhouse("examplerobotics"))}),
        config=copy.deepcopy(RUBRIC),
        scorer=scorer,
        now=lambda: START,
        monotonic=ticks,
        budget=budget,
    )


def _calls(search: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in search.execute(
            "SELECT run_id, posting_id, started_at, finished_at, duration_ms, outcome "
            "FROM score_calls ORDER BY id"
        )
    ]


def _taking(seconds: float, ticks: Ticks, then: Callable[[], Score | None]) -> Any:
    def scorer(posting: PostingText) -> Score | None:
        ticks.value += seconds
        return then()

    return scorer


def _raise(error: Exception) -> Callable[[], Score | None]:
    def go() -> Score | None:
        raise error

    return go


def test_a_scored_call_records_its_end_and_duration(search: sqlite3.Connection) -> None:
    ticks = Ticks()
    kept = Score(llm_output="model text", gaps="[]", tier="high")
    _run(search, _taking(118.25, ticks, lambda: kept), ticks)
    [call] = _calls(search)
    run_id = search.execute("SELECT max(id) FROM watch_runs").fetchone()[0]
    posting_id = search.execute("SELECT id FROM postings").fetchone()[0]
    assert call == {
        "run_id": run_id,
        "posting_id": posting_id,
        "started_at": "2026-11-01T12:00:00Z",
        "finished_at": "2026-11-01T12:00:00Z",
        "duration_ms": 118250,
        "outcome": "scored",
    }


@pytest.mark.parametrize(
    ("then", "outcome"),
    [
        (lambda: None, "unparseable"),
        (_raise(ScoreError("synthetic")), "unreachable"),
        (_raise(ScoreError("synthetic", kind="unauthorized")), "unauthorized"),
        (_raise(OSError("synthetic")), "unreachable"),
    ],
)
def test_every_outcome_is_recorded(
    search: sqlite3.Connection, then: Callable[[], Score | None], outcome: str
) -> None:
    ticks = Ticks()
    _run(search, _taking(7, ticks, then), ticks)
    assert [(c["outcome"], c["duration_ms"]) for c in _calls(search)] == [(outcome, 7000)]


def test_a_call_cut_off_by_the_budget_is_marked_budget(search: sqlite3.Connection) -> None:
    ticks = Ticks()
    budget = Deadline(600, clock=ticks)
    _run(search, _taking(601, ticks, _raise(ScoreError("synthetic"))), ticks, budget=budget)
    assert [(c["outcome"], c["duration_ms"]) for c in _calls(search)] == [("budget", 601000)]


def test_no_text_is_stored_with_a_call(search: sqlite3.Connection) -> None:
    columns = [r[1] for r in search.execute("PRAGMA table_info(score_calls)")]
    assert columns == [
        "id",
        "run_id",
        "posting_id",
        "started_at",
        "finished_at",
        "duration_ms",
        "outcome",
    ]
    with pytest.raises(sqlite3.IntegrityError):
        search.execute(
            "INSERT INTO score_calls (run_id, posting_id, started_at, finished_at, duration_ms, "
            "outcome) VALUES (1, 1, 'x', 'y', -1, 'other')"
        )


# --- doctor ------------------------------------------------------------------------------


def _seed(search: sqlite3.Connection, calls: list[tuple[str, int, str]]) -> None:
    """(started_at, duration_ms, outcome) rows, against one synthetic run and posting."""
    _run(search, _taking(1, Ticks(), lambda: None), Ticks())  # a run and a posting exist
    search.execute("DELETE FROM score_calls")
    for started, ms, outcome in calls:
        search.execute(
            "INSERT INTO score_calls (run_id, posting_id, started_at, finished_at, duration_ms, "
            "outcome) VALUES ((SELECT max(id) FROM watch_runs), (SELECT min(id) FROM postings), "
            "?, ?, ?, ?)",
            (started, started, ms, outcome),
        )
    search.commit()


def _doctor(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str], timeout: int | None = None
) -> str:
    doc = {"scoring": {"timeout_seconds": timeout}} if timeout else None
    cli._doctor_score_calls(cli._Report(), search, doc, START)
    return capsys.readouterr().out.strip()


def test_doctor_reports_the_median_and_the_longest(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(
        search,
        [
            ("2026-10-31T10:00:00Z", 99_000, "scored"),
            ("2026-10-31T11:00:00Z", 118_000, "unparseable"),
            ("2026-10-31T12:00:00Z", 166_000, "scored"),
            ("2026-10-31T13:00:00Z", 200_000, "unreachable"),
            ("2026-10-01T12:00:00Z", 500_000, "scored"),  # outside the 14 days
        ],
    )
    assert _doctor(search, capsys, timeout=300) == (
        "scoring calls   OK  3 answered in 14 days: median 118 s, max 166 s "
        "(timeout 300 s); 1 not answered"
    )


def test_doctor_warns_when_the_longest_is_close_to_the_timeout(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(
        search,
        [("2026-10-31T10:00:00Z", 99_000, "scored"), ("2026-10-31T11:00:00Z", 185_000, "scored")],
    )
    line = _doctor(search, capsys, timeout=200)
    assert line.startswith("scoring calls   WARN 2 answered in 14 days: median 142 s, max 185 s")
    assert line.endswith("(timeout 200 s); the longest is close to the timeout")


def test_doctor_with_no_answered_call(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(search, [])
    assert _doctor(search, capsys) == "scoring calls   --  no scoring call in 14 days"
    _seed(search, [("2026-10-31T10:00:00Z", 5_000, "unreachable")])
    assert _doctor(search, capsys) == (
        "scoring calls   --  none answered in 14 days; 1 not answered"
    )


def test_doctor_counts_timeouts_beside_not_answered(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(
        search,
        [
            ("2026-10-31T10:00:00Z", 120_000, "scored"),
            ("2026-10-31T11:00:00Z", 300_006, "timeout"),
            ("2026-10-31T12:00:00Z", 300_013, "timeout"),
            ("2026-10-31T13:00:00Z", 2_000, "unreachable"),
        ],
    )
    assert _doctor(search, capsys, timeout=300) == (
        "scoring calls   OK  1 answered in 14 days: median 120 s, max 120 s "
        "(timeout 300 s); 2 timed out; 1 not answered"
    )


def test_doctor_with_only_timeouts(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(search, [("2026-10-31T10:00:00Z", 300_006, "timeout")])
    assert _doctor(search, capsys) == "scoring calls   --  none answered in 14 days; 1 timed out"


def test_doctor_names_set_aside_postings_and_how_to_retry(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    cli._doctor_set_aside(cli._Report(), search)
    assert capsys.readouterr().out == "score timeouts  OK  none set aside\n"
    _seed(search, [])
    posting_id = int(search.execute("SELECT min(id) FROM postings").fetchone()[0])
    for stamp in ("2026-10-31T10:00:00Z", "2026-10-31T11:00:00Z"):
        search_store.record_timeout(search, posting_id, stamp, set_aside_after=2)
    cli._doctor_set_aside(cli._Report(), search)
    assert capsys.readouterr().out == (
        "score timeouts  WARN 1 posting timed out 2 times in a row; set aside and not scored "
        "(retry: careerhunter rescore --set-aside)\n"
    )


# --- criteria vetoes (ADR-028) -------------------------------------------------------------


def _vetoes(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str], doc: dict[str, Any] | None
) -> str:
    cli._doctor_criteria_vetoes(cli._Report(), search, doc, START)
    return capsys.readouterr().out.strip()


def test_doctor_counts_vetoes_and_rejected_vetoes_in_the_window(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(
        search,
        [
            ("2026-10-31T10:00:00Z", 90_000, "vetoed"),
            ("2026-10-31T11:00:00Z", 95_000, "vetoed"),
            ("2026-10-31T12:00:00Z", 99_000, "veto_rejected"),
            ("2026-10-31T13:00:00Z", 99_000, "scored"),
            ("2026-10-31T14:00:00Z", 99_000, "capped"),
            ("2026-10-31T15:00:00Z", 99_000, "capped"),
            ("2026-10-31T16:00:00Z", 99_000, "capped"),
            ("2026-10-31T17:00:00Z", 99_000, "cap_rejected"),
            ("2026-10-02T10:00:00Z", 99_000, "capped"),  # older than 14 days
            ("2026-10-01T10:00:00Z", 99_000, "vetoed"),  # older than 14 days
        ],
    )
    doc = {"search": {"criteria_file": "criteria.md"}}
    assert _vetoes(search, capsys, doc) == (
        "criteria vetoes OK  2 vetoed, 1 rejected; 3 capped, 1 rejected in 14 days"
    )


def test_doctor_shows_none_with_no_criteria_file(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _vetoes(search, capsys, None) == "criteria vetoes --  no criteria file"
    doc = {"search": {"criteria_file": "criteria.md"}}
    assert _vetoes(search, capsys, doc) == (
        "criteria vetoes OK  0 vetoed, 0 rejected; 0 capped, 0 rejected in 14 days"
    )
