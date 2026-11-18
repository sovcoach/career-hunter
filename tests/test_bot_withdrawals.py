"""The bot's withdrawn note for alerted postings a criteria rule vetoed (ADR-028).

Synthetic companies and postings only. Telegram is a fake; no network.
"""

from __future__ import annotations

from careerhunter import db, search_store, telegram
from tests import test_bot
from tests.test_bot import Env, alerted

env = test_bot.env  # the bot fixture, shared with test_bot
REASON = "criteria: 2. No contract or term under 12"


def _withdraw(env: Env, title: str, company: str = "Example Robotics") -> None:
    """An alerted posting, then vetoed on a re-score: its skip and withdrawal rows."""
    posting_id = alerted(env, url=None, company=company, title=title)
    conn = db.connect(env.datadir.search_db_path)
    try:
        with db.transaction(conn):
            skip_id = search_store.insert_skip(conn, posting_id, "2026-11-02", REASON)
            search_store.insert_withdrawal(conn, skip_id)
    finally:
        conn.close()


def _withdrawn(env: Env) -> list[str]:
    return [t for t in env.api.texts() if t.startswith("withdrawn")]


def test_one_withdrawal_is_a_single_line(env: Env) -> None:
    _withdraw(env, "Synthetic Coordinator")
    env.bot.send_withdrawals()
    env.bot.send_withdrawals()
    assert _withdrawn(env) == [f"withdrawn: Example Robotics — Synthetic Coordinator, {REASON}"]


def test_several_go_in_one_message(env: Env) -> None:
    for n, company in enumerate(("Example Robotics", "Northwind Analytics", "Sample Freight")):
        _withdraw(env, f"Synthetic Planner {n}", company)
    env.bot.send_withdrawals()
    assert _withdrawn(env) == [
        "withdrawn (criteria):\n"
        f"Example Robotics — Synthetic Planner 0, {REASON}\n"
        f"Northwind Analytics — Synthetic Planner 1, {REASON}\n"
        f"Sample Freight — Synthetic Planner 2, {REASON}"
    ]


def test_twelve_go_as_ten_then_two(env: Env) -> None:
    for n in range(12):
        _withdraw(env, f"Synthetic Analyst {n:02d}")
    env.bot.send_withdrawals()
    env.bot.send_withdrawals()
    env.bot.send_withdrawals()
    first, second = _withdrawn(env)
    assert first.count("\n") == 10 and "Synthetic Analyst 09" in first
    assert "Synthetic Analyst 10" not in first
    assert second.count("\n") == 2 and "Synthetic Analyst 11" in second


def test_a_vetoed_posting_never_alerted_sends_nothing(env: Env) -> None:
    alerted(env, url=None, title="Synthetic Alerted")
    conn = db.connect(env.datadir.search_db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO postings (company_id, title, first_seen, source) "
                "VALUES (1, 'Synthetic Never Alerted', '2026-10-27', 'watcher')"
            )
            search_store.insert_skip(conn, 2, "2026-11-02", REASON)
    finally:
        conn.close()
    env.bot.send_withdrawals()
    assert _withdrawn(env) == []


def test_a_failed_send_goes_again_next_poll(env: Env) -> None:
    _withdraw(env, "Synthetic Coordinator")
    env.api.refuse["sendMessage"] = telegram.TelegramError("synthetic refusal")
    env.bot.send_withdrawals()
    assert _withdrawn(env) == [] and len(env.api.refused) == 1
    del env.api.refuse["sendMessage"]
    conn = env.search()
    try:
        assert conn.execute("SELECT sent_at FROM withdrawals").fetchone()[0] is None
    finally:
        conn.close()
    env.bot.send_withdrawals()
    assert len(_withdrawn(env)) == 1
    conn = env.search()
    try:
        assert conn.execute("SELECT sent_at FROM withdrawals").fetchone()[0] is not None
    finally:
        conn.close()


def test_nothing_is_sent_while_the_config_cannot_be_read(env: Env) -> None:
    _withdraw(env, "Synthetic Coordinator")
    good = env.datadir.config_path.read_text()
    env.datadir.config_path.write_text("search: [unclosed\n")
    env.bot.send_withdrawals()
    assert _withdrawn(env) == []
    env.datadir.config_path.write_text(good)
    env.bot.send_withdrawals()
    assert len(_withdrawn(env)) == 1
