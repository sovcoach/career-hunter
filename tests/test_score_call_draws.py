"""The draws behind a voted scoring call (search migration 0038): how many were sent and
the longest one, stored on its score_calls row, and doctor comparing that longest draw
with scoring.timeout_seconds. Synthetic postings and replies only."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from careerhunter import search_store
from careerhunter.score import Score, ScoreError, score_posting
from tests.fakes import FakeClock
from tests.test_criteria_veto_watch import SLUG, _boards
from tests.test_rescore import OLD, Recorder, run
from tests.test_score import posting, reply
from tests.test_score_calls import _doctor, _seed
from tests.test_scoring_voting import BAD, HIGH, LOW, _voting


def _costed(clock: FakeClock, *steps: tuple[float, str | ScoreError]) -> Callable[..., Any]:
    """A transport whose calls take the given seconds on `clock`, then answer or raise."""
    left = list(steps)

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        json.loads(payload)
        cost, step = left.pop(0)
        clock.advance(cost)
        if isinstance(step, ScoreError):
            raise step
        return reply(step)

    return transport


def test_a_voted_score_carries_its_draws_and_the_longest() -> None:
    clock = FakeClock()
    transport = _costed(clock, (40.0, HIGH), (95.5, LOW), (60.0, HIGH))
    result = score_posting(posting(), _voting(), transport=transport, clock=clock)
    assert result is not None and result.draws == 3
    assert (result.draw_calls, result.longest_draw_ms) == (3, 95500)


def test_a_replaced_draw_counts_as_a_draw_sent_and_its_time_counts() -> None:
    clock = FakeClock()
    timeout = ScoreError("x", kind="timeout")
    transport = _costed(clock, (30.0, HIGH), (299.0, timeout), (50.0, HIGH))
    result = score_posting(posting(), _voting(), transport=transport, clock=clock)
    assert result is not None and result.draws == 2
    assert (result.draw_calls, result.longest_draw_ms) == (3, 299000)


def test_a_failed_vote_carries_its_draws_on_the_error() -> None:
    clock = FakeClock()
    timeout = ScoreError("x", kind="timeout")
    transport = _costed(clock, (70.0, BAD), (300.0, timeout))
    with pytest.raises(ScoreError) as raised:
        score_posting(posting(), _voting(), transport=transport, clock=clock)
    assert (raised.value.draw_calls, raised.value.longest_draw_ms) == (2, 300000)


def test_one_draw_records_no_draw_columns() -> None:
    clock = FakeClock()
    transport = _costed(clock, (40.0, HIGH))
    result = score_posting(posting(), _voting(on=False), transport=transport, clock=clock)
    assert result is not None and (result.draw_calls, result.longest_draw_ms) == (None, None)
    assert ScoreError("x").draw_calls is None


def test_the_draws_are_stored_and_read_back(search: sqlite3.Connection) -> None:
    _seed(search, [])
    run_id = search.execute("SELECT max(id) FROM watch_runs").fetchone()[0]
    posting_id = search.execute("SELECT min(id) FROM postings").fetchone()[0]
    search_store.record_score_call(
        search,
        run_id,
        posting_id,
        "2026-10-31T10:00:00Z",
        "2026-10-31T10:04:10Z",
        250_000,
        "scored",
        draw_calls=3,
        longest_draw_ms=120_000,
    )
    row = search.execute(
        "SELECT duration_ms, draw_calls, longest_draw_ms FROM score_calls"
    ).fetchone()
    assert tuple(row) == (250_000, 3, 120_000)


def _voted(search: sqlite3.Connection, calls: list[tuple[str, int, str, int | None]]) -> None:
    """(started_at, duration_ms, outcome, longest_draw_ms) rows."""
    _seed(search, [(started, ms, outcome) for started, ms, outcome, _ in calls])
    for (_started, _ms, _outcome, longest), (row_id,) in zip(
        calls, search.execute("SELECT id FROM score_calls ORDER BY id").fetchall(), strict=True
    ):
        search.execute(
            "UPDATE score_calls SET longest_draw_ms = ?, draw_calls = ? WHERE id = ?",
            (longest, None if longest is None else 2, row_id),
        )
    search.commit()


def test_doctor_compares_the_longest_draw_not_the_voted_total(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two draws of 120 s each: a 250 s call is not close to a 200 s timeout.
    _voted(search, [("2026-10-31T10:00:00Z", 250_000, "scored", 120_000)])
    assert _doctor(search, capsys, timeout=200) == (
        "scoring calls   OK  1 answered in 14 days: median 250 s, max 250 s, "
        "longest draw 120 s (timeout 200 s)"
    )


def test_doctor_warns_on_a_long_draw_inside_a_voted_call(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _voted(
        search,
        [
            ("2026-10-31T10:00:00Z", 250_000, "scored", 190_000),
            ("2026-10-31T11:00:00Z", 100_000, "scored", None),  # one draw: its duration
        ],
    )
    line = _doctor(search, capsys, timeout=200)
    assert "longest draw 190 s (timeout 200 s)" in line
    assert line.startswith("scoring calls   WARN") and line.endswith(
        "the longest draw is close to the timeout"
    )
    assert search_store.score_call_stats(search, "2026-10-01T00:00:00Z").longest_draw_ms == 190_000


def test_a_watch_run_stores_the_draws_of_a_voted_score(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(
        llm_output='{"gaps":[]}',
        gaps="[]",
        tier="high",
        draws=2,
        draw_calls=3,
        longest_draw_ms=88_000,
    )
    run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    row = search.execute("SELECT outcome, draw_calls, longest_draw_ms FROM score_calls").fetchone()
    assert tuple(row) == ("scored", 3, 88_000)
