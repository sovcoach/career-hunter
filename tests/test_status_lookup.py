"""`/status` for older applications (Batch R): a company or title in place of the ref,
a picker that pages through every application, and a helpful reply to a line it
can't parse. Synthetic only."""

from __future__ import annotations

from typing import Any

from careerhunter import bot as botmod
from careerhunter import db
from tests import test_bot
from tests.test_bot import Env

env = test_bot.env


def _log(env: Env, n: int, company: str, title: str) -> None:
    env.say(f"/log https://jobs.example.com/st/{n} | email | 2026-10-21 | {company} | {title}")


def _statuses(env: Env) -> list[tuple[Any, ...]]:
    conn = db.connect_read_only(env.datadir.search_db_path)
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT c.name, p.title, d.status, d.status_date FROM decision_history d "
                "JOIN applications a ON a.id = d.application_id "
                "JOIN postings p ON p.id = a.posting_id JOIN companies c ON c.id = p.company_id "
                "WHERE d.status != 'applied' ORDER BY d.id"
            )
        ]
    finally:
        conn.close()


def _labels(env: Env) -> list[str]:
    params = env.api.sent()[-1][1]
    return [b["text"] for row in params["reply_markup"]["inline_keyboard"] for b in row]


def test_the_picker_pages_through_every_application(env: Env) -> None:
    for n in range(12):
        _log(env, n, "Example Robotics", f"Analyst {n}")
    env.say("/status")
    assert env.api.texts()[-1].startswith("Which application? (1 to 10 of 12;")
    labels = _labels(env)
    assert len(labels) == 11 and labels[-1] == "More…"
    assert labels[0].endswith("Example Robotics — Analyst 11")  # newest first
    env.press("More…")
    assert env.api.texts()[-1].startswith("Which application? (11 to 12 of 12;")
    assert [label.split(" — ")[1] for label in _labels(env)] == ["Analyst 1", "Analyst 0"]


def test_company_and_title_words_with_a_status_record_it(env: Env) -> None:
    _log(env, 1, "Example Robotics", "Synthetic Analyst")
    _log(env, 2, "Sample Logistics", "Synthetic Clerk")
    env.say("/status sample logistics clerk rejected 2026-10-27")
    assert _statuses(env) == [("Sample Logistics", "Synthetic Clerk", "rejected", "2026-10-27")]


def test_words_without_a_status_show_the_matches_and_the_status_words(env: Env) -> None:
    """The case that showed it: more words than the stored title, and "position
    filled", which is not a status."""
    _log(env, 1, "Example Robotics", "Technical Support Specialist")
    _log(env, 2, "Sample Logistics", "Synthetic Clerk")
    env.say(
        "/status example robotics Client Technical Support Specialist 2026-10-27: position filled"
    )
    text = env.api.texts()[-1]
    assert text.startswith("Which application? The statuses are: screening, ")
    assert "position closed" in text
    assert _labels(env)[0].split(" ", 1)[1] == "Example Robotics — Technical Support Specialist"
    assert _statuses(env) == []
    env.press(_labels(env)[0])
    env.press("position closed")  # the date given with the words is kept
    assert _statuses(env) == [
        ("Example Robotics", "Technical Support Specialist", "position_closed", "2026-10-27")
    ]


def test_a_status_for_words_that_match_several_asks_which(env: Env) -> None:
    _log(env, 1, "Example Robotics", "Synthetic Analyst")
    _log(env, 2, "Example Robotics", "Synthetic Planner")
    env.say("/status example robotics position closed 2026-10-27")
    assert env.api.texts()[-1] == "Which application gets position closed?"
    env.press(_labels(env)[1])  # the older one
    assert _statuses(env) == [
        ("Example Robotics", "Synthetic Analyst", "position_closed", "2026-10-27")
    ]


def test_words_that_match_nothing_say_so_with_the_status_words(env: Env) -> None:
    _log(env, 1, "Example Robotics", "Synthetic Analyst")
    env.say("/status nothing like it offer")
    assert env.api.texts()[-1] == (
        "No application matches those words. Send /status to choose, or "
        "/status <ref> <status>. " + botmod.STATUS_WORDS
    )


def test_an_unknown_reference_still_says_so(env: Env) -> None:
    _log(env, 1, "Example Robotics", "Synthetic Analyst")
    env.say("/status a99 offer")
    assert env.api.texts()[-1].startswith("I don't know that reference.")
