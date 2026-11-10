"""Watch and discovery health (#42): what each run records. Synthetic boards; no sockets."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

import pytest

from careerhunter import discover, search_store
from careerhunter import watch as w
from careerhunter.fetch import FetchError
from careerhunter.filters import PostingText
from careerhunter.score import Score, ScoreError
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

SLUG = "examplewidgets"
BOARD = company("Example Widgets", "greenhouse", SLUG)
RUBRIC = {"fit_rubric": {"gap_tiers": [{"name": "strong", "max_gaps": 0, "fit": "high"}]}}
SEARX = {
    "discovery": {"searxng_url": "http://127.0.0.1:8888/search", "career_hosts": []},
    "search": {"target_titles": ["One"], "locations": ["Example City"]},
}


def _board(*, fail: bool = False, title: str = "Coordinator", ident: int = 1001) -> Scripted:
    found: Any = (
        FetchError("synthetic failure") if fail else fetched(one_greenhouse(SLUG, title, ident))
    )
    return Scripted({url("greenhouse", SLUG): found})


def _health(conn: sqlite3.Connection) -> dict[str, tuple[int, str | None, int | None]]:
    return {
        h.component: (h.failures, h.last_failure, h.held)
        for h in search_store.component_health(conn)
    }


def _useful(conn: sqlite3.Connection) -> list[int | None]:
    return [row[0] for row in conn.execute("SELECT useful FROM watch_runs ORDER BY id")]


def _answers(posting: PostingText) -> Score:
    return Score(llm_output='{"gaps":[]}', gaps="[]", tier="strong")


def _down(posting: PostingText) -> Score | None:
    raise ScoreError("the local model could not be reached")


def _garbled(posting: PostingText) -> Score | None:
    return None  # a reply that does not parse: the model answered


def test_a_run_that_fetches_a_board_and_scores_is_useful(search: sqlite3.Connection) -> None:
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_answers)
    assert _useful(search) == [1]
    health = _health(search)
    assert health["boards"] == (0, None, 0)
    assert health["lm_studio"] == (0, None, 0)
    assert health["searxng"] == (0, None, None)  # never reached: left as it was


def test_a_reply_that_does_not_parse_still_counts_as_the_model_answering(
    search: sqlite3.Connection,
) -> None:
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_garbled)
    assert _useful(search) == [1]
    assert _health(search)["lm_studio"] == (0, None, 1)  # one posting still waits


def test_every_board_failing_is_a_failed_run_of_the_boards(search: sqlite3.Connection) -> None:
    for runs in (1, 2):
        w.run(search, [BOARD], today=DAY, fetcher=_board(fail=True))
        assert _health(search)["boards"] == (runs, "failed", 0)
    assert _useful(search) == [0, 0]
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_answers)
    assert _health(search)["boards"] == (0, None, 0)  # one fetched board clears it
    assert _useful(search) == [0, 0, 1]


def test_a_model_that_is_down_fails_the_run_and_counts_what_is_held(
    search: sqlite3.Connection,
) -> None:
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_down)
    assert _useful(search) == [0]  # postings waited and the scorer never answered
    assert _health(search)["lm_studio"] == (1, "unreachable", 1)
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_down)
    assert _health(search)["lm_studio"] == (2, "unreachable", 1)
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_answers)
    assert _health(search)["lm_studio"] == (0, None, 0)


def test_a_scorer_held_for_its_resume_is_not_useful_and_not_a_model_failure(
    search: sqlite3.Connection,
) -> None:
    w.run(
        search,
        [BOARD],
        today=DAY,
        fetcher=_board(),
        config=RUBRIC,
        scorer=_answers,
        resume_problem="not_imported",
    )
    assert _useful(search) == [0]
    assert _health(search)["lm_studio"] == (0, None, None)  # never reached


def test_a_run_with_nothing_to_score_needs_no_scorer(search: sqlite3.Connection) -> None:
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_answers)
    w.run(search, [BOARD], today=DAY, fetcher=_board(), config=RUBRIC, scorer=_down)
    assert _useful(search) == [1, 1]  # the second run had nothing waiting
    assert _health(search)["lm_studio"] == (0, None, 0)


def test_recovery_is_due_only_after_a_failure_message_was_sent(
    search: sqlite3.Connection,
) -> None:
    stamp = "2026-11-01T10:00:00Z"
    with search:
        search_store.record_component(search, "searxng", failed="unreachable", stamp=stamp)
        search_store.record_component(search, "searxng", failed=None, stamp=stamp)
    row = search.execute("SELECT recovery_due FROM component_health WHERE component = 'searxng'")
    assert row.fetchone()[0] == 0  # no failure message was sent, so no recovery either
    with search:
        search_store.record_component(search, "searxng", failed="bad-reply", stamp=stamp)
        search.execute(
            "UPDATE component_health SET alerted_failures = 1 WHERE component = 'searxng'"
        )
        search_store.record_component(search, "searxng", failed=None, stamp=stamp)
    row = search.execute(
        "SELECT recovery_due, alerted_failures FROM component_health WHERE component = 'searxng'"
    ).fetchone()
    assert tuple(row) == (1, None)
    with pytest.raises(ValueError, match="unknown component"):
        search_store.record_component(search, "embedding", failed=None, stamp=stamp)
    with pytest.raises(ValueError, match="unknown failure kind"):
        search_store.record_component(search, "boards", failed="gone", stamp=stamp)


# --- discovery passes -----------------------------------------------------------------


class _Talk:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def __call__(self, address: str) -> tuple[int, bytes]:
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _pass(conn: sqlite3.Connection, answer: Any, moment: dt.datetime) -> discover.Outcome:
    return discover.run(
        conn,
        SEARX,
        today=DAY,
        transport=_Talk(answer),
        sleep=lambda seconds: None,
        now=lambda: moment,
    )


def _state(conn: sqlite3.Connection) -> tuple[str | None, int]:
    row = conn.execute("SELECT useful_at, unproductive FROM discovery_state").fetchone()
    return row[0], int(row[1])


NOW = dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(days=30)  # past any backoff


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (OSError("down"), "unreachable"),
        ((500, b""), "bad-reply"),
        ((200, b"<html>"), "bad-reply"),
        ((429, b""), "throttled"),
    ],
)
def test_a_failed_pass_is_unproductive_and_fails_searxng(
    search: sqlite3.Connection, answer: Any, kind: str
) -> None:
    _pass(search, answer, NOW)
    assert _state(search) == (None, 1)
    assert _health(search)["searxng"] == (1, kind, 0)


def test_a_pass_with_usable_replies_is_useful_and_clears_the_count(
    search: sqlite3.Connection,
) -> None:
    _pass(search, OSError("down"), NOW)
    _pass(search, (200, b"<html>"), LATER)
    assert _state(search) == (None, 2)
    assert _health(search)["searxng"] == (2, "bad-reply", 0)
    good = (200, json.dumps({"results": []}).encode())
    later = LATER + dt.timedelta(days=30)
    _pass(search, good, later)
    assert _state(search) == ("2026-12-31T10:00:00Z", 0)
    assert _health(search)["searxng"] == (0, None, 0)


def test_a_pass_inside_its_backoff_or_off_changes_nothing(search: sqlite3.Connection) -> None:
    _pass(search, OSError("down"), NOW)
    before = (_state(search), _health(search))
    outcome = _pass(search, OSError("down"), NOW + dt.timedelta(minutes=1))
    assert outcome.notice is not None and outcome.notice.startswith("discovery backing off")
    off = discover.run(search, {"discovery": {"searxng_url": None}}, today=DAY)
    assert off.notice == discover.OFF
    assert (_state(search), _health(search)) == before
