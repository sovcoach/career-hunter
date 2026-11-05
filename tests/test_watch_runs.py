"""One `watch_runs` row per watch run: started, then finished with counts, or left open.

Boards, postings and model replies here are all made up. A fake clock drives the run
budget; no socket is opened and nothing sleeps.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from careerhunter import search_store
from careerhunter import watch as w
from careerhunter.score import Score
from tests.fakes import FakeClock
from tests.test_fit import FITS, _scored_without_fit, _waiting
from tests.test_rescore import Recorder, board, lever, run
from tests.test_watch import DAY, NAMES, Scripted, Timed, entries, minutes, three_boards, ticking


def runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM watch_runs ORDER BY id").fetchall()


def test_a_finished_run_records_its_counts_and_times(search: sqlite3.Connection) -> None:
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards(), now=ticking())
    assert lines[-1] == "watch: 3 boards fetched, 0 failed, 0 left"
    (row,) = runs(search)
    assert row["started_at"] == "2026-11-01T08:01:00Z"
    assert row["finished_at"] == "2026-11-01T08:05:00Z"
    assert row["budget_reached"] == 0
    assert (row["boards_fetched"], row["boards_failed"], row["boards_left"]) == (3, 0, 0)
    # No scorer: the three new postings are stored and wait, held, for scoring.
    assert (row["held_unscored"], row["held_no_fit"], row["held_off_lane"]) == (3, 0, 0)


def test_each_run_gets_its_own_row(search: sqlite3.Connection) -> None:
    now = ticking()
    for _ in range(2):
        w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards(), now=now)
    first, second = runs(search)
    assert first["finished_at"] < second["started_at"]
    assert second["finished_at"] is not None


def test_a_run_that_crashes_partway_stays_unfinished_with_no_counts(
    search: sqlite3.Connection,
) -> None:
    fetcher = three_boards()
    fetcher.mapping[board_for("Example B")] = sqlite3.OperationalError("synthetic")
    with pytest.raises(sqlite3.OperationalError):
        w.run(search, entries(*NAMES), today=DAY, fetcher=fetcher, now=ticking())
    (row,) = runs(search)
    assert row["started_at"] == "2026-11-01T08:01:00Z"
    assert row["finished_at"] is None
    assert row["budget_reached"] == 0
    counts = ("boards_fetched", "boards_failed", "boards_left")
    held = ("held_unscored", "held_no_fit", "held_off_lane")
    assert all(row[key] is None for key in counts + held)


def test_an_isolated_crash_still_finishes_the_run_and_counts_the_board_as_failed(
    search: sqlite3.Connection,
) -> None:
    fetcher = three_boards()
    fetcher.mapping[board_for("Example B")] = RuntimeError("synthetic")
    w.run(search, entries(*NAMES), today=DAY, fetcher=fetcher, now=ticking())
    (row,) = runs(search)
    assert row["finished_at"] is not None
    assert (row["boards_fetched"], row["boards_failed"]) == (2, 1)


def test_a_budget_that_leaves_a_board_out_finishes_with_the_flag(
    search: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    fetcher = Timed(three_boards().mapping, clock, cost=5)
    lines = w.run(search, entries(*NAMES), today=DAY, fetcher=fetcher, budget=minutes(clock, 10))
    assert lines[-1] == "watch: 2 boards fetched, 0 failed, 1 left"
    (row,) = runs(search)
    assert row["finished_at"] is not None
    assert row["budget_reached"] == 1
    assert (row["boards_fetched"], row["boards_failed"], row["boards_left"]) == (2, 0, 1)


def test_a_budget_cut_in_the_scoring_pass_sets_the_flag_and_counts_what_is_held(
    search: sqlite3.Connection,
) -> None:
    for index in range(3):
        _waiting(search, f"Waiting {index}", f"2026-11-01T0{index}:00:00Z")
    clock = FakeClock()
    scorer = Recorder()
    scorer.clock, scorer.cost = clock, 700.0  # one call uses the whole budget
    lines = w.run(
        search,
        [],
        today=DAY,
        fetcher=Scripted({}),
        scorer=scorer,
        budget=minutes(clock, 10),
        config=FITS,
    )
    assert "watch: scoring: run budget reached; 2 postings left for the next run" in lines
    assert lines[-1] == "watch: 0 boards fetched, 0 failed, 0 left"
    (row,) = runs(search)
    assert row["budget_reached"] == 1
    assert row["held_unscored"] == 2  # true after the cut: one of three was scored


def test_held_counts_leave_out_skipped_and_alerted_postings(search: sqlite3.Connection) -> None:
    _waiting(search, "Unscored", "2026-11-01T07:00:00Z")
    skipped = _waiting(search, "Unscored but skipped", "2026-11-01T07:00:00Z")
    search_store.insert_skip(search, skipped, "2026-11-01", "manual")
    _scored_without_fit(search, "Renamed tier", "renamed")
    alerted = _scored_without_fit(search, "Renamed but alerted", "renamed")
    search_store.mark_alerted(search, alerted, "2026-11-01T09:00:00Z")
    _scored_without_fit(search, "Filled in", "strong")
    w.run(search, [], today=DAY, fetcher=Scripted({}), config=FITS)
    (row,) = runs(search)
    assert (row["held_unscored"], row["held_no_fit"], row["held_off_lane"]) == (1, 1, 0)


def test_a_reply_with_a_tier_not_in_config_is_held_as_no_fit(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Odd"] = Score(llm_output="{}", gaps="[]", tier="renamed")
    run(
        search,
        {board("examplea"): lever("examplea", ("a1", "Odd"))},
        ("examplea",),
        scorer=scorer,
        config=FITS,
    )
    (row,) = runs(search)
    assert (row["held_unscored"], row["held_no_fit"]) == (0, 1)


def board_for(name: str) -> Any:
    from tests.test_watch import board as watch_board

    return watch_board(name)
