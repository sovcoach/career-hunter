"""Scoring timeouts: counted in a row per posting, set aside after a limit, and released."""

from __future__ import annotations

import sqlite3

from careerhunter import search_store


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
    assert stats == search_store.ScoreCallStats((120000,), not_answered=1, timed_out=2)


def test_a_timeout_is_a_health_failure_kind(search: sqlite3.Connection) -> None:
    search_store.record_component(
        search, "lm_studio", failed="timeout", stamp="2026-09-01T10:00:00Z"
    )
    (health,) = [h for h in search_store.component_health(search) if h.component == "lm_studio"]
    assert (health.failures, health.last_failure) == (1, "timeout")
