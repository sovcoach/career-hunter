"""A discovery candidate carries the postings found on it that passed the gates (search
migration 0028). Synthetic boards, companies and postings; no sockets."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from careerhunter import discover
from careerhunter.ats import Board, Confirmed
from tests.test_discovery_postings import LEVER, LEVER_API, lever_posting, run
from tests.test_watch import Scripted, fetched


def _pending(
    conn: sqlite3.Connection, slug: str = "freshlogistics", status: str = "pending"
) -> int:
    cur = conn.execute(
        "INSERT INTO discovery_candidates (kind, url, ats_type, ats_slug, suggested_name, "
        "status, first_seen) VALUES ('board', ?, 'lever', ?, ?, ?, '2026-11-01')",
        (f"https://jobs.lever.co/{slug}", slug, slug, status),
    )
    return int(cur.lastrowid or 0)


def _links(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT l.candidate_id, p.title FROM discovery_candidate_postings l "
        "JOIN postings p ON p.id = l.posting_id ORDER BY l.posting_id"
    )
    return [(int(r[0]), str(r[1])) for r in rows]


def test_a_posting_that_passes_the_gates_is_linked_to_its_pending_board(
    search: sqlite3.Connection,
) -> None:
    candidate = _pending(search)
    lines = run(search, Scripted({LEVER_API: fetched(lever_posting())}), LEVER)
    assert _links(search) == [(candidate, "Operations Coordinator")]
    assert "discover: 1 postings that passed the gates linked to candidates" in lines


def test_a_skipped_posting_is_not_linked(search: sqlite3.Connection) -> None:
    _pending(search)
    senior = Scripted({LEVER_API: fetched(lever_posting("Senior Operations Coordinator"))})
    config = {"hard_gates": {"seniority_words": ["Senior"]}}
    run(search, senior, LEVER, config=config)
    assert _links(search) == []


def test_a_decided_board_gets_no_links(search: sqlite3.Connection) -> None:
    _pending(search, status="rejected")
    run(search, Scripted({LEVER_API: fetched(lever_posting())}), LEVER)
    assert _links(search) == []


def test_a_stored_posting_found_again_is_linked_once(search: sqlite3.Connection) -> None:
    candidate = _pending(search)
    run(search, Scripted({LEVER_API: fetched(lever_posting())}), LEVER)
    run(search, Scripted({}), LEVER)  # stored already: not fetched, still linked once
    assert _links(search) == [(candidate, "Operations Coordinator")]


def test_a_confirmed_board_keeps_the_name_its_reply_gave(search: sqlite3.Connection) -> None:
    body = json.dumps({"results": [{"url": "https://jobs.lever.co/freshlogistics/abc"}]})
    config = {
        "discovery": {"searxng_url": "http://127.0.0.1:8888/search", "queries_per_run": 1},
        "search": {"target_titles": ["One"]},
    }

    def confirm(board: Board) -> Confirmed:
        return Confirmed(True, ("  ", "Fresh Logistics Ltd."))

    def transport(url: str) -> Any:
        return 200, body.encode()

    discover.run(search, config, today="2026-11-02", transport=transport, confirm=confirm)
    row = search.execute("SELECT suggested_name, display_name FROM discovery_candidates").fetchone()
    assert tuple(row) == ("freshlogistics", "Fresh Logistics Ltd.")
