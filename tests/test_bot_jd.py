"""/log with its job description (ADR-024): stitched pastes, the caption form, LinkedIn,
the request for a missing JD and its reply. A fake Bot API, a fake web and a fake clock;
synthetic text only."""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
from typing import Any

import pypdf
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from careerhunter import bot as botmod
from careerhunter import ledger
from tests import test_bot
from tests.conftest import ACTIVE
from tests.test_bot import AD_URL, CHAT, STRANGER, TOKEN, Env, ei_apps

env = test_bot.env  # the bot fixture, shared with test_bot

LINKEDIN = "https://www.linkedin.com/jobs/view/4000000001/?refId=abc123&trk=public_jobs_topcard"
CANONICAL = "https://www.linkedin.com/jobs/view/4000000001/"
HEADER = f"/log {AD_URL} | email | 2026-10-21"
PART_1 = "About the role\nExample Robotics is looking for an analyst. "
PART_2 = "You will answer synthetic tickets,\n"
PART_3 = "  and keep made-up notes.\n"


def later(env: Env, seconds: float) -> None:
    env.clock.now += dt.timedelta(seconds=seconds)


def jd_rows(env: Env) -> list[sqlite3.Row]:
    e = env.ei()
    assert e is not None
    try:
        return e.execute("SELECT * FROM job_descriptions ORDER BY id").fetchall()
    finally:
        e.close()


def make_pdf(text: str = "", *, encrypt: bool = False) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    if text:
        pdf.drawString(72, 720, text)
    pdf.showPage()
    pdf.save()
    if not encrypt:
        return buffer.getvalue()
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(buffer.getvalue())))
    writer.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def document(env: Env, data: bytes, name: str = "f1", **extra: Any) -> dict[str, Any]:
    env.api.files[f"documents/{name}.bin"] = data
    return {"document": {"file_id": name, "file_size": len(data)}, **extra}


# --- stitching ---------------------------------------------------------------------------


def test_nothing_is_recorded_until_the_window_closes(env: Env) -> None:
    sent = len(env.api.texts())
    assert env.say(HEADER + "\n" + PART_1) == "command:log"
    assert env.bot.stitching
    assert env.counts()["ei.applications"] == 0 and len(env.api.texts()) == sent
    later(env, botmod.JD_STITCH_SECONDS)
    env.bot.tick()  # exactly at the window: still open
    assert env.bot.stitching
    later(env, 0.5)
    env.bot.tick()
    assert not env.bot.stitching
    (row,) = jd_rows(env)
    assert row["text"] == PART_1 and row["jd_source"] == "pasted" and row["file_type"] == "txt"


@pytest.mark.parametrize("parts", [[PART_1, PART_2], [PART_1, PART_2, PART_3]])
def test_fragments_are_stitched_exactly_as_sent(env: Env, parts: list[str]) -> None:
    env.say(HEADER + "\n" + parts[0])
    for part in parts[1:]:
        later(env, 3)  # the window rolls: each within 4 seconds of the one before
        assert env.say(part) == "log:fragment"
    later(env, 5)
    env.bot.tick()
    (row,) = jd_rows(env)
    assert row["text"] == botmod.STITCH_SEPARATOR.join(parts)
    assert env.api.texts()[-1] == (
        "Recorded: Example Robotics — Synthetic Analyst (ref a1). Job description saved."
    )
    sent = env.api.everything_sent()
    assert PART_1 not in sent and "synthetic tickets" not in sent  # never echoed
    s, e = env.search(), env.ei()
    assert e is not None
    assert ledger.reconcile(s, {ACTIVE.label: e}) == []
    path = s.execute("SELECT jd_path FROM applications").fetchone()[0]
    assert (env.datadir.root / path).read_text() == botmod.STITCH_SEPARATOR.join(parts)
    assert (env.datadir.root / path).stat().st_mode & 0o777 == 0o600
    s.close()
    e.close()


def test_parts_split_at_blank_lines_get_them_back_and_no_words_fuse(env: Env) -> None:
    """Telegram splits a long paste at blank lines and delivers each part with the
    whitespace at the split removed; joining them with nothing fused words, as the manual
    check with the real client found."""
    paragraphs = [
        "About the role\nExample Robotics is looking for a synthetic",
        "duties analyst for made-up robotics",
        "tickets and notes.",
    ]
    pasted = "\n\n".join(paragraphs)
    delivered = [p.strip() for p in paragraphs]  # as Telegram hands them over
    env.say(HEADER + "\n" + delivered[0])
    for part in delivered[1:]:
        later(env, 1)
        env.say(part)
    later(env, 5)
    env.bot.tick()
    (row,) = jd_rows(env)
    assert row["text"] == pasted  # the paragraph breaks are back
    for fused in ("syntheticduties", "roboticstickets", "ticketsnotes"):
        assert fused not in row["text"]
    assert row["text"].split() == pasted.split()  # every word, in order


def test_a_fragment_after_the_window_gets_the_failed_stitch_reply(env: Env) -> None:
    env.say(HEADER + "\n" + PART_1)
    later(env, 5)
    assert env.say(PART_2) == "log:late-fragment"  # closes the stitch first, then replies
    assert env.api.texts()[-1] == botmod.JD_LATE
    (row,) = jd_rows(env)
    assert row["text"] == PART_1
    later(env, botmod.LATE_FRAGMENT_SECONDS + 1)
    assert env.say("hello") == "help"  # long after: just the help


def test_the_late_fragment_reply_points_to_a_reply_that_replaces_the_jd(env: Env) -> None:
    env.say(HEADER + "\n" + PART_1)
    later(env, 5)
    env.bot.tick()
    recorded = env.api.last()[0]
    full = (PART_1 + PART_2).encode()
    assert env.say(reply_to=recorded, **document(env, full)) == "reply:snapshot"
    assert env.api.texts()[-1] == "Job description saved."
    rows = jd_rows(env)
    assert [r["text"] for r in rows] == [PART_1, PART_1 + PART_2]  # the latest is current
    s = env.search()
    assert s.execute("SELECT jd_sha256 FROM applications").fetchone()[0] == rows[-1]["text_sha256"]
    s.close()


def test_a_command_inside_the_window_closes_it_first(env: Env) -> None:
    env.say(HEADER + "\n" + PART_1)
    later(env, 1)
    assert env.say("/recent") == "command:recent"
    assert not env.bot.stitching
    (row,) = jd_rows(env)
    assert row["text"] == PART_1  # the command was not added


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chat": STRANGER, "sender": STRANGER},
        {"sender": STRANGER},
        {"chat_type": "group"},
    ],
)
def test_another_chat_inside_the_window_is_ignored(env: Env, kwargs: dict[str, Any]) -> None:
    env.say(HEADER + "\n" + PART_1)
    later(env, 1)
    assert env.say(PART_2, **kwargs) == "ignored:chat"
    assert env.bot.stitching
    later(env, 1)
    env.say(PART_3)
    later(env, 5)
    env.bot.tick()
    assert jd_rows(env)[0]["text"] == PART_1 + "\n\n" + PART_3


@pytest.mark.parametrize(
    "extra",
    [
        {"forward_origin": {"type": "user"}},
        {"reply_to_message": {"message_id": 1}},
        {"photo": [{"file_id": "p1", "file_size": 3}]},
    ],
)
def test_only_plain_text_is_stitched(env: Env, extra: dict[str, Any]) -> None:
    env.say(HEADER + "\n" + PART_1)
    later(env, 1)
    env.say(PART_2, **extra)
    assert not env.bot.stitching
    assert jd_rows(env)[0]["text"] == PART_1


def test_a_crash_before_the_window_closes_writes_nothing(env: Env) -> None:
    env.say(HEADER + "\n" + PART_1)
    env.bot = botmod.Bot(env.datadir, env.api, CHAT, clock=env.clock, fetcher=env.web)
    later(env, 5)
    env.bot.tick()
    assert env.counts()["ei.applications"] == 0
    assert not any(t.startswith("Recorded") for t in env.api.texts())


def test_a_refused_header_writes_nothing_and_opens_no_window(env: Env) -> None:
    env.say(f"/log {AD_URL} | email | 2026-10-29\n" + PART_1)
    assert "in the future" in env.api.texts()[-1] and not env.bot.stitching
    env.say(f"/log {AD_URL}\n" + PART_1)  # a JD needs the method and the date
    assert env.api.texts()[-1] == botmod.JD_NEEDS_FULL_LOG and not env.bot.stitching


def test_the_claim_check_runs_when_the_window_closes(env: Env) -> None:
    env.say(f"/log {AD_URL} | email | 2026-07-01\n" + PART_1)
    later(env, 5)
    env.bot.tick()
    assert "outside the active EI claim" in env.api.texts()[-1]
    assert env.counts()["ei.applications"] == 0


def test_a_stitched_jd_over_the_cap_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(botmod.jobdesc, "MAX_CHARS", 10)
    env.say(HEADER + "\n" + PART_1)
    later(env, 5)
    env.bot.tick()
    assert env.api.texts()[-1].startswith("Not recorded: The job description is longer")
    assert env.counts()["ei.applications"] == 0


def test_a_blank_body_records_without_a_jd(env: Env) -> None:
    env.say(HEADER + "\n  ")
    later(env, 5)
    env.bot.tick()
    assert env.api.texts()[-1] == botmod.JD_REQUEST
    assert len(ei_apps(env)) == 1 and jd_rows(env) == []


# --- the caption form --------------------------------------------------------------------


def test_a_captioned_file_is_the_jd(env: Env) -> None:
    data = b"<html><h1>Analyst</h1><p>Synthetic duties.</p></html>"
    assert env.say(caption=HEADER, **document(env, data)) == "command:log"
    (row,) = jd_rows(env)
    assert (row["text"], row["jd_source"], row["file_type"]) == (
        "Analyst\nSynthetic duties.",
        "file",
        "html",
    )
    assert (env.datadir.root / row["file_path"]).read_bytes() == data
    assert env.api.texts()[-1].endswith("Job description saved.")


def test_a_caption_over_the_limit_or_without_a_date_is_refused(env: Env) -> None:
    env.say(caption=HEADER + " " + "x" * botmod.MAX_CAPTION, **document(env, b"Synthetic."))
    assert env.api.texts()[-1] == botmod.JD_NEEDS_FULL_LOG
    env.say(caption=f"/log {AD_URL}", **document(env, b"Synthetic.", "f2"))
    assert env.api.texts()[-1] == botmod.JD_NEEDS_FULL_LOG
    assert env.counts()["ei.applications"] == 0


def test_an_encrypted_pdf_is_refused_and_nothing_is_saved(env: Env) -> None:
    env.say(caption=HEADER, **document(env, make_pdf("Synthetic.", encrypt=True)))
    assert "encrypted" in env.api.texts()[-1]
    assert env.counts()["ei.applications"] == 0
    assert not list((env.datadir.root / "snapshots").rglob("*.*"))


def test_a_pdf_with_no_text_is_kept_and_the_jd_is_asked_for(env: Env) -> None:
    data = make_pdf()
    env.say(
        caption=f"/log {test_bot.SHELL_URL} | email | 2026-10-21 | Sample Co | Clerk",
        **document(env, data),
    )
    assert env.api.texts()[-1] == botmod.JD_NO_TEXT
    (row,) = ei_apps(env)
    assert (env.datadir.root / row["snapshot_path"]).read_bytes() == data
    assert jd_rows(env) == []
    env.say("Synthetic clerk duties.", reply_to=env.prompt_id())
    assert env.api.texts()[-1] == "Job description saved."
    assert jd_rows(env)[0]["text"] == "Synthetic clerk duties."


def test_any_other_unprompted_file_keeps_todays_reply(env: Env) -> None:
    env.say(caption="a note", **document(env, b"Synthetic."))
    assert "reply to the message that asked for it" in env.api.texts()[-1]


# --- LinkedIn ----------------------------------------------------------------------------


def test_a_linkedin_link_without_names_is_refused_unfetched(env: Env) -> None:
    env.say(f"/log {LINKEDIN} | linkedin | 2026-10-21")
    assert env.api.texts()[-1] == botmod.LINKEDIN_REFUSAL
    env.say(f"/log {LINKEDIN}")
    assert env.api.texts()[-1] == botmod.LINKEDIN_REFUSAL
    assert env.web.calls == [] and env.counts()["ei.applications"] == 0


def test_a_linkedin_link_with_names_and_a_jd_is_recorded_canonically(env: Env) -> None:
    env.say(f"/log {LINKEDIN} | linkedin | 2026-10-21 | Example Robotics | Analyst\n" + PART_1)
    later(env, 5)
    env.bot.tick()
    assert env.web.calls == []  # never fetched
    (row,) = ei_apps(env)
    assert row["posting_url"] == CANONICAL
    (jd,) = jd_rows(env)
    assert row["snapshot_path"] == jd["file_path"]  # the JD file is the job-ad copy
    s = env.search()
    assert s.execute("SELECT url FROM postings").fetchone()[0] == CANONICAL
    s.close()


# --- no JD, and the reply that brings it -------------------------------------------------


def test_no_jd_is_recorded_flagged_and_asked_for(env: Env) -> None:
    env.say(HEADER)
    assert env.api.texts()[-1] == botmod.JD_REQUEST
    (row,) = ei_apps(env)
    assert row["jd_expected"] == 1 and jd_rows(env) == []
    env.say(PART_1 + PART_2, reply_to=env.prompt_id())
    assert env.api.texts()[-1] == "Job description saved."
    assert jd_rows(env)[0]["text"] == PART_1 + PART_2
    assert PART_1 not in env.api.everything_sent()


def test_a_jd_reply_that_is_not_a_jd_is_asked_again(env: Env) -> None:
    env.say(HEADER)
    env.say(reply_to=env.prompt_id(), photo=[{"file_id": "p1", "file_size": 3}])
    assert "try again" in env.api.texts()[-1]
    env.say(reply_to=env.prompt_id(), **document(env, b"\x89PNG\r\n\x1a\nimage"))
    assert "not a PDF, HTML or plain-text" in env.api.texts()[-1]
    assert jd_rows(env) == []


def test_a_fetched_jd_is_used_when_none_was_pasted(env: Env) -> None:
    page = {**test_bot.JSON_LD, "description": "<p>Fetched synthetic duties.</p>"}
    env.web.routes[AD_URL] = (
        f'<script type="application/ld+json">{json.dumps(page)}</script>'.encode()
    )
    env.say(HEADER)
    (row,) = jd_rows(env)
    assert (row["text"], row["jd_source"]) == ("Fetched synthetic duties.", "fetched")
    assert env.api.texts()[-1].endswith("Job ad saved.") is False
    assert env.api.texts()[-1].endswith("Job description saved.")
    env.say(HEADER.replace("2026-10-21", "2026-10-22") + "\nPasted wins.")
    later(env, 5)
    env.bot.tick()
    assert jd_rows(env)[-1]["text"] == "Pasted wins."


# --- the polling loop --------------------------------------------------------------------


class Ticking(test_bot.FakeTelegram):
    """getUpdates takes five seconds of fake time, as a long poll would."""

    def __init__(self, clock: test_bot.Clock) -> None:
        super().__init__()
        self.clock = clock

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        if method == "getUpdates":
            self.clock.now += dt.timedelta(seconds=5)
        return super().call(method, params, timeout=timeout)


def test_the_loop_polls_briefly_while_stitching_and_orders_a_batch(
    env: Env, keep_umask: None
) -> None:
    api = Ticking(env.clock)
    first = {**env.message(HEADER + "\n" + PART_1), "update_id": 50}
    second = {**env.message(PART_2), "update_id": 51}
    api.updates = [[second, first], [], []]  # out of order in one batch
    botmod.run(
        env.datadir,
        secret=lambda n: TOKEN,
        api_factory=lambda t: api,
        clock=env.clock,
        fetcher=env.web,
        max_polls=2,
    )
    timeouts = [p["timeout"] for m, p in api.calls if m == "getUpdates"]
    assert timeouts == [botmod.POLL_TIMEOUT, botmod.STITCH_POLL_TIMEOUT]
    (row,) = jd_rows(env)
    assert row["text"] == PART_1 + "\n\n" + PART_2
    log = (env.datadir.logs / "bot.log").read_text()
    assert "job description stitched from 2 messages" in log
    assert "synthetic" not in log.lower() and "Example Robotics" not in log


def test_the_loop_uses_the_short_timeout_while_a_stitch_is_open(env: Env, keep_umask: None) -> None:
    first = {**env.message(HEADER + "\n" + PART_1), "update_id": 60}
    env.api.updates = [[first], [], []]
    botmod.run(
        env.datadir,
        secret=lambda n: TOKEN,
        api_factory=lambda t: env.api,
        clock=env.clock,
        fetcher=env.web,
        max_polls=2,
    )
    timeouts = [p["timeout"] for m, p in env.api.calls if m == "getUpdates"]
    assert timeouts == [botmod.POLL_TIMEOUT, botmod.STITCH_POLL_TIMEOUT]


# --- Applied from an alert ---------------------------------------------------------------


def pending_jd_requests(env: Env) -> list[str]:
    """Correlation ids with an open, unused request for their JD."""
    s = env.search()
    try:
        rows = s.execute(
            "SELECT payload FROM bot_prompts WHERE used_at IS NULL AND kind = 'snapshot'"
        ).fetchall()
    finally:
        s.close()
    payloads = [json.loads(r[0]) for r in rows]
    return [str(p["correlation_id"]) for p in payloads if p.get("jd") is True]


def assert_no_jd_gap(env: Env) -> None:
    """Every application is recorded with a JD or has a pending request for one."""
    with_jd = {r["application_id"] for r in jd_rows(env)}
    asked = set(pending_jd_requests(env))
    for row in ei_apps(env):
        assert row["jd_expected"] == 1
        assert row["id"] in with_jd or row["correlation_id"] in asked


def test_the_watcher_keeps_no_description_to_attach(env: Env) -> None:
    test_bot.alerted(env)
    s = env.search()
    try:
        columns = {r[1] for r in s.execute("PRAGMA table_info(postings)")}
    finally:
        s.close()
    assert not {c for c in columns if "description" in c}


def test_applied_with_a_saved_ad_asks_for_the_jd_and_takes_a_text_reply(env: Env) -> None:
    test_bot.apply(env)
    assert env.api.texts()[-2:] == [
        "Recorded: Example Robotics — Synthetic Analyst (ref a1). Job ad saved.",
        botmod.JD_REQUEST,
    ]
    assert_no_jd_gap(env)
    (app,) = ei_apps(env)
    assert pending_jd_requests(env) == [app["correlation_id"]]
    env.say(PART_1 + PART_2, reply_to=env.prompt_id())
    assert env.api.texts()[-1] == "Job description saved."
    (row,) = jd_rows(env)
    assert (row["text"], row["jd_source"]) == (PART_1 + PART_2, "pasted")
    assert pending_jd_requests(env) == []
    assert_no_jd_gap(env)
    (app,) = ei_apps(env)
    assert app["snapshot_path"] != row["file_path"]  # the ad copy is kept
    s, e = env.search(), env.ei()
    assert e is not None
    assert ledger.reconcile(s, {ACTIVE.label: e}) == []
    assert s.execute("SELECT jd_sha256 FROM applications").fetchone()[0] == row["text_sha256"]
    s.close()
    e.close()


def test_applied_without_an_ad_asks_for_the_jd_not_a_screenshot(env: Env) -> None:
    test_bot.alerted(env, test_bot.SHELL_URL, "Sample Logistics", "Synthetic Clerk")
    env.press("Applied")
    env.press("Email")
    env.press("Today")
    assert env.api.texts()[-2:] == [
        "Recorded: Sample Logistics — Synthetic Clerk (ref a1).",
        botmod.JD_REQUEST,
    ]
    assert "screenshot" not in env.api.everything_sent()
    assert_no_jd_gap(env)
    data = b"<html><h1>Clerk</h1><p>Synthetic clerk duties.</p></html>"
    env.say(reply_to=env.prompt_id(), **document(env, data))
    assert env.api.texts()[-1] == "Job description saved."
    (row,) = jd_rows(env)
    assert (row["text"], row["jd_source"], row["file_type"]) == (
        "Clerk\nSynthetic clerk duties.",
        "file",
        "html",
    )
    (app,) = ei_apps(env)
    assert app["snapshot_path"] == row["file_path"]  # the JD file is the ad copy
    s = env.search()
    assert s.execute("SELECT snapshot_path FROM postings").fetchone()[0] == row["file_path"]
    s.close()
    assert_no_jd_gap(env)
