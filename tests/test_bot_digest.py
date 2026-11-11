"""The alert digest (#39): one per watch run, capped per poll, one keyboard per row.

Synthetic postings only. Telegram is a fake; the clock is fixed; nothing sleeps.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from typing import Any

import pytest
import yaml

from careerhunter import bot as botmod
from careerhunter import claims, cli, db, ledger, search_store, telegram
from careerhunter.claims import ClaimSettings
from careerhunter.models import Application, Dating, Posting
from tests import test_bot
from tests.conftest import ACTIVE
from tests.test_bot import CHAT, Env, _digests, _heads, _insert, _poll, _rows

env = test_bot.env  # the bot fixture, shared with test_bot


def _config(env: Env, **alerts: Any) -> None:
    doc = yaml.safe_load(env.datadir.config_path.read_text())
    doc["alerts"] = alerts
    env.datadir.config_path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _set(env: Env, posting_id: int, **columns: Any) -> None:
    conn = db.connect(env.datadir.search_db_path)
    try:
        for column, value in columns.items():
            conn.execute(
                f"UPDATE postings SET {column} = ? WHERE id = ?",  # noqa: S608 - test column names
                (value, posting_id),
            )
    finally:
        conn.close()


def _run(env: Env, started: str, *, finished: str | None = None) -> int:
    """A watch_runs row; finished ones get zero counts."""
    conn = db.connect(env.datadir.search_db_path)
    try:
        db.migrate(conn, "search")
        run_id = search_store.start_watch_run(conn, started)
        if finished is not None:
            search_store.finish_watch_run(
                conn,
                run_id,
                finished_at=finished,
                budget_reached=False,
                boards_fetched=0,
                boards_failed=0,
                boards_left=0,
                held=search_store.HeldCounts(0, 0, 0),
            )
        return run_id
    finally:
        conn.close()


def _alerted(env: Env) -> dict[str, str | None]:
    conn = env.search()
    try:
        return {
            str(row["title"]): row["alerted_at"]
            for row in conn.execute("SELECT title, alerted_at FROM postings")
        }
    finally:
        conn.close()


def _post(env: Env, title: str, **kw: Any) -> int:
    slug = title.replace(" ", "-")
    return _insert(env, "Example Org", title, f"https://jobs.example.com/ads/{slug}", **kw)


# --- what a row shows ------------------------------------------------------------------------


def test_a_row_has_every_field_and_never_the_gap_names(env: Env, keep_umask: None) -> None:
    posting = _post(env, "Coordinator", schedule="on_call")
    _set(
        env,
        posting,
        location="Example City",
        gaps=json.dumps([{"name": "SENTINEL-gap-a"}, {"name": "SENTINEL-gap-b"}]),
        key_requirements=json.dumps(["Forklift licence", "Weekend availability"]),
    )
    assert _poll(env) == 0
    assert _rows(env) == [
        [
            "Example Org — Coordinator",
            "Example City | Lane: operations | Fit: High | 2 gaps",
            "Schedule: on call",
            "Needs: Forklift licence; Weekend availability",
            "https://jobs.example.com/ads/Coordinator",
        ]
    ]
    assert "SENTINEL-gap" not in env.api.everything_sent()


def test_nothing_in_a_digest_carries_learning_fields_or_ei_details(
    env: Env, keep_umask: None
) -> None:
    sentinels = {
        "gaps": "SENTINEL-digest-gaps-4c2",
        "llm": "SENTINEL-digest-llm-4c2",
        "tier": "SENTINEL-digest-tier-4c2",
        "notes": "SENTINEL-digest-notes-4c2",
        "reason": "SENTINEL-digest-reason-4c2",
    }
    with claims.active_claim(env.datadir, ClaimSettings(ACTIVE, (ACTIVE,))) as active:
        conn = db.connect(env.datadir.search_db_path)
        db.migrate(conn, "search")
        applied = Posting(
            "Example Org",
            "Applied Coordinator",
            "2026-10-20",
            url="https://jobs.example.com/ads/applied",
            source="watcher",
            gaps=json.dumps([{"name": sentinels["gaps"]}]),
            llm_output=sentinels["llm"],
            scout_metadata=json.dumps({"tier": sentinels["tier"]}),
        )
        ledger.record_application(
            conn,
            active,
            Application(
                applied,
                Dating("exact", day="2026-10-20"),
                "email",
                reasoning=sentinels["reason"],
                notes=sentinels["notes"],
            ),
        )
        posting_id = conn.execute("SELECT id FROM postings").fetchone()[0]
        conn.execute(
            "UPDATE postings SET fit = 'high', lane = 'operations', lane_checked = 1 WHERE id = ?",
            (posting_id,),
        )
        conn.close()
    other = _post(env, "Coordinator")
    _set(env, other, gaps=json.dumps([{"name": sentinels["gaps"]}]), llm_output=sentinels["llm"])
    assert _poll(env) == 0
    assert _digests(env), "a digest was sent"
    sent = env.api.everything_sent()
    for word in [*sentinels.values(), ACTIVE.label, ACTIVE.start.isoformat(), "LLM:"]:
        assert word not in sent, word


def test_long_fields_are_capped_and_a_huge_link_is_left_out(env: Env, keep_umask: None) -> None:
    posting = _post(env, "Coordinator")
    _set(
        env,
        posting,
        title="Coordinator " + "x" * 300,
        location="Example City " + "y" * 300,
        url="https://jobs.example.com/" + "z" * 1200,
    )
    assert _poll(env) == 0
    (row,) = _rows(env)
    assert len(row[0]) <= len("Example Org — ") + 120 and row[0].endswith("…")
    assert row[1].split(" | ")[0].endswith("…") and len(row[1].split(" | ")[0]) == 80
    assert row[-1] == "(link too long to send)"


# --- order, the cap, and the runs --------------------------------------------------------------


def test_the_cap_carries_the_rest_over_in_lane_order(env: Env, keep_umask: None) -> None:
    _config(env, max_per_poll=2)
    _post(env, "Medium Ops", fit="medium")
    _post(env, "Top Ops")
    _post(env, "Off Lane", lane=None)
    _post(env, "Fallback Ops", lane="customer_success")
    assert _poll(env) == 0
    assert _heads(env) == ["Example Org — Top Ops", "Example Org — Fallback Ops"]
    assert _digests(env)[0]["text"].startswith("New postings: 2\n\n")
    assert _poll(env) == 0
    assert _heads(env)[2:] == ["Example Org — Medium Ops", "Example Org — Off Lane"]
    assert _poll(env) == 0
    assert len(_digests(env)) == 2  # nothing left


def test_one_digest_per_run_older_runs_and_unknown_runs_first(env: Env, keep_umask: None) -> None:
    early = _run(env, "2026-10-28T10:00:00Z", finished="2026-10-28T10:05:00Z")
    late = _run(env, "2026-10-28T11:00:00Z", finished="2026-10-28T11:05:00Z")
    _set(env, _post(env, "Late Top"), score_run_id=late)
    _set(env, _post(env, "Early Medium", fit="medium"), score_run_id=early)
    _post(env, "Before Runs", fit="low")  # scored before migration 0011: no run
    assert _poll(env) == 0
    digests = _digests(env)
    assert [d["text"].split("\n\n")[1].split("\n")[0] for d in digests] == [
        "1. Example Org — Before Runs",
        "1. Example Org — Early Medium",
        "1. Example Org — Late Top",
    ]


def test_an_earlier_runs_leftovers_go_before_a_later_run(env: Env, keep_umask: None) -> None:
    _config(env, max_per_poll=1)
    early = _run(env, "2026-10-28T10:00:00Z", finished="2026-10-28T10:05:00Z")
    _set(env, _post(env, "Early One", fit="low"), score_run_id=early)
    _set(env, _post(env, "Early Two", fit="low"), score_run_id=early)
    assert _poll(env) == 0
    late = _run(env, "2026-10-28T11:00:00Z", finished="2026-10-28T11:05:00Z")
    _set(env, _post(env, "Late High"), score_run_id=late)
    assert _poll(env) == 0
    assert _poll(env) == 0
    assert _heads(env) == [
        "Example Org — Early One",
        "Example Org — Early Two",
        "Example Org — Late High",
    ]


def test_a_run_still_going_waits_and_a_dead_one_is_sent_later(env: Env, keep_umask: None) -> None:
    started = env.clock.now - dt.timedelta(minutes=5)
    running = _run(env, started.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _set(env, _post(env, "Coordinator"), score_run_id=running)
    assert _poll(env) == 0
    assert _digests(env) == []
    assert _alerted(env) == {"Coordinator": None}
    # Never finished: killed or crashed. After twice the budget it no longer holds rows.
    env.clock.now = started + botmod.STALE_RUN
    assert _poll(env) == 0
    assert _heads(env) == ["Example Org — Coordinator"]


def test_the_stale_window_is_longer_than_a_run() -> None:
    from careerhunter import watch

    assert botmod.STALE_RUN.total_seconds() >= 2 * watch.RUN_BUDGET


def test_unchecked_lanes_and_low_off_lane_rows_stay_held(env: Env, keep_umask: None) -> None:
    _post(env, "Off Low", lane=None, fit="low")
    unchecked = _post(env, "Unchecked")
    conn = db.connect(env.datadir.search_db_path)
    conn.execute("DROP TRIGGER posting_lane_checked_stays")
    conn.execute("UPDATE postings SET lane = NULL, lane_checked = 0 WHERE id = ?", (unchecked,))
    conn.close()
    assert _poll(env) == 0
    assert _digests(env) == []


# --- packing -------------------------------------------------------------------------------------


def test_a_large_digest_is_split_and_every_message_fits(env: Env, keep_umask: None) -> None:
    _config(env, max_per_poll=50)
    for index in range(23):
        posting = _post(env, f"Role{index:02d} Coordinator")
        _set(
            env,
            posting,
            title=f"Role{index:02d} " + "t" * 200,
            location="Example City " + "l" * 200,
            key_requirements=json.dumps(["r" * 60, "s" * 60, "u" * 60]),
            url=f"https://jobs.example.com/{index}/" + "p" * 900,
        )
    assert _poll(env) == 0
    digests = _digests(env)
    assert len(digests) >= 3
    numbers: list[int] = []
    for part, params in enumerate(digests, start=1):
        assert len(params["text"].encode("utf-16-le")) // 2 <= botmod.TEXT_LIMIT
        assert params["text"].startswith(f"New postings: 23 (part {part} of {len(digests)})")
        blocks = params["text"].split("\n\n")[1:]
        assert len(blocks) <= botmod.ROWS_PER_MESSAGE
        numbers += [int(block.split(".", 1)[0]) for block in blocks]
        keyboard = params["reply_markup"]["inline_keyboard"]
        assert [row[0]["text"] for row in keyboard] == [
            f"Applied {n}" for n in numbers[-len(blocks) :]
        ]
    assert numbers == list(range(1, 24))


def test_ten_short_rows_fit_one_message_and_eleven_do_not(env: Env, keep_umask: None) -> None:
    _config(env, max_per_poll=11)
    for index in range(11):
        _post(env, f"Short{index:02d}")
    assert _poll(env) == 0
    digests = _digests(env)
    assert [len(d["text"].split("\n\n")) - 1 for d in digests] == [10, 1]


# --- each row has its own buttons ----------------------------------------------------------------


def _tap(env: Env, label: str) -> str:
    """Tap a digest button, with the message's keyboard as Telegram echoes it back."""
    for message_id, params in reversed(env.api.sent()):
        markup = params.get("reply_markup")
        for row in (markup or {}).get("inline_keyboard", []):
            for button in row:
                if button["text"] == label:
                    update = env.callback(button["callback_data"], message_id)
                    update["callback_query"]["message"]["reply_markup"] = _current(
                        env, message_id, markup
                    )
                    return env.bot.handle_safely(update)
    raise AssertionError(f"no button {label!r}")


def _current(env: Env, message_id: int, original: dict[str, Any]) -> dict[str, Any]:
    """The keyboard as it stands after the bot's latest edit of that message."""
    current = original
    for method, params in env.api.calls:
        if method == "editMessageReplyMarkup" and params["message_id"] == message_id:
            current = params["reply_markup"]
    return current


def _edits(env: Env) -> list[list[list[str]]]:
    return [
        [[b["text"] for b in row] for row in params["reply_markup"]["inline_keyboard"]]
        for method, params in env.api.calls
        if method == "editMessageReplyMarkup"
    ]


def test_one_rows_tap_leaves_the_other_rows_buttons_valid(env: Env, keep_umask: None) -> None:
    _post(env, "First")
    _post(env, "Second")
    _post(env, "Third")
    assert _poll(env) == 0
    assert _tap(env, "Applied 1") == "callback:apply"
    assert _edits(env)[-1] == [["Applied 2", "Skipped 2"], ["Applied 3", "Skipped 3"]]
    assert _tap(env, "Skipped 2") == "callback:skip"
    assert _edits(env)[-1] == [["Applied 3", "Skipped 3"]]
    # Row 1's other button was spent with its row.
    before = len(env.api.calls)
    assert _tap(env, "Skipped 1") == "callback:rejected"
    answers = [p for m, p in env.api.calls[before:] if m == "answerCallbackQuery"]
    assert answers[-1]["text"] == "That button has expired."
    assert _tap(env, "Applied 3") == "callback:apply"
    assert _edits(env)[-1] == []


def test_a_forged_button_in_the_echoed_keyboard_is_dropped(env: Env, keep_umask: None) -> None:
    _post(env, "First")
    _post(env, "Second")
    assert _poll(env) == 0
    message_id, params = next(
        (mid, p) for mid, p in env.api.sent() if p["text"].startswith("New postings:")
    )
    markup = json.loads(json.dumps(params["reply_markup"]))
    markup["inline_keyboard"].append([{"text": "Forged", "callback_data": "A" * 22}])
    first = markup["inline_keyboard"][0][0]["callback_data"]
    update = env.callback(first, message_id)
    update["callback_query"]["message"]["reply_markup"] = markup
    env.bot.handle_safely(update)
    assert _edits(env)[-1] == [["Applied 2", "Skipped 2"]]


# --- failures -------------------------------------------------------------------------------------


class _Failing:
    """Wraps the fake so the next sendMessage calls raise the given errors, in order."""

    def __init__(self, env: Env, *errors: Exception) -> None:
        self.errors = list(errors)
        self.attempts = 0
        self.real = env.api.call
        env.api.call = self  # type: ignore[method-assign]

    def __call__(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "sendMessage":
            self.attempts += 1
        if method == "sendMessage" and self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return self.real(method, params)


@pytest.mark.parametrize(
    "error",
    [
        telegram.TelegramUnreachable("sendMessage: TimeoutError"),
        telegram.TelegramBadReply("sendMessage: the reply was not JSON"),
        telegram.TelegramRateLimited("sendMessage: HTTP 429"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_no_answer_a_bad_reply_or_429_stops_the_poll_and_marks_nothing(
    env: Env, keep_umask: None, error: Exception
) -> None:
    early = _run(env, "2026-10-28T10:00:00Z", finished="2026-10-28T10:05:00Z")
    _set(env, _post(env, "First"), score_run_id=early)
    _post(env, "Second")  # another digest, which this poll never reaches
    failing = _Failing(env, error)
    assert _poll(env) == 0
    assert set(_alerted(env).values()) == {None}
    assert failing.attempts == 1  # the second digest was not tried
    log_text = (env.datadir.logs / "bot.log").read_text()
    assert f"alert digest failed ({type(error).__name__}); left for the next poll" in log_text
    assert "First" not in log_text and "Example Org" not in log_text
    assert _poll(env) == 0
    assert set(_alerted(env).values()) == {"2026-10-28T16:00:00Z"}


def test_a_refused_digest_is_sent_row_by_row_and_the_bad_row_goes_last(
    env: Env, keep_umask: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="careerhunter.bot")
    _config(env, max_per_poll=2)
    _post(env, "Bad Row")
    _post(env, "Good Row")
    _post(env, "Waiting Row", fit="medium")
    refused = telegram.TelegramRejected("sendMessage: Bad Request")
    _Failing(env, refused, refused, None)  # the digest, then the bad row alone, then the good
    env.bot.send_pending_alerts()  # what it refused is counted in search.db
    alerted = _alerted(env)
    assert alerted["Bad Row"] is None and alerted["Good Row"] is not None
    assert alerted["Waiting Row"] is None
    logged = [r.getMessage() for r in caplog.records]
    assert "alert digest refused (TelegramRejected); sending its rows one by one" in logged
    assert "an alert was refused (TelegramRejected); left for the next poll" in logged
    # Next poll: the refused row goes behind the waiting one, so it can't block it.
    env.bot.send_pending_alerts()
    assert _heads(env)[-2:] == ["Example Org — Waiting Row", "Example Org — Bad Row"]
    assert set(_alerted(env).values()) != {None}


def _refusals(env: Env) -> list[tuple[str, int, bool]]:
    conn = env.search()
    try:
        rows = conn.execute(
            "SELECT p.title, r.refusals, r.given_up_at IS NOT NULL AS given_up "
            "FROM alert_refusals r JOIN postings p ON p.id = r.posting_id ORDER BY p.id"
        )
        return [(str(r["title"]), int(r["refusals"]), bool(r["given_up"])) for r in rows]
    finally:
        conn.close()


def _restarted(env: Env) -> None:
    """A new Bot on the same data folder: nothing carried over in memory."""
    env.bot = botmod.Bot(env.datadir, env.api, CHAT, clock=env.clock, fetcher=env.web)


def test_a_refusal_is_remembered_after_a_restart(env: Env, keep_umask: None) -> None:
    _config(env, max_per_poll=1)
    _post(env, "Bad Row")
    _post(env, "Waiting Row", fit="medium")
    refused = telegram.TelegramRejected("sendMessage: Bad Request")
    _Failing(env, refused, refused)  # the digest, then the row alone
    env.bot.send_pending_alerts()
    assert _refusals(env) == [("Bad Row", 1, False)]
    _restarted(env)
    env.bot.send_pending_alerts()  # the refused row still goes behind the waiting one
    assert _heads(env)[-1] == "Example Org — Waiting Row"
    env.bot.send_pending_alerts()
    assert _heads(env)[-1] == "Example Org — Bad Row"  # accepted now
    assert _refusals(env) == []  # an accepted posting's refusals are forgotten


def test_three_refusals_give_a_posting_up_and_doctor_counts_it(
    env: Env,
    keep_umask: None,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.WARNING, logger="careerhunter.bot")
    _post(env, "Bad Row")
    refused = telegram.TelegramRejected("sendMessage: Bad Request")
    for count in (1, 2, 3):
        _Failing(env, refused, refused)
        _restarted(env)
        env.bot.send_pending_alerts()
        assert _refusals(env) == [("Bad Row", count, count == 3)]
    assert "an alert was refused 3 times (TelegramRejected); given up, see doctor" in [
        r.getMessage() for r in caplog.records
    ]
    before = len(env.api.sent())
    env.bot.send_pending_alerts()
    assert len(env.api.sent()) == before  # never sent again
    assert _alerted(env) == {"Bad Row": None}  # held: never counted as sent
    conn = env.search()
    try:
        assert search_store.pending_alert_ids(conn) == []
        assert search_store.given_up_count(conn) == 1
    finally:
        conn.close()
    report = cli._Report()
    conn = env.search()
    try:
        capsys.readouterr()
        cli._doctor_refusals(report, conn)
    finally:
        conn.close()
    assert capsys.readouterr().out.strip() == (
        "bot refusals    WARN 1 posting refused by Telegram 3 times; given up and never sent "
        "(see logs/bot.log)"
    )


@pytest.mark.parametrize(
    "error",
    [
        telegram.TelegramUnreachable("sendMessage: TimeoutError"),
        telegram.TelegramBadReply("sendMessage: the reply was not JSON"),
        telegram.TelegramRateLimited("sendMessage: HTTP 429"),
    ],
)
def test_a_failure_that_is_not_a_refusal_is_not_counted(
    env: Env, keep_umask: None, error: Exception
) -> None:
    _post(env, "Coordinator")
    _Failing(env, error)
    env.bot.send_pending_alerts()
    assert _refusals(env) == []
    assert _alerted(env) == {"Coordinator": None}


def test_config_unreadable_sends_no_digest(env: Env, keep_umask: None) -> None:
    _post(env, "Coordinator")
    env.datadir.config_path.write_text("search: [unclosed\n")
    env.bot.send_pending_alerts()
    assert _digests(env) == []
    assert _alerted(env) == {"Coordinator": None}


def test_a_bad_max_per_poll_never_reaches_the_bot(env: Env, keep_umask: None) -> None:
    """Config validation refuses it, so the poll is held like any unreadable config."""
    _post(env, "Coordinator")
    _config(env, max_per_poll=0)
    env.bot.send_pending_alerts()
    assert _digests(env) == []
    conn = env.search()
    try:
        assert search_store.alerts_held_since(conn) is not None
    finally:
        conn.close()


def test_chat_id_is_the_only_recipient(env: Env, keep_umask: None) -> None:
    _post(env, "Coordinator")
    assert _poll(env) == 0
    assert {p["chat_id"] for p in _digests(env)} == {CHAT}


# --- the job-ad alert and the edges of a row -------------------------------------------------


def test_a_job_ad_that_is_a_scored_watcher_posting_is_logged_against_it(
    env: Env, keep_umask: None
) -> None:
    """/log of a link the watcher already stored records the application on that posting,
    and sends neither its tier nor its cosine (#34)."""
    posting = _insert(env, "Example Robotics", "Synthetic Analyst", test_bot.AD_URL)
    _set(env, posting, embedding_score=0.82)
    env.say(f"/log {test_bot.AD_URL} | email | 2026-10-21")
    assert env.api.texts()[-2].startswith("Recorded: Example Robotics — Synthetic Analyst")
    sent = env.api.everything_sent()
    assert "strong" not in sent and "0.82" not in sent
    s = env.search()
    assert s.execute("SELECT count(*) FROM postings").fetchone()[0] == 1
    assert s.execute("SELECT posting_id FROM applications").fetchone()[0] == posting
    s.close()
    assert _poll(env) == 0
    assert _digests(env) == []  # it has an application, so it is never alerted


def test_a_row_leaves_out_what_it_cannot_show(env: Env, keep_umask: None) -> None:
    posting = _post(env, "Coordinator", schedule="some_future_mark")
    _set(env, posting, gaps="not a list", url=None)
    assert _poll(env) == 0
    assert _rows(env) == [["Example Org — Coordinator", "Lane: operations | Fit: High"]]


def test_a_store_failure_reading_alerts_sends_nothing(
    env: Env, keep_umask: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _post(env, "Coordinator")

    def broken(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("disk I/O error")

    caplog.set_level(logging.WARNING, logger="careerhunter.bot")
    for name in ("pending_alerts", "digest_fields"):
        with monkeypatch.context() as patch:
            patch.setattr(search_store, name, broken)
            env.bot.send_pending_alerts()
    assert _digests(env) == []
    logged = [r.getMessage() for r in caplog.records]
    assert "pending alerts could not be read (OperationalError)" in logged
    assert "alert digest could not be read (OperationalError)" in logged
    assert _alerted(env) == {"Coordinator": None}


def test_no_answer_while_sending_row_by_row_stops_the_poll(
    env: Env, keep_umask: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="careerhunter.bot")
    _post(env, "First")
    _post(env, "Second")
    late = _run(env, "2026-10-28T11:00:00Z", finished="2026-10-28T11:05:00Z")
    _set(env, _post(env, "Next Run"), score_run_id=late)
    failing = _Failing(
        env,
        telegram.TelegramRejected("sendMessage: Bad Request"),
        telegram.TelegramUnreachable("sendMessage: TimeoutError"),
    )
    env.bot.send_pending_alerts()
    assert failing.attempts == 2  # the digest, then its first row; not the second, nor run two
    assert set(_alerted(env).values()) == {None}
    assert "alert digest failed (TelegramUnreachable); left for the next poll" in [
        r.getMessage() for r in caplog.records
    ]
