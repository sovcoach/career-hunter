"""The Telegram bot, end to end against a fake Bot API, a fake web and a fixed clock.

Synthetic data only, and no network: the autouse guard in conftest fails any test that
tries to open a connection.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import shutil
import sqlite3
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import bot as botmod
from careerhunter import claims, db, ledger, telegram
from careerhunter.bot import Bot, BotStartupError
from careerhunter.claims import ClaimSettings
from careerhunter.fetch import Fetched, FetchError
from careerhunter.models import Application, Dating, Posting
from careerhunter.secrets_store import SecretError
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE, EARLIER, set_claims

ROOT = Path(__file__).parents[1]
CHAT = 424242
STRANGER = 777001
NOW = dt.datetime(2026, 10, 28, 16, 0, tzinfo=dt.UTC)  # a Wednesday, 16:00 UTC
# Assembled at runtime so no source file holds anything shaped like a real token.
TOKEN = "1234" + "56789:" + "S" * 35
AD_URL = "https://jobs.example.com/ads/1"
SHELL_URL = "https://careers.example.com/jobs/2"
JSON_LD = {
    "@type": "JobPosting",
    "title": "Synthetic Analyst",
    "hiringOrganization": {"name": "Example Robotics"},
    "jobLocation": {"address": {"addressLocality": "Example City"}},
}
AD = f'<script type="application/ld+json">{json.dumps(JSON_LD)}</script><p>An ad.</p>'.encode()
SHELL = b"<html><title>Careers</title><div id='app'></div></html>"
PDF = b"%PDF-1.7\nsynthetic job ad\n"


class FakeTelegram:
    """Answers the Bot API methods the bot uses, and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ids = itertools.count(100)
        self.updates: list[Any] = []
        self.files: dict[str, bytes] = {}
        self.me: Any = {"is_bot": True, "username": "synthetic_bot"}

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.calls.append((method, params))
        if method == "sendMessage":
            return {"message_id": next(self.ids)}
        if method == "getMe":
            if isinstance(self.me, Exception):
                raise self.me
            return self.me
        if method == "getUpdates":
            item = self.updates.pop(0) if self.updates else []
            if isinstance(item, Exception):
                raise item
            return item
        if method == "getFile":
            return {"file_path": f"documents/{params['file_id']}.bin"}
        return True

    def download(self, file_path: str, max_bytes: int) -> bytes:
        return self.files[file_path]

    # -- reading what was sent -----------------------------------------------------------

    def sent(self) -> list[tuple[int, dict[str, Any]]]:
        """(message_id, params) for every sendMessage, in order."""
        out, ids = [], itertools.count(100)
        for method, params in self.calls:
            if method == "sendMessage":
                out.append((next(ids), params))
        return out

    def texts(self) -> list[str]:
        return [p["text"] for _, p in self.sent()]

    def everything_sent(self) -> str:
        """Every text and button label the bot sent, as one string."""
        bits = []
        for _, p in self.sent():
            bits.append(p["text"])
            for row in p.get("reply_markup", {}).get("inline_keyboard", []):
                bits.extend(b["text"] for b in row)
        return "\n".join(bits)

    def last(self) -> tuple[int, dict[str, Any]]:
        return self.sent()[-1]


@dataclass
class FakeWeb:
    routes: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def __call__(self, url: str) -> Fetched:
        self.calls.append(url)
        found = self.routes.get(url)
        if isinstance(found, Exception):
            raise found
        if found is None:
            raise FetchError("not found")
        return Fetched(url, 200, "text/html", found)


@dataclass
class Clock:
    now: dt.datetime = NOW

    def __call__(self) -> dt.datetime:
        return self.now


@dataclass
class Env:
    bot: Bot
    api: FakeTelegram
    web: FakeWeb
    clock: Clock
    datadir: DataDir
    update_ids: Any = field(default_factory=lambda: itertools.count(1))

    def say(self, text: str | None = None, **kwargs: Any) -> str:
        return self.bot.handle_safely(self.message(text, **kwargs))

    def message(
        self,
        text: str | None = None,
        *,
        chat: int = CHAT,
        sender: int = CHAT,
        chat_type: str = "private",
        reply_to: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": 9000 + next(self.update_ids),
            "chat": {"id": chat, "type": chat_type},
            "from": {"id": sender, "is_bot": False},
            **extra,
        }
        if text is not None:
            message["text"] = text
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        return {"update_id": next(self.update_ids), "message": message}

    def press(
        self, label: str, *, sender: int = CHAT, chat: int = CHAT, message_id: int | None = None
    ) -> str:
        """Tap the button with this label on the most recent keyboard that has one."""
        for mid, params in reversed(self.api.sent()):
            for row in params.get("reply_markup", {}).get("inline_keyboard", []):
                for button in row:
                    if button["text"] == label or button["text"].startswith(label):
                        return self.bot.handle_safely(
                            self.callback(
                                button["callback_data"], message_id or mid, sender=sender, chat=chat
                            )
                        )
        raise AssertionError(f"no button {label!r} was sent")

    def callback(
        self, data: Any, message_id: int, *, sender: int = CHAT, chat: int = CHAT
    ) -> dict[str, Any]:
        return {
            "update_id": next(self.update_ids),
            "callback_query": {
                "id": f"cq{next(self.update_ids)}",
                "from": {"id": sender},
                "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}},
                "data": data,
            },
        }

    def prompt_id(self) -> int:
        for mid, params in reversed(self.api.sent()):
            if params.get("reply_markup", {}).get("force_reply"):
                return mid
        raise AssertionError("no prompt was sent")

    def search(self) -> sqlite3.Connection:
        return db.connect_read_only(self.datadir.search_db_path)

    def ei(self) -> sqlite3.Connection | None:
        path = self.datadir.ei_record_path(ACTIVE.label)
        return db.connect_read_only(path) if path.exists() else None

    def counts(self) -> dict[str, int]:
        """Rows per table in both layers; a file that doesn't exist yet counts as empty."""
        out = {}
        layers = (
            (
                "search",
                self.datadir.search_db_path,
                ("postings", "applications", "decision_history", "skips"),
            ),
            (
                "ei",
                self.datadir.ei_record_path(ACTIVE.label),
                ("applications", "status_history", "activities"),
            ),
        )
        for layer, path, tables in layers:
            conn = db.connect_read_only(path) if path.exists() else None
            for table in tables:
                query = f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed names
                out[f"{layer}.{table}"] = conn.execute(query).fetchone()[0] if conn else 0
            if conn:
                conn.close()
        return out


def write_config(datadir: DataDir, *, chat_id: int | None = CHAT) -> None:
    path = datadir.config_path
    doc = yaml.safe_load(path.read_text())
    doc["telegram"] = {"chat_id": chat_id}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture
def env(datadir: DataDir) -> Env:
    for name in ("logs", "snapshots"):
        (datadir.root / name).mkdir(mode=0o700)
    shutil.copyfile(ROOT / "config.example.yaml", datadir.config_path)
    set_claims(datadir.root, ACTIVE.label, ACTIVE)
    write_config(datadir)
    api, web, clock = FakeTelegram(), FakeWeb({AD_URL: AD, SHELL_URL: SHELL}), Clock()
    return Env(Bot(datadir, api, CHAT, clock=clock, fetcher=web), api, web, clock, datadir)


def apply(env: Env, choice: str = "Today", url: str = AD_URL, method: str = "Company site") -> None:
    env.say(f"/log {url}")
    env.press("Applied")
    env.press(method)
    env.press(choice)


def ei_apps(env: Env) -> list[sqlite3.Row]:
    e = env.ei()
    assert e is not None
    rows = e.execute("SELECT * FROM applications ORDER BY id").fetchall()
    e.close()
    return rows


# --- the allowlist ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chat": STRANGER, "sender": STRANGER},  # a stranger
        {"chat_type": "group"},  # a group chat that happens to carry my id
        {"sender": STRANGER},  # someone else's message in my chat id
        {"forward_origin": {"type": "user"}},  # a forwarded message
    ],
    ids=["stranger", "group", "other sender", "forwarded"],
)
def test_messages_outside_the_allowlist_get_no_reply_and_write_nothing(
    env: Env, kwargs: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="careerhunter.bot"):
        env.say(f"/log {AD_URL}", **kwargs)
    assert env.api.calls == []
    assert env.web.calls == []
    assert not env.datadir.search_db_path.exists() or env.counts()["search.postings"] == 0
    assert str(STRANGER) not in caplog.text  # only a short hash of a stranger's chat id


def test_a_strangers_button_press_is_ignored(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    before = list(env.api.calls)
    env.press("Applied", sender=STRANGER, chat=STRANGER)
    assert env.api.calls == before  # not even answerCallbackQuery


# --- buttons are opaque, single-use and checked --------------------------------------------


def answers(env: Env) -> list[dict[str, Any]]:
    return [p for m, p in env.api.calls if m == "answerCallbackQuery"]


def test_a_forged_token_is_rejected(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    mid = env.api.last()[0]
    env.bot.handle_safely(env.callback("A" * 22, mid))
    assert answers(env)[-1]["text"] == "That button has expired."
    assert env.counts()["search.applications"] == 0


@pytest.mark.parametrize("data", ["x" * 100, "../../etc", "", None, 12345, "short"])
def test_malformed_callback_data_is_rejected(env: Env, data: Any) -> None:
    env.say(f"/log {AD_URL}")
    env.bot.handle_safely(env.callback(data, env.api.last()[0]))
    assert answers(env)[-1]["text"] == "That button has expired."


def test_a_token_can_be_used_once_and_spends_its_keyboard(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    alert_id = env.api.last()[0]
    env.press("Applied", message_id=alert_id)
    env.press("Applied", message_id=alert_id)  # the same button again
    assert answers(env)[-1]["text"] == "That button has expired."
    env.press("Skipped", message_id=alert_id)  # another button on the spent keyboard
    assert answers(env)[-1]["text"] == "That button has expired."
    assert env.counts()["search.skips"] == 0


def test_an_expired_token_is_rejected(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    env.clock.now = NOW + dt.timedelta(days=8)
    env.press("Applied")
    assert answers(env)[-1]["text"] == "That button has expired."


def test_a_token_from_another_message_is_rejected(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    env.press("Applied", message_id=1)
    assert answers(env)[-1]["text"] == "That button has expired."


def test_a_token_issued_to_another_chat_is_rejected(env: Env) -> None:
    s = db.connect(env.datadir.search_db_path)
    db.migrate(s, "search")
    s.execute(
        "INSERT INTO bot_tokens (token, keyboard, chat_id, message_id, action, payload, "
        "expires_at) "
        "VALUES (?, 'k', ?, 55, 'apply', '{}', '2099-01-01T00:00:00Z')",
        ("B" * 22, STRANGER),
    )
    s.close()
    env.bot.handle_safely(env.callback("B" * 22, 55))
    assert answers(env)[-1]["text"] == "That button has expired."


def test_an_unknown_action_writes_nothing(env: Env) -> None:
    s = db.connect(env.datadir.search_db_path)
    db.migrate(s, "search")
    s.execute(
        "INSERT INTO bot_tokens (token, keyboard, chat_id, message_id, action, payload, "
        "expires_at) "
        "VALUES (?, 'k', ?, 55, 'drop_tables', '{}', '2099-01-01T00:00:00Z')",
        ("C" * 22, CHAT),
    )
    s.close()
    env.bot.handle_safely(env.callback("C" * 22, 55))
    assert env.api.texts()[-1] == "That button isn't recognized."


# --- /log and Applied, with each date choice -----------------------------------------------


def test_log_sends_an_alert_with_company_title_and_link_only(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    text = env.api.texts()[-1]
    assert text == f"Example Robotics — Synthetic Analyst\n{AD_URL}"
    labels = [b["text"] for b in env.api.last()[1]["reply_markup"]["inline_keyboard"][0]]
    assert labels == ["Applied", "Skipped"]
    s = env.search()
    assert tuple(s.execute("SELECT title, location, source, url FROM postings").fetchone()) == (
        "Synthetic Analyst",
        "Example City",
        "self_sourced",
        AD_URL,
    )
    s.close()


def test_log_without_details_on_the_page_asks_for_them(env: Env) -> None:
    env.say(f"/log {SHELL_URL}")
    assert "/log <link> | Company | Title" in env.api.texts()[-1]
    env.say(f"/log {SHELL_URL} | Sample Logistics | Synthetic Clerk")
    assert env.api.texts()[-1].startswith("Sample Logistics — Synthetic Clerk")


def test_log_of_a_refused_link_writes_nothing(env: Env) -> None:
    env.web.routes["http://10.0.0.1/x"] = FetchError(
        "refusing to fetch from a private, local or reserved address"
    )
    env.say("/log http://10.0.0.1/x")
    assert "couldn't read that link" in env.api.texts()[-1]
    assert not env.datadir.search_db_path.exists() or env.counts()["search.postings"] == 0


@pytest.mark.parametrize(
    ("choice", "precision", "day", "week"),
    [
        ("Today", "exact", "2026-10-28", None),
        ("Yesterday", "exact", "2026-10-27", None),
        ("This week", "week", None, "2026-10-26"),
        ("Last week", "week", None, "2026-10-19"),
    ],
)
def test_applied_records_both_layers(
    env: Env, choice: str, precision: str, day: str | None, week: str | None
) -> None:
    apply(env, choice)
    assert (
        env.api.texts()[-1]
        == "Recorded: Example Robotics — Synthetic Analyst (ref a1). Job ad saved."
    )
    (row,) = ei_apps(env)
    assert (row["date_precision"], row["date_applied"], row["week_of"]) == (precision, day, week)
    assert (row["employer"], row["role"], row["method"]) == (
        "Example Robotics",
        "Synthetic Analyst",
        "company_site",
    )
    s, e = env.search(), env.ei()
    assert e is not None
    assert ledger.reconcile(s, {ACTIVE.label: e}) == []
    path = row["snapshot_path"]
    assert path and s.execute("SELECT snapshot_path FROM postings").fetchone()[0] == path
    assert (env.datadir.root / path).read_bytes() == AD
    assert (env.datadir.root / path).stat().st_mode & 0o777 == 0o600
    s.close()
    e.close()


def test_applied_with_another_date(env: Env) -> None:
    apply(env, "Other date")
    assert "YYYY-MM-DD" in env.api.texts()[-1]
    env.say("2026-10-20", reply_to=env.prompt_id())
    (row,) = ei_apps(env)
    assert (row["date_precision"], row["date_applied"]) == ("exact", "2026-10-20")


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ("2026-10-29", "in the future"),
        ("2026-07-01", "outside the active EI claim"),
        ("last tuesday", "YYYY-MM-DD"),
        ("2026-02-30", "isn't a real date"),
    ],
)
def test_a_bad_date_is_refused_and_asked_again(env: Env, reply: str, message: str) -> None:
    apply(env, "Other date")
    env.say(reply, reply_to=env.prompt_id())
    assert message in env.api.texts()[-1] and "try again" in env.api.texts()[-1]
    assert env.counts()["ei.applications"] == 0
    env.say("2026-10-21", reply_to=env.prompt_id())  # the retry prompt still works
    assert ei_apps(env)[0]["date_applied"] == "2026-10-21"


def test_the_same_application_twice_is_refused(env: Env) -> None:
    apply(env)
    apply(env)
    assert env.api.texts()[-1] == "That application is already recorded."
    assert env.counts()["ei.applications"] == 1


def test_no_snapshot_then_an_uploaded_pdf(env: Env) -> None:
    env.say(f"/log {SHELL_URL} | Sample Logistics | Synthetic Clerk")
    env.press("Applied")
    env.press("Email")
    env.press("Today")
    assert env.api.texts()[-2] == "Recorded: Sample Logistics — Synthetic Clerk (ref a1)."
    assert "PDF or screenshot" in env.api.texts()[-1]
    prompt = env.prompt_id()
    env.api.files["documents/f1.bin"] = b"<html>not a pdf</html>"
    env.say(reply_to=prompt, document={"file_id": "f1", "file_size": 22})
    assert "isn't a PDF, PNG or JPEG" in env.api.texts()[-1]
    env.api.files["documents/f2.bin"] = PDF
    env.say(reply_to=env.prompt_id(), document={"file_id": "f2", "file_size": len(PDF)})
    assert env.api.texts()[-1] == "Job ad saved."
    (row,) = ei_apps(env)
    assert row["snapshot_path"].endswith(".pdf")
    assert (env.datadir.root / row["snapshot_path"]).read_bytes() == PDF
    e = env.ei()
    assert e is not None
    edit = e.execute("SELECT column_name, new_value FROM edits").fetchone()
    assert tuple(edit) == ("snapshot_path", row["snapshot_path"])  # an audited update
    e.close()
    s = env.search()
    assert s.execute("SELECT snapshot_path FROM postings").fetchone()[0] == row["snapshot_path"]
    s.close()


def test_a_file_sent_without_being_asked_is_not_attached(env: Env) -> None:
    env.say(document={"file_id": "f9"})
    assert "reply to the message that asked for it" in env.api.texts()[-1]
    assert ("getFile", {"file_id": "f9"}) not in env.api.calls


def test_workday_json_is_used_for_a_javascript_page(env: Env) -> None:
    ad = "https://example.wd3.myworkdayjobs.com/External/job/Example-City/Synthetic_R-9"
    api = "https://example.wd3.myworkdayjobs.com/wday/cxs/example/External/job/Example-City/Synthetic_R-9"
    env.web.routes[ad] = SHELL
    env.web.routes[api] = json.dumps({"jobPostingInfo": {"title": "Synthetic Analyst"}}).encode()
    env.say(f"/log {ad} | Example Robotics | Synthetic Analyst")
    env.press("Applied")
    env.press("Company site")
    env.press("Today")
    assert env.api.texts()[-1].endswith("Job ad saved.")
    saved = (env.datadir.root / ei_apps(env)[0]["snapshot_path"]).read_bytes()
    assert b"Synthetic Analyst" in saved and b"wday/cxs" in saved


# --- Skipped ----------------------------------------------------------------------------------


def test_skipped_with_a_reason_writes_search_only(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    env.press("Skipped")
    env.press("Location")
    assert env.api.texts()[-1] == "Skipped: Example Robotics — Synthetic Analyst."
    counts = env.counts()
    assert counts["search.skips"] == 1
    assert counts["ei.applications"] == counts["ei.activities"] == 0


def test_skipped_with_a_typed_reason(env: Env) -> None:
    env.say(f"/log {AD_URL}")
    env.press("Skipped")
    env.press("Other")
    env.say("synthetic reason: rotating weekends", reply_to=env.prompt_id())
    s = env.search()
    assert (
        s.execute("SELECT reason FROM skips").fetchone()[0] == "synthetic reason: rotating weekends"
    )
    s.close()


# --- /status ------------------------------------------------------------------------------------


def test_status_by_reference_lands_in_both_layers(env: Env) -> None:
    apply(env)
    env.say("/status a1 interview_scheduled")
    assert env.api.texts()[-1] == "Example Robotics — Synthetic Analyst: interview scheduled."
    e, s = env.ei(), env.search()
    assert e is not None
    statuses = [r[0] for r in e.execute("SELECT status FROM status_history ORDER BY id")]
    assert statuses == ["applied", "interview_scheduled"]
    assert ledger.reconcile(s, {ACTIVE.label: e}) == []
    e.close()
    s.close()


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("/status a99 offer", "I don't know that reference"),
        ("/status 1 offer", "I don't know that reference"),  # never a database id
        ("/status a1 ghosted", "The status must be one of"),
        ("/status a1 applied", "The status must be one of"),
        ("/status a1", "Send: /status <ref> <status>"),
    ],
)
def test_bad_status_requests(env: Env, command: str, message: str) -> None:
    apply(env)
    env.say(command)
    assert message in env.api.texts()[-1]
    assert env.counts()["ei.status_history"] == 1


def test_status_by_buttons(env: Env) -> None:
    apply(env)
    env.say("/status")
    env.press("a1 ")
    env.press("hired")
    e = env.ei()
    assert e is not None
    assert e.execute("SELECT status FROM status_history ORDER BY id DESC").fetchone()[0] == "hired"
    e.close()


def test_a_reference_expires(env: Env) -> None:
    apply(env)
    env.clock.now = NOW + dt.timedelta(days=8)
    env.say("/status a1 offer")
    assert "I don't know that reference" in env.api.texts()[-1]


# --- /activity --------------------------------------------------------------------------------


def ei_activities(env: Env) -> list[tuple[Any, ...]]:
    e = env.ei()
    assert e is not None
    rows = [
        tuple(r)
        for r in e.execute(
            "SELECT type, date_precision, date, week_of, description FROM activities"
        )
    ]
    e.close()
    return rows


def test_activity_with_a_date(env: Env) -> None:
    env.say("/activity networking 2026-10-27 Coffee with a synthetic recruiter")
    assert env.api.texts()[-1] == "Recorded activity: networking."
    assert ei_activities(env) == [
        ("networking", "exact", "2026-10-27", None, "Coffee with a synthetic recruiter")
    ]


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("Today", ("exact", "2026-10-28", None)),
        ("Yesterday", ("exact", "2026-10-27", None)),
        ("This week", ("week", None, "2026-10-26")),
        ("Last week", ("week", None, "2026-10-19")),
    ],
)
def test_activity_date_buttons(env: Env, choice: str, expected: tuple[Any, ...]) -> None:
    env.say("/activity course Synthetic spreadsheet course")
    assert env.api.texts()[-1] == "When was it?"
    env.press(choice)
    assert ei_activities(env) == [("course", *expected, "Synthetic spreadsheet course")]


def test_activity_with_another_date_refuses_the_future(env: Env) -> None:
    env.say("/activity agency_registration Registered with Synthetic Staffing")
    env.press("Other date")
    env.say("2026-11-02", reply_to=env.prompt_id())
    assert "in the future" in env.api.texts()[-1]
    env.say("2026-10-15", reply_to=env.prompt_id())
    assert ei_activities(env)[0][1:3] == ("exact", "2026-10-15")


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("/activity job_fair 2026-11-02 A future fair", "in the future"),
        ("/activity party Something", "The type is one of"),
        ("/activity networking", "Add a short description"),
        ("/activity networking 2026-07-01 Too early", "outside the active EI claim"),
    ],
)
def test_bad_activities(env: Env, command: str, message: str) -> None:
    env.say(command)
    assert message in env.api.texts()[-1]
    assert env.counts()["ei.activities"] == 0


def test_activity_with_no_active_claim(env: Env) -> None:
    set_claims(env.datadir.root, None)
    write_config(env.datadir)
    env.say("/activity networking 2026-10-27 Synthetic call")
    assert env.api.texts()[-1] == "Not recorded: no EI claim is active."


# --- /recent and privacy -------------------------------------------------------------------------


def test_recent_lists_references_and_statuses(env: Env) -> None:
    apply(env)
    env.say(f"/log {SHELL_URL} | Sample Logistics | Synthetic Clerk")
    env.press("Applied")
    env.press("Agency")
    env.press("Yesterday")
    env.say("/status a1 offer")
    env.say("/recent")
    assert env.api.texts()[-1] == (
        "a2 Sample Logistics — Synthetic Clerk: applied\n"
        "a1 Example Robotics — Synthetic Analyst: offer"
    )


def test_nothing_sent_carries_learning_fields_or_ei_details(env: Env) -> None:
    sentinels = [
        "SENTINEL-notes-9b1",
        "SENTINEL-gaps-9b1",
        "SENTINEL-llm-9b1",
        "SENTINEL-reason-9b1",
    ]
    with claims.active_claim(env.datadir, ClaimSettings(ACTIVE, (ACTIVE,))) as active:
        s = db.connect(env.datadir.search_db_path)
        db.migrate(s, "search")
        posting = Posting(
            "Example Robotics",
            "Synthetic Analyst",
            "2026-10-20",
            url=AD_URL,
            gaps=sentinels[1],
            llm_output=sentinels[2],
            fit_rating=9,
        )
        ledger.record_application(
            s,
            active,
            Application(
                posting,
                Dating("exact", day="2026-10-20"),
                "email",
                reasoning=sentinels[3],
                notes=sentinels[0],
            ),
        )
        posting_id = s.execute("SELECT id FROM postings").fetchone()[0]
        env.bot.alert(s, posting_id)
        s.close()
    env.say("/recent")
    env.say("/status")
    env.say("/status a1 rejected")
    env.say("/activity networking 2026-07-01 Too early")  # refused for being outside the claim
    sent = env.api.everything_sent()
    for word in [*sentinels, ACTIVE.label, ACTIVE.start.isoformat(), "fit"]:
        assert word not in sent, word


# --- startup fails closed -----------------------------------------------------------------------


def make_earlier_unsealed(datadir: DataDir) -> None:
    with claims.active_claim(datadir, ClaimSettings(EARLIER, (EARLIER,))):
        pass
    set_claims(datadir.root, ACTIVE.label, EARLIER, ACTIVE)
    write_config(datadir)


@pytest.mark.parametrize(
    "problem",
    [
        "bad config",
        "no chat id",
        "unsealed claim",
        "no token",
        "plaintext keyring",
        "getMe fails",
        "not a bot",
    ],
)
def test_startup_fails_closed(env: Env, problem: str, keep_umask: None) -> None:
    secret: Any = lambda name: TOKEN  # noqa: E731
    if problem == "bad config":
        env.datadir.config_path.write_text("search: nope\n")
    elif problem == "no chat id":
        write_config(env.datadir, chat_id=None)
    elif problem == "unsealed claim":
        make_earlier_unsealed(env.datadir)
    elif problem == "no token":
        secret = lambda name: None  # noqa: E731
    elif problem == "plaintext keyring":

        def secret(name: str) -> str:
            raise SecretError("Refusing to use keyring backend keyrings.alt.file.PlaintextKeyring")

    elif problem == "getMe fails":
        env.api.me = telegram.TelegramError("getMe: Unauthorized")
    elif problem == "not a bot":
        env.api.me = {"is_bot": False}
    with pytest.raises(BotStartupError) as exc:
        botmod.run(env.datadir, secret=secret, api_factory=lambda token: env.api, max_polls=3)
    assert TOKEN not in str(exc.value)
    assert "getUpdates" not in [m for m, _ in env.api.calls]


def test_the_token_is_handed_to_the_client(env: Env, keep_umask: None) -> None:
    seen: list[str] = []
    botmod.run(
        env.datadir,
        secret=lambda n: TOKEN,
        api_factory=lambda t: seen.append(t) or env.api,
        max_polls=1,
    )
    assert seen == [TOKEN]


# --- the polling loop ---------------------------------------------------------------------------


def test_the_loop_acknowledges_updates_and_logs_no_content(env: Env, keep_umask: None) -> None:
    update = env.message(f"/log {AD_URL}")
    env.api.updates = [[{**update, "update_id": 41}], []]
    code = botmod.run(
        env.datadir,
        secret=lambda n: TOKEN,
        api_factory=lambda t: env.api,
        clock=env.clock,
        fetcher=env.web,
        max_polls=2,
    )
    assert code == 0
    polls = [p for m, p in env.api.calls if m == "getUpdates"]
    assert "offset" not in polls[0] and polls[1]["offset"] == 42
    assert env.bot.offset() == 42
    logs = env.datadir.logs
    text = (logs / "bot.log").read_text()
    assert "update handled: command:log" in text
    assert AD_URL not in text and "Example Robotics" not in text
    for name in ("bot.log", "bot.heartbeat"):
        assert (logs / name).stat().st_mode & 0o777 == 0o600
    assert dt.datetime.fromisoformat((logs / "bot.heartbeat").read_text().strip())


def test_network_errors_back_off_and_are_logged_redacted(env: Env, keep_umask: None) -> None:
    env.api.updates = [telegram.TelegramError(f"getUpdates: timed out talking to bot{TOKEN}"), []]
    sleeps: list[float] = []
    botmod.run(
        env.datadir,
        secret=lambda n: TOKEN,
        api_factory=lambda t: env.api,
        sleep=sleeps.append,
        max_polls=2,
    )
    assert sleeps == [2.0]
    text = (env.datadir.logs / "bot.log").read_text()
    assert "getUpdates failed" in text and TOKEN not in text


def test_an_unexpected_error_is_reported_without_details(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(url: str) -> Fetched:
        raise RuntimeError(f"synthetic failure mentioning {AD_URL}")

    env.bot.fetcher = broken
    with caplog.at_level(logging.ERROR, logger="careerhunter.bot"):
        assert env.say(f"/log {AD_URL}") == "error:RuntimeError"
    assert env.api.texts()[-1].startswith("Something went wrong")
    assert AD_URL not in caplog.text


def test_discover_lists_chats_without_replying(env: Env) -> None:
    env.api.updates = [[env.message("hi"), env.message("hello", chat=STRANGER, sender=STRANGER)]]
    found = botmod.discover(env.datadir, secret=lambda n: TOKEN, api_factory=lambda t: env.api)
    assert found == [(CHAT, "private"), (STRANGER, "private")]
    assert [m for m, _ in env.api.calls] == ["getUpdates"]
    assert "offset" not in env.api.calls[0][1]


# --- the real client never leaks the token ---------------------------------------------


def test_client_errors_never_contain_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(request: Any, timeout: float, context: Any) -> Any:
        raise urllib.error.URLError(f"cannot reach {request.full_url}")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", failing)
    client = telegram.HttpTelegram(TOKEN)
    with pytest.raises(telegram.TelegramError) as exc:
        client.call("getMe")
    assert TOKEN not in str(exc.value) and "<token>" in str(exc.value)
    assert exc.value.__suppress_context__  # the original, URL-bearing error is hidden
    with pytest.raises(telegram.TelegramError, match="unexpected file path"):
        client.download("../../etc/passwd", 10)


def test_the_log_filter_redacts_the_token(env: Env) -> None:
    botmod.setup_logging(env.datadir, TOKEN)
    botmod.log.warning("something mentioned %s and bot%s", TOKEN, TOKEN)
    for handler in botmod.log.handlers:
        handler.flush()
    text = (env.datadir.logs / "bot.log").read_text()
    assert TOKEN not in text and "<token>" in text


class FakeReply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeReply:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return self.body


def test_the_client_parses_replies_and_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter(
        [
            json.dumps({"ok": True, "result": {"is_bot": True}}).encode(),
            json.dumps({"ok": False, "description": f"Unauthorized for {TOKEN}"}).encode(),
            b"not json",
            PDF,
            b"x" * 50,
        ]
    )
    seen: list[str] = []

    def urlopen(request: Any, timeout: float, context: Any) -> FakeReply:
        seen.append(request.full_url)
        return FakeReply(next(replies))

    monkeypatch.setattr(telegram.urllib.request, "urlopen", urlopen)
    client = telegram.HttpTelegram(TOKEN)
    assert client.call("getMe") == {"is_bot": True}
    with pytest.raises(telegram.TelegramError) as exc:
        client.call("getMe")
    assert "Unauthorized" in str(exc.value) and TOKEN not in str(exc.value)
    with pytest.raises(telegram.TelegramError, match="not JSON"):
        client.call("getUpdates")
    assert client.download("documents/file_1.pdf", 1024) == PDF
    with pytest.raises(telegram.TelegramError, match="larger than"):
        client.download("documents/file_2.pdf", 10)
    assert seen[0].startswith("https://api.telegram.org/bot") and seen[-1].endswith("file_2.pdf")


def test_an_http_error_body_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    def urlopen(request: Any, timeout: float, context: Any) -> Any:
        body = io.BytesIO(f"bad request near bot{TOKEN}".encode())
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)  # type: ignore[arg-type]

    monkeypatch.setattr(telegram.urllib.request, "urlopen", urlopen)
    with pytest.raises(telegram.TelegramError) as exc:
        telegram.HttpTelegram(TOKEN).call("sendMessage", {"chat_id": CHAT, "text": "x"})
    assert "HTTP 400" in str(exc.value) and TOKEN not in str(exc.value)
