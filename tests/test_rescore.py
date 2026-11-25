"""Scoring with the resume, and re-scoring what `careerhunter rescore` queued (ADR-023).
A new resume applies only to postings scored after it (batch R2).

Boards, postings, resumes and model replies here are all made up. A fake clock drives
the run budget; no socket is opened and nothing sleeps.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

import pytest

from careerhunter import db, search_store
from careerhunter import watch as w
from careerhunter.filters import PostingText
from careerhunter.resume import Resume
from careerhunter.score import Score, ScoreError
from tests.fakes import FakeClock
from tests.test_watch import (
    DAY,
    STILL_LISTED,
    OnBoard,
    Scripted,
    Timed,
    company,
    fetched,
    minutes,
    ticking,
    url,
)

OLD = Resume("Avery Example, first version of a synthetic resume.", "a" * 64)
NEW = Resume("Avery Example, second version of a synthetic resume.", "b" * 64)
DESC = "Synthetic description: coordinate deliveries for a sample warehouse."


def lever(slug: str, *jobs: tuple[str, str]) -> Any:
    """A Lever board: each job is (id, title), with a description in the list row."""
    return fetched(
        [
            {
                "id": ident,
                "text": title,
                "hostedUrl": f"https://jobs.lever.co/{slug}/{ident}",
                "categories": {"location": "Example City"},
                "workplaceType": "onsite",
                "descriptionPlain": f"{DESC} ({title})",
            }
            for ident, title in jobs
        ]
    )


def board(slug: str) -> str:
    return url("lever", slug)


def watched(*slugs: str) -> list[object]:
    return [company(slug.title(), "lever", slug) for slug in slugs]


class Recorder:
    """A scorer that records what it was sent, and replies from a script."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[PostingText] = []
        self.events = events if events is not None else []
        self.replies: dict[str, Score | Exception | None] = {}
        self.cost = 0.0
        self.clock: FakeClock | None = None

    def __call__(self, text: PostingText) -> Score | None:
        self.calls.append(text)
        self.events.append(f"score {text.title}")
        if self.clock is not None:
            self.clock.advance(self.cost)
        reply = self.replies.get(text.title, Score(llm_output="{}", gaps="[]", tier="high"))
        if isinstance(reply, Exception):
            raise reply
        return reply


class Logged(Scripted):
    def __init__(self, mapping: dict[str, Any], events: list[str]) -> None:
        super().__init__(mapping)
        self.events = events

    def __call__(self, address: str) -> Any:
        self.events.append(f"fetch {address}")
        return super().__call__(address)


def row(conn: sqlite3.Connection, title: str) -> sqlite3.Row:
    found: sqlite3.Row = conn.execute(
        "SELECT id, llm_output, gaps, scored_resume_sha256, score_attempted_at, alerted_at "
        "FROM postings WHERE title = ?",
        (title,),
    ).fetchone()
    return found


def queue(conn: sqlite3.Connection, *names: str) -> None:
    """Queue these postings for a re-score, as `careerhunter rescore` does."""
    with db.transaction(conn):
        search_store.mark_for_rescore(conn, [int(row(conn, name)["id"]) for name in names])


def titles(scorer: Recorder) -> list[str]:
    return [text.title for text in scorer.calls]


# The fake model's calls take 150 s or less, so a call may start with that much left
# (scoring.timeout_seconds); the default 300 s would leave these runs no scoring time.
FAST_CALLS = {"scoring": {"timeout_seconds": 150}}


def run(
    conn: sqlite3.Connection, boards: dict[str, Any], slugs: tuple[str, ...], **kw: Any
) -> list[str]:
    return w.run(
        conn, watched(*slugs), today=DAY, fetcher=kw.pop("fetcher", Scripted(boards)), **kw
    )


def seed(
    conn: sqlite3.Connection, slug: str, *jobs: tuple[str, str], resume: Resume | None
) -> None:
    """A first run that stores and scores these jobs with `resume`."""
    run(conn, {board(slug): lever(slug, *jobs)}, (slug,), scorer=Recorder(), resume=resume)


# --- the hash a score was made with ---------------------------------------------------------


def test_a_score_records_the_resume_it_was_made_with(search: sqlite3.Connection) -> None:
    seed(search, "examplerobotics", ("r1", "Coordinator"), resume=OLD)
    assert row(search, "Coordinator")["scored_resume_sha256"] == OLD.sha256
    assert row(search, "Coordinator")["score_attempted_at"] is not None


def test_without_a_resume_nothing_is_re_scored(search: sqlite3.Connection) -> None:
    seed(search, "examplerobotics", ("r1", "Coordinator"), resume=None)
    assert row(search, "Coordinator")["scored_resume_sha256"] is None
    scorer = Recorder()
    run(
        search,
        {board("examplerobotics"): lever("examplerobotics", ("r1", "Coordinator"))},
        ("examplerobotics",),
        scorer=scorer,
    )
    assert scorer.calls == []


def test_an_unusable_resume_holds_all_scoring(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    boards = {board("examplerobotics"): lever("examplerobotics", ("r1", "Coordinator"))}
    lines = run(
        search,
        boards,
        ("examplerobotics",),
        scorer=scorer,
        resume_problem="the resume file does not match its import; run careerhunter resume import",
    )
    assert scorer.calls == []
    assert lines[0] == (
        "watch: scoring: resume not usable (the resume file does not match its import; "
        "run careerhunter resume import); left for the next run"
    )
    assert row(search, "Coordinator")["llm_output"] is None
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"


# --- re-scoring what is queued, and a new resume ---


def test_a_new_resume_queues_no_re_scores(search: sqlite3.Connection) -> None:
    seed(search, "examplerobotics", ("r1", "Coordinator"), resume=OLD)
    before = row(search, "Coordinator")
    scorer = Recorder()
    boards = {
        board("examplerobotics"): lever("examplerobotics", ("r1", "Coordinator"), ("r2", "Planner"))
    }
    run(search, boards, ("examplerobotics",), scorer=scorer, resume=NEW)
    assert titles(scorer) == ["Planner"]  # only the new posting, scored with the new resume
    after = row(search, "Coordinator")
    assert (after["llm_output"], after["scored_resume_sha256"]) == (
        before["llm_output"],
        OLD.sha256,
    )
    assert row(search, "Planner")["scored_resume_sha256"] == NEW.sha256
    run(search, boards, ("examplerobotics",), scorer=scorer, resume=NEW)
    assert titles(scorer) == [
        "Planner"
    ]  # and not on a later run either------------------------------------------------------------


def test_a_stored_posting_on_a_fetched_board_is_re_scored_after_the_boards_with_its_description(
    search: sqlite3.Connection,
) -> None:
    seed(search, "examplerobotics", ("r1", "Coordinator"), resume=OLD)
    queue(search, "Coordinator")
    events: list[str] = []
    scorer = Recorder(events)
    boards = {
        board("examplerobotics"): lever(
            "examplerobotics", ("r1", "Coordinator"), ("r2", "Planner")
        ),
        board("samplelogistics"): lever("samplelogistics", ("s1", "Dispatcher")),
    }
    lines = run(
        search,
        boards,
        ("examplerobotics", "samplelogistics"),
        fetcher=Logged(boards, events),
        scorer=scorer,
        resume=NEW,
    )
    # New postings are scored inline; the re-score waits until every board is fetched.
    assert events == [
        f"fetch {board('samplelogistics')}",  # never tried: it goes first
        "score Dispatcher",
        f"fetch {board('examplerobotics')}",
        "score Planner",
        "score Coordinator",
    ]
    rescored = scorer.calls[-1]
    assert rescored.description == f"{DESC} (Coordinator)"
    assert row(search, "Coordinator")["scored_resume_sha256"] == NEW.sha256
    assert "watch: Examplerobotics: 'Coordinator' re-scored" in lines
    assert all(DESC not in line for line in lines)


def test_passes_go_never_scored_then_queued_then_stored_fields(search: sqlite3.Connection) -> None:
    # Stored, then queued: one stays on a fetched board, one is gone from its list
    # and is read again on its own before it is re-scored (ADR-027).
    seed(search, "examplerobotics", ("r1", "Listed"), ("r2", "Delisted"), resume=OLD)
    queue(search, "Listed", "Delisted")
    # Stored but never scored.
    run(
        search,
        {board("samplelogistics"): lever("samplelogistics", ("s1", "Waiting"))},
        ("samplelogistics",),
    )
    scorer = Recorder()
    boards = {
        board("examplerobotics"): lever("examplerobotics", ("r1", "Listed")),
        board("samplelogistics"): lever("samplelogistics", ("s1", "Waiting")),
    }
    run(
        search,
        boards,
        ("examplerobotics", "samplelogistics"),
        fetcher=OnBoard(boards),
        scorer=scorer,
        resume=NEW,
    )
    assert titles(scorer) == ["Waiting", "Listed", "Delisted"]
    waiting, listed, delisted = scorer.calls
    assert waiting.description == f"{DESC} (Waiting)"  # met on its board this run
    assert listed.description == f"{DESC} (Listed)"
    assert delisted.description == STILL_LISTED  # read again: never the stored fields only


def test_a_re_score_whose_posting_left_its_board_is_skipped_as_gone(
    search: sqlite3.Connection,
) -> None:
    seed(search, "examplerobotics", ("r1", "Listed"), ("r2", "Delisted"), resume=OLD)
    queue(search, "Listed", "Delisted")
    single = "https://api.lever.co/v0/postings/examplerobotics/r2?mode=json"
    boards = {
        board("examplerobotics"): lever("examplerobotics", ("r1", "Listed")),
        single: fetched({}, status=404),
    }
    scorer = Recorder()
    lines = run(search, boards, ("examplerobotics",), scorer=scorer, resume=NEW)
    assert titles(scorer) == ["Listed"]
    assert "watch: Examplerobotics (lever): 'Delisted' skipped (gone)" in lines
    reasons = search.execute(
        "SELECT s.reason FROM skips s JOIN postings p ON p.id = s.posting_id "
        "WHERE p.title = 'Delisted'"
    ).fetchall()
    assert [r[0] for r in reasons] == ["gone"]


def test_skipped_and_applied_postings_keep_their_score(search: sqlite3.Connection) -> None:
    seed(
        search, "examplerobotics", ("r1", "Skipped"), ("r2", "Applied"), ("r3", "Open"), resume=OLD
    )
    with db.transaction(search):
        search_store.insert_skip(search, int(row(search, "Skipped")["id"]), DAY, "not for me")
        search.execute(
            "INSERT INTO applications (correlation_id, posting_id, date_applied, "
            "date_precision, method) VALUES (?, ?, ?, 'exact', 'company_site')",
            ("c" * 32, int(row(search, "Applied")["id"]), DAY),
        )
    queue(search, "Skipped", "Applied", "Open")
    before = row(search, "Skipped")["llm_output"], row(search, "Applied")["llm_output"]
    scorer = Recorder()
    run(
        search,
        {
            board("examplerobotics"): lever(
                "examplerobotics", ("r1", "Skipped"), ("r2", "Applied"), ("r3", "Open")
            )
        },
        ("examplerobotics",),
        scorer=scorer,
        resume=NEW,
    )
    assert titles(scorer) == ["Open"]  # queued, but a skip or an application keeps its score
    assert (row(search, "Skipped")["llm_output"], row(search, "Applied")["llm_output"]) == before


def test_an_alerted_posting_is_re_scored_and_not_alerted_again(search: sqlite3.Connection) -> None:
    seed(search, "examplerobotics", ("r1", "Coordinator"), resume=OLD)
    posting_id = int(row(search, "Coordinator")["id"])
    with db.transaction(search):
        search_store.mark_alerted(search, posting_id, "2026-11-01T09:00:00Z")
    queue(search, "Coordinator")
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(llm_output='{"gaps":[]}', gaps="[]", tier="medium")
    run(
        search,
        {board("examplerobotics"): lever("examplerobotics", ("r1", "Coordinator"))},
        ("examplerobotics",),
        scorer=scorer,
        resume=NEW,
    )
    after = row(search, "Coordinator")
    assert after["scored_resume_sha256"] == NEW.sha256
    assert after["alerted_at"] == "2026-11-01T09:00:00Z"
    assert search_store.pending_alert_ids(search) == []


@pytest.mark.parametrize("failure", ["unparseable", "unreachable"])
def test_a_failed_re_score_keeps_the_old_score_and_stays_queued(
    search: sqlite3.Connection, failure: str
) -> None:
    seed(search, "examplerobotics", ("r1", "Coordinator"), resume=OLD)
    queue(search, "Coordinator")
    before = row(search, "Coordinator")
    scorer = Recorder()
    scorer.replies["Coordinator"] = (
        None if failure == "unparseable" else ScoreError("the local model could not be reached")
    )
    lines = run(
        search,
        {board("examplerobotics"): lever("examplerobotics", ("r1", "Coordinator"))},
        ("examplerobotics",),
        scorer=scorer,
        resume=NEW,
        now=ticking(),
    )
    after = row(search, "Coordinator")
    assert (after["llm_output"], after["gaps"], after["scored_resume_sha256"]) == (
        before["llm_output"],
        before["gaps"],
        None,  # still queued
    )
    reason = "unparseable reply" if failure == "unparseable" else "model unreachable"
    assert (
        f"watch: Examplerobotics: 'Coordinator' left unscored ({reason}); old score kept" in lines
    )
    if failure == "unreachable":  # not the posting's fault: it keeps its place
        assert after["score_attempted_at"] == before["score_attempted_at"]
    else:  # behind the others next time
        assert after["score_attempted_at"] != before["score_attempted_at"]


# --- the budget, the reserve, and starvation ------------------------------------------------------


def test_later_boards_are_fetched_while_re_scores_wait(search: sqlite3.Connection) -> None:
    slugs = ("examplea", "exampleb", "examplec")
    for slug in slugs:
        seed(search, slug, (f"{slug}1", f"Role {slug}"), resume=OLD)
    queue(search, *(f"Role {slug}" for slug in slugs))
    clock = FakeClock()
    boards = {board(slug): lever(slug, (f"{slug}1", f"Role {slug}")) for slug in slugs}
    events: list[str] = []
    fetcher = Timed(boards, clock, cost=0.5)
    scorer = Recorder(events)
    scorer.clock, scorer.cost = clock, 150.0  # each model call takes 150 s

    def fetch(address: str) -> Any:
        events.append("fetch")
        return fetcher(address)

    lines = run(
        search,
        boards,
        slugs,
        fetcher=fetch,
        scorer=scorer,
        resume=NEW,
        budget=minutes(clock, 10),
        config=FAST_CALLS,
    )
    # Every board is fetched before any re-score; after 90 s of fetching, the calls
    # start with 510, 360 and 210 s left, each at least the 150 s one may take.
    assert events == ["fetch"] * 3 + [f"score Role {slug}" for slug in slugs]
    assert all(row(search, f"Role {s}")["scored_resume_sha256"] == NEW.sha256 for s in slugs)
    assert lines[-1] == "watch: 3 boards fetched, 0 failed, 0 left"


def test_a_budget_cut_leaves_re_scores_for_the_next_run_uncounted(
    search: sqlite3.Connection,
) -> None:
    slugs = ("examplea", "exampleb", "examplec", "exampled")
    for slug in slugs:
        seed(search, slug, (f"{slug}1", f"Role {slug}"), resume=OLD)
    queue(search, *(f"Role {slug}" for slug in slugs))
    clock = FakeClock()
    boards = {board(slug): lever(slug, (f"{slug}1", f"Role {slug}")) for slug in slugs}
    scorer = Recorder()
    scorer.clock, scorer.cost = clock, 250.0
    lines = run(
        search,
        boards,
        slugs,
        fetcher=Timed(boards, clock, cost=0.0),
        scorer=scorer,
        resume=NEW,
        budget=minutes(clock, 10),
        config={"scoring": {"timeout_seconds": 250}},
    )
    # Calls start at 0 and 250 s. At 500 s only 100 s are left, less than the 250 s a
    # call may take, so no third call starts: the budget would cut it short.
    assert len(scorer.calls) == 2
    assert "watch: scoring: run budget reached; 2 postings left for the next run" in lines
    hashes = [row(search, f"Role {s}")["scored_resume_sha256"] for s in slugs]
    assert hashes.count(None) == 2 and hashes.count(NEW.sha256) == 2  # two still queued
    assert lines[-1] == "watch: 4 boards fetched, 0 failed, 0 left"


def test_new_postings_in_the_reserve_wait_for_the_passes_and_keep_their_description(
    search: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    boards = {
        board("examplea"): lever("examplea", ("a1", "First")),
        board("exampleb"): lever("exampleb", ("b1", "Second")),
    }
    # The first fetch takes 6.5 minutes: 210 s are left, under the 240 s reserve.
    fetcher = Timed(boards, clock, costs={board("examplea"): 6.5, board("exampleb"): 0.0})
    scorer = Recorder()
    lines = run(
        search,
        boards,
        ("examplea", "exampleb"),
        fetcher=fetcher,
        scorer=scorer,
        budget=minutes(clock, 10),
        config=FAST_CALLS,
    )
    assert "watch: Examplea (lever): 'First' left for scoring after the boards (reserve)" in lines
    assert "watch: Exampleb (lever): 'Second' left for scoring after the boards (reserve)" in lines
    assert titles(scorer) == ["First", "Second"]
    assert all(text.description is not None for text in scorer.calls)


def test_a_posting_that_keeps_failing_goes_behind_the_others(search: sqlite3.Connection) -> None:
    """One model call fits in each run. The failing posting does not take every turn."""
    run(
        search,
        {board("examplea"): lever("examplea", ("a1", "Poison"), ("a2", "Healthy"))},
        ("examplea",),
    )
    # Stored before the first attempt on the same clock as the attempt stamps.
    search.execute("UPDATE postings SET created_at = '2026-11-01T07:00:00Z'")
    seen: list[str] = []
    for _ in range(2):
        clock = FakeClock()
        scorer = Recorder()
        scorer.clock, scorer.cost = clock, 700.0  # one call uses the whole budget
        scorer.replies["Poison"] = None
        w.run(
            search,
            [],
            today=DAY,
            fetcher=OnBoard({}),
            scorer=scorer,
            budget=minutes(clock, 10),
            now=ticking(),
        )
        seen += titles(scorer)
    assert seen == ["Poison", "Healthy"]
    assert row(search, "Healthy")["llm_output"] is not None
    assert row(search, "Poison")["llm_output"] is None


def test_never_scored_postings_go_before_re_scores_and_the_rest_is_not_counted(
    search: sqlite3.Connection,
) -> None:
    seed(search, "examplea", ("a1", "Scored"), resume=OLD)
    queue(search, "Scored")
    run(search, {board("exampleb"): lever("exampleb", ("b1", "Waiting"))}, ("exampleb",))
    clock = FakeClock()
    scorer = Recorder()
    scorer.clock, scorer.cost = clock, 700.0
    lines = w.run(
        search,
        [],
        today=DAY,
        fetcher=OnBoard({}),
        scorer=scorer,
        resume=NEW,
        budget=minutes(clock, 10),
    )
    assert titles(scorer) == ["Waiting"]
    assert "watch: scoring: run budget reached; 1 postings left for the next run" in lines
    assert row(search, "Scored")["scored_resume_sha256"] is None  # still queued
    assert lines[-1] == "watch: 0 boards fetched, 0 failed, 0 left"


def test_a_model_that_goes_down_stops_the_passes_and_counts_what_is_left(
    search: sqlite3.Connection,
) -> None:
    seed(search, "examplea", ("a1", "One"), ("a2", "Two"), ("a3", "Three"), resume=OLD)
    queue(search, "One", "Two", "Three")
    scorer = Recorder()
    scorer.replies["One"] = ScoreError("the local model could not be reached")
    lines = w.run(search, [], today=DAY, fetcher=OnBoard({}), scorer=scorer, resume=NEW)
    assert titles(scorer) == ["One"]
    assert "watch: scoring: model unreachable; 2 postings left for the next run" in lines


def test_score_stamps_are_utc(search: sqlite3.Connection) -> None:
    moment = dt.datetime(2026, 11, 1, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    run(
        search,
        {board("examplea"): lever("examplea", ("a1", "One"))},
        ("examplea",),
        scorer=Recorder(),
        now=lambda: moment,
    )
    assert row(search, "One")["score_attempted_at"] == "2026-10-31T22:04:05Z"


def test_a_budget_that_runs_out_during_a_call_keeps_the_postings_place(
    search: sqlite3.Connection,
) -> None:
    run(search, {board("examplea"): lever("examplea", ("a1", "One"), ("a2", "Two"))}, ("examplea",))
    clock = FakeClock()

    def slow_then_cut(text: PostingText) -> Score | None:
        clock.advance(700.0)  # past the whole budget
        raise ScoreError("the local model could not be reached")

    lines = w.run(
        search,
        [],
        today=DAY,
        fetcher=OnBoard({}),
        scorer=slow_then_cut,
        budget=minutes(clock, 10),
        now=ticking(),
    )
    assert "watch: Examplea: 'One' left for the next run (run budget reached)" in lines
    assert "model unreachable" not in "\n".join(lines)  # a cut budget is not a down model
    assert "watch: scoring: run budget reached; 1 postings left for the next run" in lines
    assert row(search, "One")["score_attempted_at"] is None  # its earlier (empty) stamp is back
