"""The weekly digest by lane (#43): counts only, sent at a configured local day and hour.

Telegram is a fake, the clock is fixed, nothing sleeps, and the data is synthetic.
"""

from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
import zoneinfo
from typing import Any

import pytest
import yaml

from careerhunter import bot as botmod
from careerhunter import db, telegram
from tests import test_bot
from tests.test_bot import Env

env = test_bot.env  # the bot fixture, shared with test_bot
# test_bot's clock is Wednesday 2026-10-28 16:00 UTC: 21:30 at UTC+05:30.
SCHEDULED = "2026-10-28T03:30:00Z"  # Wednesday 09:00 at UTC+05:30
IN_WEEK = "2026-10-25T12:00:00Z"
BEFORE = "2026-10-21T03:29:59Z"  # one second before the week starts
AFTER = "2026-10-28T03:30:00Z"  # the next week's first second


def _configure(env: Env, *, zone: str = "UTC+05:30", weekly: Any = None) -> None:
    path = env.datadir.config_path
    doc = yaml.safe_load(path.read_text())
    doc["schedule"]["timezone"] = zone
    doc.setdefault("alerts", {})["weekly_digest"] = (
        {"day": "wednesday", "hour": 9} if weekly is None else weekly
    )
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _db(env: Env) -> sqlite3.Connection:
    conn = db.connect(env.datadir.search_db_path)
    db.migrate(conn, "search")
    return conn


def _posting(conn: sqlite3.Connection, title: str, lane: str | None, checked: int = 1) -> int:
    company = conn.execute(
        "INSERT INTO companies (name, source) VALUES (?, 'watcher') "
        "ON CONFLICT (name) DO UPDATE SET name = excluded.name RETURNING id",
        ("Example Org",),
    ).fetchone()[0]
    return int(
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source, lane, lane_checked, "
            "gaps, llm_output) VALUES (?, ?, '2026-10-20', 'watcher', ?, ?, ?, ?)",
            (company, title, lane, checked, '[{"name": "SENTINEL-gap"}]', "SENTINEL-llm"),
        ).lastrowid
        or 0
    )


def _application(conn: sqlite3.Connection, posting_id: int, *statuses: tuple[str, str]) -> None:
    app_id = conn.execute(
        "INSERT INTO applications (correlation_id, posting_id, date_applied, date_precision, "
        "method, notes) VALUES (?, ?, '2026-10-22', 'exact', 'email', 'SENTINEL-note')",
        (secrets.token_hex(16), posting_id),
    ).lastrowid
    for status, at in statuses:
        conn.execute(
            "INSERT INTO decision_history (application_id, status, recorded_at, note) "
            "VALUES (?, ?, ?, 'SENTINEL-note')",
            (app_id, status, at),
        )


def _skip(conn: sqlite3.Connection, posting_id: int, at: str, *, by_operator: int = 1) -> None:
    conn.execute(
        "INSERT INTO skips (posting_id, date, reason, by_operator, created_at) "
        "VALUES (?, '2026-10-25', 'SENTINEL-reason', ?, ?)",
        (posting_id, by_operator, at),
    )


def _weekly(env: Env) -> list[str]:
    return [t for t in env.api.texts() if t.startswith("Weekly digest")]


def test_counts_by_lane_are_correct_and_carry_nothing_else(env: Env) -> None:
    _configure(env)
    conn = _db(env)
    try:
        ops = [_posting(conn, f"SENTINEL-title-{n}", "operations") for n in range(4)]
        off = _posting(conn, "SENTINEL-title-off", None)
        unchecked = _posting(conn, "SENTINEL-title-self", None, checked=0)
        for posting_id in (ops[0], ops[1], off):
            conn.execute("UPDATE postings SET alerted_at = ? WHERE id = ?", (IN_WEEK, posting_id))
        conn.execute("UPDATE postings SET alerted_at = ? WHERE id = ?", (BEFORE, ops[2]))
        conn.execute("UPDATE postings SET alerted_at = ? WHERE id = ?", (AFTER, ops[3]))
        # applied in the week, then a response and an outcome in the week
        _application(
            conn, ops[0], ("applied", IN_WEEK), ("screening", IN_WEEK), ("rejected", IN_WEEK)
        )
        # applied before the week; its first response is in the week, its second is not new
        _application(
            conn,
            ops[1],
            ("applied", BEFORE),
            ("screening", IN_WEEK),
            ("interview_scheduled", IN_WEEK),
        )
        # applied before; first response before the week: not counted again
        _application(conn, ops[2], ("applied", BEFORE), ("screening", BEFORE), ("offer", IN_WEEK))
        _application(conn, unchecked, ("applied", IN_WEEK), ("no_response", IN_WEEK))
        _skip(conn, ops[3], IN_WEEK)
        _skip(conn, off, IN_WEEK)
        _skip(conn, off, IN_WEEK, by_operator=0)  # the watcher's: not counted
        _skip(conn, ops[2], AFTER)
    finally:
        conn.close()
    env.bot.send_weekly_digest()
    assert _weekly(env) == [
        "Weekly digest, 2026-10-21 09:00 to 2026-10-28 09:00\n"
        "operations: 2 alerts, 1 applied, 1 skipped, 2 responses, 2 outcomes\n"
        "Off-lane: 1 alerts, 0 applied, 1 skipped, 0 responses, 0 outcomes\n"
        "No lane: 0 alerts, 1 applied, 0 skipped, 0 responses, 1 outcomes\n"
        "All lanes: 3 alerts, 2 applied, 2 skipped, 2 responses, 3 outcomes"
    ]
    assert "SENTINEL" not in env.api.everything_sent()


def test_it_goes_once_a_week(env: Env) -> None:
    _configure(env)
    env.bot.send_weekly_digest()
    env.bot.send_weekly_digest()
    assert len(_weekly(env)) == 1
    env.clock.now += dt.timedelta(days=6)  # Tuesday: still the same week
    env.bot.send_weekly_digest()
    assert len(_weekly(env)) == 1
    env.clock.now += dt.timedelta(days=1)  # the next Wednesday, after 09:00 local
    env.bot.send_weekly_digest()
    assert _weekly(env)[-1].startswith("Weekly digest, 2026-10-28 09:00 to 2026-11-04 09:00")


def _sent_last_week(env: Env) -> None:
    conn = _db(env)
    try:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, '2026-10-21')", (botmod.WEEKLY_KEY,)
        )
    finally:
        conn.close()


def test_the_send_time_follows_the_configured_time_zone(env: Env) -> None:
    _sent_last_week(env)
    _configure(env)  # Wednesday 09:00 at UTC+05:30 is 03:30 UTC
    env.clock.now = dt.datetime(2026, 10, 28, 3, 29, 59, tzinfo=dt.UTC)
    env.bot.send_weekly_digest()
    assert _weekly(env) == []
    env.clock.now = dt.datetime(2026, 10, 28, 3, 30, tzinfo=dt.UTC)
    env.bot.send_weekly_digest()
    assert len(_weekly(env)) == 1
    assert _weekly(env)[0].startswith("Weekly digest, 2026-10-21 09:00 to 2026-10-28 09:00")


def test_the_same_moment_is_not_yet_due_in_another_zone(env: Env) -> None:
    _sent_last_week(env)
    _configure(env, zone="UTC")  # Wednesday 09:00 UTC
    env.clock.now = dt.datetime(2026, 10, 28, 3, 30, tzinfo=dt.UTC)
    env.bot.send_weekly_digest()
    assert _weekly(env) == []
    env.clock.now = dt.datetime(2026, 10, 28, 9, 0, tzinfo=dt.UTC)
    env.bot.send_weekly_digest()
    assert len(_weekly(env)) == 1


def test_a_week_across_a_daylight_saving_change_keeps_the_local_hour() -> None:
    """Europe/Berlin leaves summer time on 2026-10-25: Monday 09:00 is 07:00 UTC before
    it and 08:00 UTC after it, so that week is one hour longer."""
    zone = zoneinfo.ZoneInfo("Europe/Berlin")
    now = dt.datetime(2026, 10, 26, 8, 0, tzinfo=dt.UTC)
    start, end = botmod._digest_week(now, zone, 0, 9)
    assert (start, end) == (
        dt.datetime(2026, 10, 19, 7, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 26, 8, 0, tzinfo=dt.UTC),
    )
    before = botmod._digest_week(now - dt.timedelta(seconds=1), zone, 0, 9)
    assert before[1] == dt.datetime(2026, 10, 19, 7, 0, tzinfo=dt.UTC)
    # Spring: 02:30 on 2026-03-29 doesn't exist; it is read as 03:30 summer time.
    spring = botmod._digest_week(dt.datetime(2026, 3, 30, tzinfo=dt.UTC), zone, 6, 2)
    assert spring[1] == dt.datetime(2026, 3, 29, 0, 0, tzinfo=dt.UTC) + dt.timedelta(hours=1)


def test_nothing_is_sent_while_config_is_unreadable_or_when_it_is_off(env: Env) -> None:
    _configure(env, weekly={"day": "wednesday", "hour": 9})
    good = env.datadir.config_path.read_text()
    env.datadir.config_path.write_text("search: [unclosed\n")
    env.bot.send_weekly_digest()
    assert _weekly(env) == []
    env.datadir.config_path.write_text(good)
    path = env.datadir.config_path
    doc = yaml.safe_load(path.read_text())
    doc["alerts"]["weekly_digest"] = None
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    env.bot.send_weekly_digest()
    assert _weekly(env) == []


def test_a_failed_send_goes_again_next_poll(env: Env) -> None:
    _configure(env)
    real = env.api.call
    errors: list[Exception] = [telegram.TelegramUnreachable("sendMessage: TimeoutError")]

    def flaky(method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "sendMessage" and errors:
            raise errors.pop()
        return real(method, params)

    env.api.call = flaky  # type: ignore[method-assign]
    env.bot.send_weekly_digest()
    assert _weekly(env) == []
    env.bot.send_weekly_digest()
    assert len(_weekly(env)) == 1


@pytest.mark.parametrize(
    ("weekly", "message"),
    [
        ({"day": "funday", "hour": 9}, "alerts.weekly_digest.day must be a weekday"),
        ({"day": "monday", "hour": 24}, "alerts.weekly_digest.hour must be an integer from 0"),
        ({"day": "monday"}, "alerts.weekly_digest must be a mapping with day and hour"),
        ("monday 9", "alerts.weekly_digest must be a mapping with day and hour"),
    ],
)
def test_the_weekly_setting_is_checked(weekly: object, message: str) -> None:
    from careerhunter.config import validate_config
    from tests.test_config import example

    doc = example()
    doc["alerts"] = {"weekly_digest": weekly}
    assert any(message in problem for problem in validate_config(doc))
