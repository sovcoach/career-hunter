"""`/skip <link>` on a link career-hunter doesn't have yet (Batch R): the posting is
stored first, self-sourced, then skipped with the usual reasons. No EI entry.
Synthetic only."""

from __future__ import annotations

import sqlite3

from careerhunter import db
from tests import test_bot
from tests.test_bot import AD_URL, SHELL_URL, Env, alerted

env = test_bot.env

LINKEDIN = "https://www.linkedin.com/jobs/view/1234567890"


def _postings(env: Env) -> list[sqlite3.Row]:
    conn = db.connect_read_only(env.datadir.search_db_path)
    try:
        return conn.execute(
            "SELECT c.name AS company, p.title, p.location, p.url, p.source, "
            "s.reason, s.by_operator FROM postings p JOIN companies c ON c.id = p.company_id "
            "LEFT JOIN skips s ON s.posting_id = p.id ORDER BY p.id"
        ).fetchall()
    finally:
        conn.close()


def test_an_unknown_link_is_stored_from_its_page_then_skipped(env: Env) -> None:
    env.say(f"/skip {AD_URL}?utm_source=synthetic")
    assert env.api.texts()[-1] == "Why skip it?"
    env.press("Not a fit")
    (row,) = _postings(env)
    assert tuple(row) == (
        "Example Robotics",
        "Synthetic Analyst",
        "Example City",
        AD_URL,  # stored canonically, like /log
        "self_sourced",
        "not a fit",
        1,
    )
    assert env.api.texts()[-1] == "Skipped: Example Robotics — Synthetic Analyst."
    assert test_bot.ei_apps(env) == []  # no EI entry for a skip


def test_a_page_without_names_asks_for_them(env: Env) -> None:
    env.say(f"/skip {SHELL_URL}")
    assert env.api.texts()[-1].endswith("Send: /skip <link> | Company | Title")
    assert env.counts()["search.postings"] == 0
    env.say(f"/skip {SHELL_URL} | Sample Logistics | Synthetic Clerk")
    env.press("Location")
    (row,) = _postings(env)
    assert (row["company"], row["title"], row["reason"]) == (
        "Sample Logistics",
        "Synthetic Clerk",
        "location",
    )


def test_a_linkedin_link_needs_the_names_and_is_never_fetched(env: Env) -> None:
    env.say(f"/skip {LINKEDIN}")
    assert "LinkedIn links are never fetched" in env.api.texts()[-1]
    calls = len(env.web.calls)
    env.say(f"/skip {LINKEDIN} | Example Robotics | Synthetic Analyst")
    env.press("Seniority")
    assert len(env.web.calls) == calls
    (row,) = _postings(env)
    assert (row["source"], row["reason"]) == ("self_sourced", "seniority")


def test_a_skip_of_a_known_link_is_unchanged(env: Env) -> None:
    alerted(env)
    env.say(f"/skip {AD_URL}")
    env.press("Pay")
    (row,) = _postings(env)
    assert (row["source"], row["reason"]) == ("watcher", "pay")


def test_one_name_alone_is_refused(env: Env) -> None:
    env.say(f"/skip {SHELL_URL} | Sample Logistics")
    assert env.api.texts()[-1] == "Send /skip <link>, or /skip <link> | Company | Title."
    assert env.counts()["search.postings"] == 0


def test_the_same_link_later_is_the_same_posting(env: Env) -> None:
    env.say(f"/skip {AD_URL}")
    env.press("Not a fit")
    env.say(f"/skip {AD_URL}")
    assert env.api.texts()[-1] == "Already skipped."
    assert env.counts()["search.postings"] == 1
