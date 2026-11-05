"""Lanes on stored postings (#33): set once at insert, filled in once, and counted.

Boards, lanes, titles and model replies here are all made up. No socket is opened and
nothing sleeps.
"""

from __future__ import annotations

import copy
import sqlite3
from typing import Any

from careerhunter import watch as w
from careerhunter.score import Score
from tests.test_fit import FITS
from tests.test_rescore import Recorder, board, lever, run, titles
from tests.test_watch import DAY, Scripted

LANES = [
    {"name": "operations", "priority": 1, "titles": ["Operations Coordinator"]},
    {
        "name": "support",
        "priority": 2,
        "fallback": True,
        "titles": [["Support Specialist", "Help Desk Specialist"]],
    },
]


def with_lanes(**search: Any) -> dict[str, Any]:
    doc = copy.deepcopy(FITS)
    doc["search"] = {"lanes": copy.deepcopy(LANES)} | search
    return doc


def lanes_of(conn: sqlite3.Connection) -> dict[str, tuple[object, object]]:
    rows = conn.execute("SELECT title, lane, lane_checked FROM postings ORDER BY id")
    return {row["title"]: (row["lane"], row["lane_checked"]) for row in rows}


JOBS = (
    ("a1", "Operations Coordinator"),
    ("a2", "Help Desk Specialist II"),
    ("a3", "Warehouse Associate"),
)
BOARDS = {board("examplea"): lever("examplea", *JOBS)}


def test_a_new_posting_gets_its_lane_when_it_is_stored(search: sqlite3.Connection) -> None:
    run(search, BOARDS, ("examplea",), config=with_lanes())
    assert lanes_of(search) == {
        "Operations Coordinator": ("operations", 1),
        "Help Desk Specialist II": ("support", 1),
        "Warehouse Associate": (None, 1),  # off-lane: checked, no lane
    }


def test_a_skipped_posting_gets_a_lane_too(search: sqlite3.Connection) -> None:
    exclusions = {"roles": [{"company": "Examplea", "title": "Operations Coordinator"}]}
    lines = run(search, BOARDS, ("examplea",), config=with_lanes(), exclusions=exclusions)
    assert any("'Operations Coordinator' skipped" in line for line in lines)
    assert lanes_of(search)["Operations Coordinator"] == ("operations", 1)


def test_an_off_lane_posting_is_still_gated_and_scored(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    run(search, BOARDS, ("examplea",), config=with_lanes(), scorer=scorer)
    assert "Warehouse Associate" in titles(scorer)
    fit = search.execute("SELECT fit FROM postings WHERE title = 'Warehouse Associate'")
    assert fit.fetchone()[0] == "high"


def test_without_lanes_nothing_is_checked_and_adding_them_fills_in_once(
    search: sqlite3.Connection,
) -> None:
    run(search, BOARDS, ("examplea",), config=FITS)
    assert set(lanes_of(search).values()) == {(None, 0)}
    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), config=with_lanes())
    assert "watch: lanes filled in from config for 3 postings" in lines
    assert lanes_of(search) == {
        "Operations Coordinator": ("operations", 1),
        "Help Desk Specialist II": ("support", 1),
        "Warehouse Associate": (None, 1),
    }
    again = w.run(search, [], today=DAY, fetcher=Scripted({}), config=with_lanes())
    assert not any("lanes filled in" in line for line in again)


def test_the_fill_in_reaches_every_source_and_skipped_rows(search: sqlite3.Connection) -> None:
    company = search.execute(
        "INSERT INTO companies (name, source) VALUES ('Example Org', 'manual')"
    ).lastrowid
    for title, source in (
        ("Operations Coordinator", "self_sourced"),
        ("Support Specialist", "grok"),
    ):
        search.execute(
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (?, ?, '2026-10-01', ?)",
            (company, title, source),
        )
    search.execute("INSERT INTO skips (posting_id, date, reason) VALUES (2, '2026-10-01', 'x')")
    w.run(search, [], today=DAY, fetcher=Scripted({}), config=with_lanes())
    assert lanes_of(search) == {
        "Operations Coordinator": ("operations", 1),
        "Support Specialist": ("support", 1),
    }


def test_a_stored_lane_does_not_move_when_the_lanes_change(search: sqlite3.Connection) -> None:
    run(search, BOARDS, ("examplea",), config=with_lanes())
    changed = with_lanes()
    changed["search"]["lanes"] = [
        {"name": "warehouse", "priority": 1, "titles": ["Warehouse Associate"]},
    ]
    run(search, BOARDS, ("examplea",), config=changed)
    assert lanes_of(search)["Warehouse Associate"] == (None, 1)
    assert lanes_of(search)["Operations Coordinator"] == ("operations", 1)


def test_a_crash_in_the_lane_fill_in_is_isolated(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    run(search, BOARDS, ("examplea",), config=FITS)

    def broken(conn: sqlite3.Connection) -> list[tuple[int, str]]:
        raise ValueError("synthetic")

    monkeypatch.setattr(w.search_store, "unchecked_lane_titles", broken)
    lines = w.run(search, [], today=DAY, fetcher=Scripted({}), config=with_lanes())
    assert "watch: lane fill-in: ValueError; continuing" in lines
    assert set(lanes_of(search).values()) == {(None, 0)}


def _held_off_lane(search: sqlite3.Connection) -> int:
    row = search.execute("SELECT held_off_lane FROM watch_runs ORDER BY id DESC LIMIT 1")
    return int(row.fetchone()[0])


def test_off_lane_postings_below_the_threshold_are_counted_as_held(
    search: sqlite3.Connection,
) -> None:
    scorer = Recorder()
    scorer.replies["Warehouse Associate"] = Score(llm_output="{}", gaps="[]", tier="possible")
    run(search, BOARDS, ("examplea",), config=with_lanes(), scorer=scorer)
    assert _held_off_lane(search) == 1  # medium, below the default high
    w.run(search, [], today=DAY, fetcher=Scripted({}), config=with_lanes(off_lane_min_fit="medium"))
    assert _held_off_lane(search) == 0
    w.run(search, [], today=DAY, fetcher=Scripted({}), config=FITS)  # lanes off: none held
    assert _held_off_lane(search) == 0
    search.execute("UPDATE postings SET alerted_at = '2026-11-01T09:00:00Z'")
    w.run(search, [], today=DAY, fetcher=Scripted({}), config=with_lanes())
    assert _held_off_lane(search) == 0  # an alerted posting is not held
