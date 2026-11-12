"""The bot's health messages (#42): one failure and one recovery message per outage.

Telegram is a fake, the clock is fixed, nothing sleeps, and the data is synthetic.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from careerhunter import db, search_store, telegram
from tests import test_bot
from tests.test_bot import Env

env = test_bot.env  # the bot fixture, shared with test_bot
STAMP = "2026-10-28T15:00:00Z"


def _record(env: Env, component: str, failed: str | None) -> None:
    """One watch run's outcome, as the watcher records it."""
    conn = db.connect(env.datadir.search_db_path)
    try:
        db.migrate(conn, "search")
        with db.transaction(conn):
            search_store.record_component(conn, component, failed=failed, stamp=STAMP)
    finally:
        conn.close()


def _health_texts(env: Env) -> list[str]:
    return [text for text in env.api.texts() if text.startswith("Health:")]


def _fail(env: Env, component: str, runs: int, kind: str = "unreachable") -> None:
    for _ in range(runs):
        _record(env, component, kind)


def test_one_failure_message_after_the_threshold_and_no_more(env: Env) -> None:
    _fail(env, "lm_studio", 2)
    env.bot.send_health()
    assert _health_texts(env) == []  # below health.failure_runs (3)
    _fail(env, "lm_studio", 1)
    env.bot.send_health()
    env.bot.send_health()
    _fail(env, "lm_studio", 2)  # the outage goes on
    env.bot.send_health()
    assert _health_texts(env) == [
        "Health: LM Studio has failed 3 runs in a row. 0 postings are waiting to be scored."
    ]


def test_one_recovery_message_when_it_works_again(env: Env) -> None:
    _fail(env, "searxng", 3, "bad-reply")
    env.bot.send_health()
    _record(env, "searxng", None)
    env.bot.send_health()
    env.bot.send_health()
    _record(env, "searxng", None)
    env.bot.send_health()
    assert _health_texts(env) == [
        "Health: SearXNG has failed 3 runs in a row. 0 postings are waiting to be scored.",
        "Health: SearXNG is working again.",
    ]


def test_a_second_outage_gets_its_own_pair(env: Env) -> None:
    for _ in range(2):
        _fail(env, "boards", 3, "failed")
        env.bot.send_health()
        _record(env, "boards", None)
        env.bot.send_health()
    assert (
        _health_texts(env)
        == [
            "Health: Every board has failed 3 runs in a row. 0 postings are waiting to be scored.",
            "Health: Every board is working again.",
        ]
        * 2
    )


def test_a_short_outage_with_no_message_has_no_recovery_either(env: Env) -> None:
    _fail(env, "lm_studio", 2)
    _record(env, "lm_studio", None)
    env.bot.send_health()
    assert _health_texts(env) == []


def test_the_message_counts_the_postings_held_for_scoring(env: Env) -> None:
    for title in ("Coordinator", "Planner", "Clerk", "Analyst"):
        test_bot._insert(
            env, "Example Org", title, f"https://jobs.example.com/ads/{title}", scored=False
        )
    _fail(env, "lm_studio", 3)
    env.bot.send_health()
    (text,) = _health_texts(env)
    assert (
        text == "Health: LM Studio has failed 3 runs in a row. 4 postings are waiting to be scored."
    )
    for leak in ("127.0.0.1", "localhost", ":1234", str(env.datadir.root), "/"):
        assert leak not in text


def test_the_threshold_comes_from_config(env: Env) -> None:
    path = env.datadir.config_path
    doc = yaml.safe_load(path.read_text())
    doc["health"] = {"failure_runs": 1}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    _fail(env, "searxng", 1, "throttled")
    env.bot.send_health()
    assert _health_texts(env) == [
        "Health: SearXNG has failed 1 run in a row. 0 postings are waiting to be scored."
    ]


def test_nothing_is_sent_while_config_is_unreadable(env: Env) -> None:
    _fail(env, "lm_studio", 3)
    env.datadir.config_path.write_text("search: [unclosed\n")
    env.bot.send_health()
    assert _health_texts(env) == []
    env.datadir.config_path.write_text((test_bot.ROOT / "config.example.yaml").read_text())
    test_bot.set_claims(env.datadir.root, test_bot.ACTIVE.label, test_bot.ACTIVE)
    test_bot.write_config(env.datadir)  # readable again, as the fixture wrote it
    env.bot.send_health()
    assert len(_health_texts(env)) == 1  # sent once config can be read again


@pytest.mark.parametrize(
    "error",
    [
        telegram.TelegramUnreachable("sendMessage: TimeoutError"),
        telegram.TelegramRejected("sendMessage: Bad Request"),
    ],
)
def test_a_message_that_fails_is_sent_again_next_poll(env: Env, error: Exception) -> None:
    _fail(env, "lm_studio", 3)
    real = env.api.call
    failures = [error]

    def flaky(method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "sendMessage" and failures:
            raise failures.pop()
        return real(method, params)

    env.api.call = flaky  # type: ignore[method-assign]
    env.bot.send_health()
    assert _health_texts(env) == []
    env.bot.send_health()
    assert len(_health_texts(env)) == 1


def test_a_recovery_between_the_read_and_the_mark_is_not_lost(env: Env) -> None:
    """The bot read 3 failures and sent the message; the watcher saw it work before the
    bot marked it. The outage is over, so the recovery message is due, not stuck."""
    _fail(env, "lm_studio", 3)
    _record(env, "lm_studio", None)
    conn = db.connect(env.datadir.search_db_path)
    try:
        with db.transaction(conn):
            search_store.mark_failure_told(conn, "lm_studio", 3, 3)
        (health,) = [h for h in search_store.component_health(conn) if h.component == "lm_studio"]
    finally:
        conn.close()
    assert (health.alerted_failures, health.recovery_due) == (None, True)
    env.bot.send_health()
    assert _health_texts(env) == ["Health: LM Studio is working again."]


def test_an_unauthorized_outage_says_so(env: Env) -> None:
    _fail(env, "lm_studio", 3, "unauthorized")
    env.bot.send_health()
    (text,) = _health_texts(env)
    assert text == (
        "Health: LM Studio has failed 3 runs in a row: unauthorized, it refused the API "
        "token (run: careerhunter secret set lm_studio_token). "
        "0 postings are waiting to be scored."
    )
    assert "unreachable" not in text
