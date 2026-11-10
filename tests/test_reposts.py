"""Repost suppression (#34): a role the operator logged or skipped is never alerted again.

Synthetic boards and companies only; no sockets.
"""

from __future__ import annotations

import sqlite3

import pytest

from careerhunter import ledger, search_store
from careerhunter import watch as w
from careerhunter.filters import title_key
from careerhunter.models import Application, Dating, Posting
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

SLUG = "examplewidgets"
BOARD = company("Example Widgets", "greenhouse", SLUG)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Analyst (R12345)", "Analyst (R67890)"),
        ("Analyst [JR-2041]", "analyst"),
        ("Analyst - 104332", "Analyst"),  # six digits: still a code, still stripped
        ("Analyst - 2026-104332", "Analyst"),  # a code that starts with a year
        ("Analyst - 3026", "Analyst"),  # not 19xx or 20xx: a code
        ("Analyst \u2013 R12345", "ANALYST"),  # an en dash
        ("Analyst, R12345", "Analyst"),
        ("Analyst #12345", "Analyst"),
        ("Ops Lead (R123) - JR-99999", "Ops Lead"),
        ("Data  Analyst", "data analyst"),
        ("\uff24ata Analyst", "Data Analyst"),  # NFKC: a full-width letter
    ],
)
def test_title_keys_that_match(left: str, right: str) -> None:
    assert title_key(left) == title_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Analyst (Level 2)", "Analyst (Level 3)"),
        ("Analyst (Remote)", "Analyst"),
        ("Analyst (R12)", "Analyst"),  # two digits: not a requisition code
        ("Analyst - Team A", "Analyst"),  # a space: not one token
        ("Analyst Team-12345", "Analyst Team"),  # no space before the hyphen
        ("Analyst (Ops 12345)", "Analyst"),  # a space inside the brackets
        ("Analyst - 2026", "Analyst - 2027"),  # a trailing year is not a requisition code
        ("Analyst - 2026", "Analyst"),
        ("Analyst, 1999", "Analyst, 2001"),
    ],
)
def test_title_keys_that_do_not_match(left: str, right: str) -> None:
    assert title_key(left) != title_key(right)


def _board(title: str, ident: int) -> Scripted:
    return Scripted({url("greenhouse", SLUG): fetched(one_greenhouse(SLUG, title, ident))})


def _run(search: sqlite3.Connection, title: str, ident: int) -> list[str]:
    return w.run(search, [BOARD], today=DAY, fetcher=_board(title, ident))


def _posting(search: sqlite3.Connection, ident: int) -> sqlite3.Row:
    row = search.execute(
        "SELECT * FROM postings WHERE url = ?",
        (f"https://boards.greenhouse.io/{SLUG}/jobs/{ident}",),
    ).fetchone()
    assert row is not None
    return row


def _skips(search: sqlite3.Connection, ident: int) -> list[tuple[str, int]]:
    posting_id = int(_posting(search, ident)["id"])
    rows = search.execute(
        "SELECT reason, by_operator FROM skips WHERE posting_id = ? ORDER BY id", (posting_id,)
    )
    return [(str(r["reason"]), int(r["by_operator"])) for r in rows]


def _operator_skip(search: sqlite3.Connection, ident: int) -> None:
    posting = search_store.posting_by_id(search, int(_posting(search, ident)["id"]))
    assert posting is not None
    ledger.record_skip(search, posting, DAY, "not a fit")


def test_a_repost_with_another_requisition_id_is_skipped_as_already_handled(
    search: sqlite3.Connection,
) -> None:
    _run(search, "Analyst (R12345)", 1001)
    _operator_skip(search, 1001)
    lines = _run(search, "Analyst (R67890)", 1002)
    repost = _posting(search, 1002)
    assert repost["requisition_id"] == "REQ-1002"  # a different requisition id, and url
    assert _skips(search, 1002) == [("already_handled", 0)]
    assert "watch: Example Widgets (greenhouse): 'Analyst (R67890)' skipped (already_handled)" in (
        lines
    )


def test_a_different_level_is_a_different_role(search: sqlite3.Connection) -> None:
    _run(search, "Analyst (Level 2)", 1001)
    _operator_skip(search, 1001)
    _run(search, "Analyst (Level 3)", 1002)
    assert _skips(search, 1002) == []


def test_a_watcher_skip_does_not_suppress_a_repost(search: sqlite3.Connection) -> None:
    _run(search, "Analyst", 1001)
    posting_id = int(_posting(search, 1001)["id"])
    with search:
        search_store.insert_skip(search, posting_id, DAY, "seniority")  # the watcher's own
    _run(search, "Analyst", 1002)
    assert _skips(search, 1002) == []


def test_a_logged_application_suppresses_its_reposts(search: sqlite3.Connection) -> None:
    logged = Posting(
        company="Example Widgets",
        title="Analyst - 104332",
        first_seen=DAY,
        url="https://careers.example.com/jobs/104332",
        source="self_sourced",
    )
    ledger.record_application(
        search, None, Application(logged, Dating("exact", day=DAY), "company_site")
    )
    _run(search, "Analyst (R12345)", 1001)
    assert _skips(search, 1001) == [("already_handled", 0)]


def test_a_posting_stored_before_the_skip_is_held_then_skipped(
    search: sqlite3.Connection,
) -> None:
    """Two copies of one role are stored; the operator skips one. The other is never in the
    digest, and the next run gives it its skip."""
    _run(search, "Analyst (R12345)", 1001)
    _run(search, "Analyst (R67890)", 1002)
    first, second = (int(_posting(search, i)["id"]) for i in (1001, 1002))
    with search:
        for posting_id in (first, second):
            search.execute(
                "UPDATE postings SET llm_output = '{}', gaps = '[]', fit = 'high' WHERE id = ?",
                (posting_id,),
            )
    assert search_store.pending_alert_ids(search) == [first, second]
    _operator_skip(search, 1001)
    assert search_store.pending_alert_ids(search) == []  # held before the next run
    lines = _run(search, "Analyst (R67890)", 1002)
    assert _skips(search, 1002) == [("already_handled", 0)]
    assert "watch: Example Widgets: 'Analyst (R67890)' skipped (already_handled)" in lines
    _run(search, "Analyst (R67890)", 1002)
    assert _skips(search, 1002) == [("already_handled", 0)]  # safe to run twice


def test_a_posting_with_an_application_is_never_pending(search: sqlite3.Connection) -> None:
    _run(search, "Analyst", 1001)
    row = _posting(search, 1001)
    with search:
        search.execute(
            "UPDATE postings SET llm_output = '{}', gaps = '[]', fit = 'high' WHERE id = ?",
            (row["id"],),
        )
    assert search_store.pending_alert_ids(search) == [row["id"]]
    posting = search_store.posting_by_id(search, int(row["id"]))
    assert posting is not None
    ledger.record_application(
        search, None, Application(posting, Dating("exact", day=DAY), "company_site")
    )
    assert search_store.pending_alert_ids(search) == []


def test_another_company_with_the_same_title_is_not_a_repost(
    search: sqlite3.Connection,
) -> None:
    _run(search, "Analyst", 1001)
    _operator_skip(search, 1001)
    other = company("Example Gadgets", "greenhouse", "examplegadgets")
    w.run(
        search,
        [other],
        today=DAY,
        fetcher=Scripted(
            {
                url("greenhouse", "examplegadgets"): fetched(
                    one_greenhouse("examplegadgets", "Analyst", 2001)
                )
            }
        ),
    )
    row = search.execute(
        "SELECT count(*) FROM skips s JOIN postings p ON p.id = s.posting_id "
        "WHERE p.url LIKE '%examplegadgets%'"
    ).fetchone()
    assert row[0] == 0
