"""scoring.run_minutes: scoring goes on after the boards, up to that many minutes from
the run's start, while the boards keep their ten (watch.RUN_BUDGET).

Boards, postings and replies are made up. A fake clock drives both budgets; nothing
sleeps and no socket is opened.
"""

from __future__ import annotations

import sqlite3

import pytest

from careerhunter import cli
from careerhunter import watch as w
from careerhunter.config import scoring_run_minutes, validate_config
from careerhunter.deadline import Deadline
from tests.fakes import FakeClock
from tests.test_rescore import NEW, OLD, Recorder, board, lever, queue, run, seed
from tests.test_watch import Timed

SLUGS = tuple(f"example{c}" for c in "abcdefgh")


def _seeded(search: sqlite3.Connection) -> dict[str, object]:
    for slug in SLUGS:
        seed(search, slug, (f"{slug}1", f"Role {slug}"), resume=OLD)
    queue(search, *(f"Role {slug}" for slug in SLUGS))
    return {board(slug): lever(slug, (f"{slug}1", f"Role {slug}")) for slug in SLUGS}


def _run(
    search: sqlite3.Connection, minutes: int | None, *, board_seconds: float = 207.0
) -> tuple[Recorder, list[str]]:
    """Eight queued re-scores after boards that take `board_seconds` in all, with calls
    of 160 s and the default 300 s timeout."""
    boards = _seeded(search)
    clock = FakeClock()
    scorer = Recorder()
    scorer.clock, scorer.cost = clock, 160.0
    config = {} if minutes is None else {"scoring": {"run_minutes": minutes}}
    total = Deadline(w.run_seconds(config), clock)
    lines = run(
        search,
        boards,
        SLUGS,
        fetcher=Timed(boards, clock, cost=board_seconds / 60 / len(SLUGS)),
        scorer=scorer,
        resume=NEW,
        budget=total,
        boards_budget=Deadline(w.RUN_BUDGET, clock),
        config=config,
    )
    return scorer, lines


@pytest.mark.parametrize(
    ("minutes", "calls"),
    [
        (10, 1),  # the old single budget: 393 s left after the boards, one call
        (None, 5),  # the default, 20: 993 s left; calls start at 993, 833, 673, 513, 353
        (30, 8),  # every queued re-score
    ],
)
def test_scoring_goes_on_after_the_boards_until_run_minutes(
    search: sqlite3.Connection, minutes: int | None, calls: int
) -> None:
    scorer, lines = _run(search, minutes)
    assert len(scorer.calls) == calls
    assert lines[-1] == "watch: 8 boards fetched, 0 failed, 0 left"


def test_the_boards_keep_their_own_ten_minutes(search: sqlite3.Connection) -> None:
    # Boards that would take 16 minutes: the last ones are left at the boards' budget,
    # even with 30 minutes for the run.
    scorer, lines = _run(search, 30, board_seconds=16 * 60)
    assert lines[-1] == "watch: 5 boards fetched, 0 failed, 3 left"
    assert scorer.calls  # the scoring passes still get the rest of the run


def test_without_a_boards_budget_one_budget_counts_as_before(search: sqlite3.Connection) -> None:
    boards = _seeded(search)
    clock = FakeClock()
    scorer = Recorder()
    scorer.clock, scorer.cost = clock, 160.0
    run(
        search,
        boards,
        SLUGS,
        fetcher=Timed(boards, clock, cost=207.0 / 60 / len(SLUGS)),
        scorer=scorer,
        resume=NEW,
        budget=Deadline(w.RUN_BUDGET, clock),
    )
    assert len(scorer.calls) == 1


def test_run_seconds_and_its_bounds() -> None:
    assert w.run_seconds(None) == 20 * 60
    assert w.run_seconds({"scoring": {"run_minutes": 10}}) == w.RUN_BUDGET
    assert w.run_seconds({"scoring": {"run_minutes": 50}}) == 50 * 60
    for bad in (9, 51, "20", True, 20.0):
        assert scoring_run_minutes({"scoring": {"run_minutes": bad}}) == 20


@pytest.mark.parametrize("value", [9, 51, "20", True, 12.5])
def test_config_refuses_a_bad_value(value: object) -> None:
    problems = validate_config({"scoring": {"run_minutes": value}})
    assert "scoring.run_minutes must be an integer from 10 to 50" in problems


@pytest.mark.parametrize("value", [None, 10, 20, 50])
def test_config_accepts_a_good_value(value: object) -> None:
    problems = validate_config({"scoring": {"run_minutes": value}})
    assert not [p for p in problems if "run_minutes" in p]


def test_a_set_value_must_be_under_the_watch_interval() -> None:
    doc = {"scoring": {"run_minutes": 30}, "schedule": {"watcher_interval_minutes": 30}}
    assert "scoring.run_minutes must be less than schedule.watcher_interval_minutes (30)" in (
        validate_config(doc)
    )
    assert scoring_run_minutes(doc) == 20  # not used: the default for that interval
    doc["schedule"]["watcher_interval_minutes"] = 31
    assert not [p for p in validate_config(doc) if "run_minutes" in p]
    assert scoring_run_minutes(doc) == 30


@pytest.mark.parametrize(("interval", "minutes"), [(15, 10), (20, 15), (25, 20), (60, 20)])
def test_unset_follows_a_short_watch_interval(interval: int, minutes: int) -> None:
    assert scoring_run_minutes({"schedule": {"watcher_interval_minutes": interval}}) == minutes


def test_doctor_grace_follows_the_setting() -> None:
    assert cli._run_grace(None).total_seconds() == 20 * 60 + 300
    assert cli._run_grace({"scoring": {"run_minutes": 45}}).total_seconds() == 45 * 60 + 300
