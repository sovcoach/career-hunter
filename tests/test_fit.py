"""Fit tiers on scored postings, the fill-in from config, and the first-come retry order.

Companies, postings, tiers and model replies here are all made up. A fake clock drives
the run budget; no socket is opened and nothing sleeps.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from careerhunter import search_store
from careerhunter import watch as w
from careerhunter.score import Score
from tests.fakes import FakeClock
from tests.test_rescore import NEW, OLD, Recorder, board, lever, run, titles
from tests.test_watch import DAY, Scripted, minutes

FITS = {
    "fit_rubric": {
        "gap_tiers": {
            "tiers": [
                {"name": "strong", "max_gaps": 1, "fit": "high"},
                {"name": "possible", "max_gaps": 2, "fit": "medium"},
            ],
            "exclude_from_gaps": 3,
        }
    }
}


def fit_of(conn: sqlite3.Connection, title: str) -> str | None:
    row = conn.execute("SELECT fit FROM postings WHERE title = ?", (title,)).fetchone()
    return None if row["fit"] is None else str(row["fit"])


def scored(tier: str | None, *, excluded: bool = False) -> Score:
    return Score(llm_output="{}", gaps="[]", tier=tier, excluded=excluded)


def one_board(*jobs: tuple[str, str]) -> dict[str, Any]:
    return {board("examplea"): lever("examplea", *jobs)}


# --- the fit is stored with the score -------------------------------------------------------


def test_a_new_score_stores_its_tiers_fit(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Second"] = scored("possible")
    run(
        search,
        one_board(("a1", "First"), ("a2", "Second")),
        ("examplea",),
        scorer=scorer,
        config=FITS,
    )
    assert fit_of(search, "First") == "high"
    assert fit_of(search, "Second") == "medium"


def test_an_excluded_posting_has_no_fit(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Too far"] = scored(None, excluded=True)
    lines = run(search, one_board(("a1", "Too far")), ("examplea",), scorer=scorer, config=FITS)
    assert "watch: Examplea (lever): 'Too far' skipped (gap_tier)" in lines
    assert fit_of(search, "Too far") is None


def test_a_tier_name_missing_from_config_leaves_the_fit_null(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Odd"] = scored("renamed")
    run(search, one_board(("a1", "Odd")), ("examplea",), scorer=scorer, config=FITS)
    row = search.execute("SELECT llm_output, fit FROM postings WHERE title = 'Odd'").fetchone()
    assert row["llm_output"] is not None
    assert row["fit"] is None


def test_a_re_score_replaces_the_fit_and_a_failed_one_keeps_it(search: sqlite3.Connection) -> None:
    jobs = (("a1", "Kept"), ("a2", "Moved"))
    run(search, one_board(*jobs), ("examplea",), scorer=Recorder(), resume=OLD, config=FITS)
    assert (fit_of(search, "Kept"), fit_of(search, "Moved")) == ("high", "high")
    scorer = Recorder()
    scorer.replies["Kept"] = None  # unparseable: the old score, hash and fit stay
    scorer.replies["Moved"] = scored("possible")
    run(search, one_board(*jobs), ("examplea",), scorer=scorer, resume=NEW, config=FITS)
    assert sorted(titles(scorer)) == ["Kept", "Moved"]
    assert fit_of(search, "Kept") == "high"
    assert fit_of(search, "Moved") == "medium"
    hashes = dict(search.execute("SELECT title, scored_resume_sha256 FROM postings").fetchall())
    assert hashes == {"Kept": OLD.sha256, "Moved": NEW.sha256}


# --- the fill-in for postings scored before fits existed ----------------------------------------


def _scored_without_fit(conn: sqlite3.Connection, title: str, tier: str | None) -> int:
    company = conn.execute("SELECT id FROM companies WHERE name = 'Examplea'").fetchone()
    if company is None:
        company_id = int(
            conn.execute(
                "INSERT INTO companies (name, source) VALUES ('Examplea', 'watcher')"
            ).lastrowid
            or 0
        )
    else:
        company_id = int(company["id"])
    metadata = None if tier is None else f'{{"tier": "{tier}"}}'
    cur = conn.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, gaps, llm_output, "
        "scout_metadata) VALUES (?, ?, '2026-10-01', 'watcher', '[]', '{}', ?)",
        (company_id, title, metadata),
    )
    return int(cur.lastrowid or 0)


def test_the_fill_in_maps_stored_tier_names_and_holds_unknown_ones(
    search: sqlite3.Connection,
) -> None:
    _scored_without_fit(search, "Waiting strong", "strong")
    _scored_without_fit(search, "Waiting possible", "possible")
    _scored_without_fit(search, "Waiting renamed", "renamed")
    alerted = _scored_without_fit(search, "Already alerted", "strong")
    search_store.mark_alerted(search, alerted, "2026-10-02T09:00:00Z")
    skipped = _scored_without_fit(search, "Skipped", "strong")
    search_store.insert_skip(search, skipped, "2026-10-02", "manual")
    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), config=FITS)
    assert "watch: fit filled in from config for 2 scored postings" in lines
    assert fit_of(search, "Waiting strong") == "high"
    assert fit_of(search, "Waiting possible") == "medium"
    assert fit_of(search, "Waiting renamed") is None  # held until the name is back in config
    assert fit_of(search, "Already alerted") is None
    assert fit_of(search, "Skipped") is None
    again = w.run(search, [], today=DAY, fetcher=Scripted({}), config=FITS)
    assert not any("fit filled in" in line for line in again)


def test_the_fill_in_runs_without_a_scorer_and_without_gap_tiers(
    search: sqlite3.Connection,
) -> None:
    _scored_without_fit(search, "Waiting", "strong")
    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), config={})
    assert not any("fit filled in" in line for line in lines)
    assert fit_of(search, "Waiting") is None
    w.run(search, [], today=DAY, fetcher=Scripted({}), config=FITS)
    assert fit_of(search, "Waiting") == "high"


def test_a_crash_in_the_fill_in_is_isolated(search: sqlite3.Connection, monkeypatch: Any) -> None:
    _scored_without_fit(search, "Waiting", "strong")

    def broken(conn: sqlite3.Connection, fits: Any) -> int:
        raise ValueError("synthetic")

    monkeypatch.setattr(search_store, "fill_missing_fits", broken)
    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), config=FITS)
    assert "watch: fit fill-in: ValueError; continuing" in lines
    assert lines[-1] == "watch: 0 boards fetched, 0 failed, 0 left"
    assert fit_of(search, "Waiting") is None


# --- first come, first served among never-scored postings (R1) --------------------------------


def _waiting(conn: sqlite3.Connection, title: str, created_at: str) -> int:
    """An unscored watcher posting stored at `created_at`."""
    posting_id = _scored_without_fit(conn, title, None)
    conn.execute(
        "UPDATE postings SET gaps = NULL, llm_output = NULL, created_at = ? WHERE id = ?",
        (created_at, posting_id),
    )
    return posting_id


def test_a_retry_goes_ahead_of_postings_stored_after_its_attempt(
    search: sqlite3.Connection,
) -> None:
    _waiting(search, "Stored before", "2026-11-01T08:00:00Z")
    retry = _waiting(search, "Retry", "2026-11-01T07:00:00Z")
    search_store.set_score_attempted_at(search, retry, "2026-11-01T09:00:00Z")
    _waiting(search, "Stored after", "2026-11-01T10:00:00Z")
    scorer = Recorder()
    w.run(search, [], today=DAY, fetcher=Scripted({}), scorer=scorer, config=FITS)
    assert titles(scorer) == ["Stored before", "Retry", "Stored after"]


def test_equal_stamps_keep_the_oldest_id_first(search: sqlite3.Connection) -> None:
    first = _waiting(search, "First", "2026-11-01T08:00:00Z")
    second = _waiting(search, "Second", "2026-11-01T09:00:00Z")
    search_store.set_score_attempted_at(search, second, "2026-11-01T08:00:00Z")
    assert first < second
    scorer = Recorder()
    w.run(search, [], today=DAY, fetcher=Scripted({}), scorer=scorer, config=FITS)
    assert titles(scorer) == ["First", "Second"]


def test_new_arrivals_every_run_cannot_push_a_retry_back_forever(
    search: sqlite3.Connection,
) -> None:
    """One model call fits in each run, and two new postings arrive before every run."""
    retry = _waiting(search, "Retry", "2026-11-01T07:00:00Z")
    search_store.set_score_attempted_at(search, retry, "2026-11-01T08:00:00Z")
    start = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)
    seen: list[str] = []
    for index in range(3):
        moment = start + dt.timedelta(hours=index)
        for n in range(2):
            stamp = (moment + dt.timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _waiting(search, f"Arrival {index}.{n}", stamp)
        clock = FakeClock()
        scorer = Recorder()
        scorer.clock, scorer.cost = clock, 700.0  # one call uses the whole budget
        w.run(
            search,
            [],
            today=DAY,
            fetcher=Scripted({}),
            scorer=scorer,
            budget=minutes(clock, 10),
            now=lambda m=moment: m + dt.timedelta(minutes=30),
            config=FITS,
        )
        seen += titles(scorer)
    assert seen[0] == "Retry"
    assert fit_of(search, "Retry") == "high"
    assert seen[1:] == ["Arrival 0.0", "Arrival 0.1"]  # then strictly oldest first


def test_a_retry_that_fails_again_waits_behind_what_arrived_before_it(
    search: sqlite3.Connection,
) -> None:
    retry = _waiting(search, "Retry", "2026-11-01T07:00:00Z")
    search_store.set_score_attempted_at(search, retry, "2026-11-01T08:00:00Z")
    _waiting(search, "Arrived at nine", "2026-11-01T09:00:00Z")
    scorer = Recorder()
    scorer.replies["Retry"] = None
    stamp = dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.UTC)
    w.run(
        search,
        [],
        today=DAY,
        fetcher=Scripted({}),
        scorer=scorer,
        now=lambda: stamp,
        config=FITS,
    )
    assert titles(scorer) == ["Retry", "Arrived at nine"]
    _waiting(search, "Arrived at eleven", "2026-11-01T11:00:00Z")
    scorer = Recorder()
    w.run(search, [], today=DAY, fetcher=Scripted({}), scorer=scorer, config=FITS)
    assert titles(scorer) == ["Retry", "Arrived at eleven"]  # its 10:00 stamp is older
