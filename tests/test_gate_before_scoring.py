"""Every posting is gated on its full description just before it is scored (ADR-027).

Boards, postings and descriptions here are made up; "Examplish" is a made-up
language. No socket is opened.
"""

from __future__ import annotations

import sqlite3

from careerhunter import search_store
from careerhunter import watch as w
from careerhunter.fetch import FetchError
from tests.test_fit import FITS, _waiting
from tests.test_rescore import Recorder, titles
from tests.test_watch import DAY, Scripted, fetched, url

GATED = {**FITS, "hard_gates": {"required_languages": ["Examplish", "bilingual"]}}


def _single(conn: sqlite3.Connection, title: str) -> str:
    """The Lever single-posting request for a posting `_waiting` stored."""
    (posting_id,) = conn.execute("SELECT id FROM postings WHERE title = ?", (title,)).fetchone()
    return f"https://api.lever.co/v0/postings/examplea/w{posting_id}?mode=json"


def _lever(description: str) -> object:
    return fetched({"id": "w1", "text": "Listed", "descriptionPlain": description})


def _skips(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT p.title, s.reason FROM skips s JOIN postings p ON p.id = s.posting_id"
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def test_a_stored_posting_whose_text_requires_an_avoided_language_is_skipped_unscored(
    search: sqlite3.Connection,
) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    _waiting(search, "Planner", "2026-11-01T08:00:00Z")
    fetcher = Scripted(
        {
            _single(search, "Coordinator"): _lever("Bilingual (English and Examplish) required."),
            _single(search, "Planner"): _lever("Plan synthetic deliveries."),
        }
    )
    scorer = Recorder()
    lines = w.run(search, [], today=DAY, fetcher=fetcher, scorer=scorer, config=GATED)
    assert titles(scorer) == ["Planner"]
    assert scorer.calls[0].description == "Plan synthetic deliveries."
    assert _skips(search) == {"Coordinator": "required_language"}
    assert "watch: Examplea: 'Coordinator' skipped (required_language)" in lines
    assert all("Examplish" not in line for line in lines)  # the text stays out of the log


def test_a_posting_that_cannot_be_read_again_is_not_scored_and_waits(
    search: sqlite3.Connection,
) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    fetcher = Scripted({_single(search, "Coordinator"): FetchError("synthetic")})
    scorer = Recorder()
    lines = w.run(search, [], today=DAY, fetcher=fetcher, scorer=scorer, config=GATED)
    assert scorer.calls == []
    assert _skips(search) == {}
    assert search_store.held_counts(search).unscored == 1
    assert (
        "watch: Examplea (lever): 'Coordinator': FetchError; "
        "description not read; left for the next run"
    ) in lines


def test_a_429_blocks_the_host_for_the_rest_of_the_pass(search: sqlite3.Connection) -> None:
    _waiting(search, "First", "2026-11-01T07:00:00Z")
    _waiting(search, "Second", "2026-11-01T08:00:00Z")
    fetcher = Scripted({_single(search, "First"): fetched({}, status=429)})
    scorer = Recorder()
    lines = w.run(search, [], today=DAY, fetcher=fetcher, scorer=scorer, config=GATED)
    assert scorer.calls == []
    assert fetcher.calls == [_single(search, "First")]  # Second is not requested
    assert any("'Second': api.lever.co already returned HTTP 429" in line for line in lines)
    assert search_store.held_counts(search).unscored == 2


def test_an_ashby_board_is_read_once_for_all_its_stored_postings(
    search: sqlite3.Connection,
) -> None:
    jobs = []
    for ident, title in (("j1", "Coordinator"), ("j2", "Planner")):
        _waiting(search, title, "2026-11-01T07:00:00Z")
        search.execute(
            "UPDATE postings SET url = ? WHERE title = ?",
            (f"https://jobs.ashbyhq.com/exampleorg/{ident}", title),
        )
        jobs.append(
            {
                "id": ident,
                "title": title,
                "jobUrl": f"https://jobs.ashbyhq.com/exampleorg/{ident}",
                "location": "Example City",
                "descriptionPlain": f"Synthetic {title} duties.",
            }
        )
    fetcher = Scripted({url("ashby", "exampleorg"): fetched({"jobs": jobs})})
    scorer = Recorder()
    w.run(search, [], today=DAY, fetcher=fetcher, scorer=scorer, config=GATED)
    assert sorted(titles(scorer)) == ["Coordinator", "Planner"]
    assert {text.description for text in scorer.calls} == {
        "Synthetic Coordinator duties.",
        "Synthetic Planner duties.",
    }
    assert fetcher.calls == [url("ashby", "exampleorg")]


def test_a_posting_gone_from_its_ashby_board_is_skipped_as_gone(
    search: sqlite3.Connection,
) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    search.execute("UPDATE postings SET url = 'https://jobs.ashbyhq.com/exampleorg/j9'")
    fetcher = Scripted({url("ashby", "exampleorg"): fetched({"jobs": []})})
    scorer = Recorder()
    w.run(search, [], today=DAY, fetcher=fetcher, scorer=scorer, config=GATED)
    assert scorer.calls == []
    assert _skips(search) == {"Coordinator": "gone"}


def test_a_posting_with_no_link_and_no_key_is_skipped_unscored(
    search: sqlite3.Connection,
) -> None:
    _waiting(search, "Coordinator", "2026-11-01T07:00:00Z")
    search.execute("UPDATE postings SET url = NULL, ats_key = NULL")
    scorer = Recorder()
    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), scorer=scorer, config=GATED)
    assert scorer.calls == []
    assert _skips(search) == {"Coordinator": "no_reference"}
    assert "watch: Examplea: 'Coordinator' skipped (no_reference)" in lines


def test_a_new_posting_is_gated_on_the_description_it_came_with_and_not_read_again(
    search: sqlite3.Connection,
) -> None:
    board = url("lever", "examplea")
    listed = [
        {
            "id": "n1",
            "text": "Coordinator",
            "hostedUrl": "https://jobs.lever.co/examplea/n1",
            "categories": {"location": "Example City"},
            "descriptionPlain": "Synthetic duties.",
        }
    ]
    fetcher = Scripted({board: fetched(listed)})
    scorer = Recorder()
    w.run(
        search,
        [{"name": "Examplea", "ats_type": "lever", "ats_slug": "examplea"}],
        today=DAY,
        fetcher=fetcher,
        scorer=scorer,
        config=GATED,
    )
    assert titles(scorer) == ["Coordinator"]
    assert fetcher.calls == [board]
