"""The Telegram bot: long polling, one allowlisted chat, writes through the ledger.

Trust boundary (ADR-016):

- **Long polling only.** The bot calls getUpdates; it has no webhook and listens on
  no port.
- **One chat, enforced in code.** An update is handled only if its chat is private,
  the chat id is the configured telegram.chat_id, and so is the sender's id. Anything
  else gets no reply at all; the log records only a short hash of the chat id.
  Forwarded messages are ignored too.
- **Nothing in a message is an identifier.** Buttons carry random single-use tokens,
  replies are matched to the bot's own prompt by message id, and /status takes short
  references issued by the bot. All are looked up in search.db (migration 0002) and
  checked against the chat, the message, an expiry and whether they were used.
- **Fail closed.** `startup()` refuses unless config.yaml is valid with a chat id,
  every earlier EI claim is sealed, the token is in the Keychain, and getMe works.
  Before every EI-bearing write the bot re-reads config and re-checks the claims,
  as the CLI does. A discovery approve or reject opens `search.db` only.

Privacy (ADR-015, amended by ADR-019, ADR-020, #39 and #41): watcher and discovery
postings go out as one digest per watch run. A digest row carries the company, title,
location, lane,
fit word (`Fit: High`, `Medium` or `Low`), the number of gaps, the schedule flag in
words, up to three key requirements the model took from the posting, and the link.
/log records a self-sourced application at once (#34) and replies with the company,
the title and a reference; it sends no alert. Status words and references are sent
too. A discovery row sends the suggested name and the reduced URL only. A
watchlist agency with no separate employer stored is labelled " (agency)". Never
notes, 0-10 fit ratings, gap names, model text, vectors, snippets, queries, EI
record details or claim labels.

Job descriptions (ADR-024): `/log <link> | method | date [| Company | Title]` may carry
the JD after its first line. Telegram splits a long paste into several messages, so
after such a /log, each plain text message from the same chat that arrives within
JD_STITCH_SECONDS of the one before is added to the JD after a blank line (Telegram
splits a paste at blank lines and drops them, so that is what puts them back); nothing is
fetched or recorded until that window closes, and the bot polls with a short timeout
while it is open. A PDF, HTML or text file whose caption is the /log line is the JD
too. Without a JD (and none could be fetched), the application is still recorded and
the reply asks for the JD as a reply to it. The JD comes in through Telegram and is
never sent back: no reply quotes it, and the log counts the messages it was made of.
A LinkedIn link is never fetched, so it needs `| Company | Title`.

Dates: after Applied (or a bare /log) and a method, the bot asks when, with buttons
(today, yesterday, this week, last week, or another date typed as YYYY-MM-DD). Exact
days record precision `exact`; the week buttons record `week`. /activity takes an
optional YYYY-MM-DD, else asks the same way. Future dates are refused, and everything
passes the claim check.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

import yaml

from careerhunter import (
    claims,
    db,
    enums,
    jobdesc,
    lanes,
    ledger,
    search_store,
    selfsourced,
    snapshots,
    urls,
)
from careerhunter import discover as discovery
from careerhunter.claims import ActiveClaim, ClaimError, Verdict, check_dating
from careerhunter.config import (
    ConfigError,
    alerts_max_per_poll,
    health_failure_runs,
    load_config,
    telegram_chat_id,
    weekly_digest,
)
from careerhunter.config import timezone as config_timezone
from careerhunter.config import today as config_today
from careerhunter.fetch import Fetched, fetch
from careerhunter.ledger import PartialWriteError
from careerhunter.models import Activity, Application, Dating, Posting
from careerhunter.secrets_store import SecretError, get_secret
from careerhunter.seed import monday_of
from careerhunter.storage_guard import DataDir
from careerhunter.telegram import (
    HttpTelegram,
    TelegramAPI,
    TelegramError,
    TelegramRejected,
    redact,
)
from careerhunter.watch import RUN_BUDGET

log = logging.getLogger("careerhunter.bot")
# The /log reply when config.yaml can't be read. No path and no content.
CONFIG_UNREADABLE = "Config can't be read, so nothing was recorded. Run careerhunter doctor."

POLL_TIMEOUT = 50
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{16,64}")
REF_SHAPE = re.compile(r"a[1-9][0-9]{0,6}")
ALERT_TTL = dt.timedelta(days=7)
DISCOVERY_EACH = 5
DISCOVERY_DIGEST = 8
STEP_TTL = dt.timedelta(days=1)
REF_TTL = dt.timedelta(days=7)
MAX_UPLOAD = 20 * 1024 * 1024
MAX_TEXT = 500
MAX_CAPTION = 1024  # Telegram's own caption limit
# A long paste arrives as several messages: each one within this many seconds of the one
# before is part of the same JD. Set in code, with no config key.
JD_STITCH_SECONDS = 4
STITCH_POLL_TIMEOUT = 1  # getUpdates' timeout while a JD is being stitched
STITCH_SEPARATOR = "\n\n"  # between the parts of a split paste
LATE_FRAGMENT_SECONDS = 60  # a plain text this soon after a saved JD was likely part of it
FILE_KINDS = ("document", "photo", "video", "audio", "voice", "sticker", "animation")

METHOD_BUTTONS = [
    ("Company site", "company_site"),
    ("LinkedIn", "linkedin_easy_apply"),
    ("Email", "email"),
    ("Referral", "referral"),
    ("Agency", "agency"),
    ("Other", "other"),
]
SKIP_BUTTONS = [
    ("Location", "location"),
    ("Seniority", "seniority"),
    ("Schedule", "schedule"),
    ("Pay", "pay"),
    ("Not a fit", "not a fit"),
    ("Other…", "other"),
]
DATE_BUTTONS = [
    ("Today", "today"),
    ("Yesterday", "yesterday"),
    ("This week", "this_week"),
    ("Last week", "last_week"),
    ("Other date", "other"),
]
LATER_STATUSES = [s for s in enums.STATUSES if s != "applied"]

# Telegram refusing one posting sent alone this many times gives it up (#42).
REFUSALS_TO_GIVE_UP = 3

LOG_USAGE = (
    "Send: /log <link> | method | date, or just /log <link>. "
    "Add | Company | Title when the page doesn't show them."
)
LINKEDIN_REFUSAL = (
    "LinkedIn links are never fetched, so give the company and title: "
    "/log <link> | method | date | Company | Title"
)
JD_NEEDS_FULL_LOG = (
    "Send the job description with /log <link> | method | date on the first line "
    "(or as the caption of its file)."
)
JD_REQUEST = (
    "No job description was saved. Reply to this message with it, as text or as a PDF, "
    "HTML or text file."
)
JD_NO_TEXT = (
    "The file was kept as the job ad, but it has no text I can read. Reply to this message "
    "with the job description as text, or as another PDF, HTML or text file."
)
JD_NO_TEXT_AGAIN = (
    "The file was kept as the job ad, but it has no text I can read. Send the job "
    "description as text, or as another PDF, HTML or text file."
)
JD_LATE = (
    "The job description was saved without that message. Reply to the Recorded message "
    "with the whole description as a .txt file."
)

HELP = (
    "Commands:\n"
    "/log <link> | method | date: record a job you applied to yourself, in one step "
    "(or /log <link> to be asked; add | Company | Title if the page doesn't show them). "
    "Paste the job description on the lines after it, or send its PDF, HTML or text "
    "file with the /log line as the caption\n"
    "/status <ref> <status>: update an application, or /status to choose\n"
    "/activity <type> [YYYY-MM-DD] <description>: a recruiter call, agency "
    "registration and so on\n"
    "/recent: your latest applications"
)


class BotStartupError(Exception):
    """The bot refuses to start. The message is user-facing and never holds the token."""


class Refusal(Exception):
    """A request the bot declines, with the reply to send. Nothing was written."""


def chat_hash(chat_id: Any) -> str:
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:8]


def _utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _words(value: str) -> str:
    return value.replace("_", " ")


def company_label(company: str, doc: Mapping[str, Any]) -> str:
    """The company line for an alert.

    A watchlist entry with ``agency: true`` whose name is the stored company had no
    separate employer on the posting, so the label gains `` (agency)``. A client name
    stored from the board does not match that entry and stays as it is.
    """
    watchlist = doc.get("watchlist")
    if not isinstance(watchlist, list):
        return company
    folded = company.casefold().strip()
    for item in watchlist:
        if not isinstance(item, dict) or item.get("agency") is not True:
            continue
        name = item.get("name")
        if isinstance(name, str) and name.casefold().strip() == folded:
            return f"{company} (agency)"
    return company


def choice_dating(choice: str, today: dt.date) -> Dating:
    if choice == "today":
        return Dating("exact", day=today.isoformat())
    if choice == "yesterday":
        return Dating("exact", day=(today - dt.timedelta(days=1)).isoformat())
    if choice == "this_week":
        return Dating("week", week_of=monday_of(today).isoformat())
    if choice == "last_week":
        return Dating("week", week_of=(monday_of(today) - dt.timedelta(days=7)).isoformat())
    raise Refusal("That choice isn't recognized.")


def parse_date(text: str, today: dt.date) -> Dating:
    text = text.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise Refusal("Please send the date as YYYY-MM-DD, for example 2026-10-27.")
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        raise Refusal(f"{text} isn't a real date.") from None
    if day > today:
        raise Refusal("That date is in the future, so it can't be recorded.")
    return Dating("exact", day=day.isoformat())


def _fit_label(raw: object) -> str:
    """`High`, `Medium` or `Low`; anything else is `not scored`."""
    if isinstance(raw, str) and raw in enums.FIT_LEVELS:
        return raw.capitalize()
    return "not scored"


# The digest (#39). Telegram allows 4096 characters of message text, counted in UTF-16
# code units; each message also stays at ten rows, twenty buttons.
TEXT_LIMIT = 4096
ROWS_PER_MESSAGE = 10
# A run not finished this long after it started was killed or crashed. Twice the run
# budget, so a slow run that is still going is never cut in half.
STALE_RUN = dt.timedelta(seconds=2 * RUN_BUDGET)
_CAPS = {"company": 80, "title": 120, "location": 80, "lane": 60, "requirement": 60}
MAX_URL = 1000
_SCHEDULE_WORDS = {"on_call": "on call", "over_weekend_limit": "weekend days over your limit"}


@dataclass(frozen=True)
class _DigestEntry:
    fields: search_store.DigestFields
    label: str  # the company, with " (agency)" when the watchlist says so
    order: lanes.Ordered


@dataclass(frozen=True)
class _Numbered:
    number: int  # 1-based, through the whole digest
    entry: _DigestEntry


def _units(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str, limit: int) -> str:
    """One line, at most `limit` characters, with "…" when cut."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _digest_row(numbered: _Numbered) -> str:
    """Company, title, location, lane, fit, gap count, schedule, key requirements, URL.

    Never gap names, the model text, notes, the tier name or a cosine.
    """
    fields = numbered.entry.fields
    company = _clip(numbered.entry.label, _CAPS["company"])
    lines = [f"{numbered.number}. {company} — {_clip(fields.title, _CAPS['title'])}"]
    facts = []
    if fields.location.strip():
        facts.append(_clip(fields.location, _CAPS["location"]))
    if numbered.entry.order.lane_line:
        facts.append(_clip(numbered.entry.order.lane_line, _CAPS["lane"]))
    facts.append(f"Fit: {_fit_label(fields.fit)}")
    if fields.gap_count is not None:
        facts.append(f"{fields.gap_count} gap" + ("" if fields.gap_count == 1 else "s"))
    lines.append(" | ".join(facts))
    if fields.schedule:
        words = [_SCHEDULE_WORDS.get(mark, "") for mark in fields.schedule.split()]
        shown = [word for word in words if word]
        if shown:
            lines.append("Schedule: " + ", ".join(shown))
    if fields.key_requirements:
        needs = [_clip(item, _CAPS["requirement"]) for item in fields.key_requirements[:3]]
        lines.append("Needs: " + "; ".join(needs))
    if fields.url and len(fields.url) <= MAX_URL and fields.url.isprintable():
        lines.append(fields.url)
    elif fields.url:
        lines.append("(link too long to send)")
    return "\n".join(lines)


def _entries(
    fields: Mapping[int, search_store.DigestFields],
    rows: list[lanes.Ordered],
    doc: Mapping[str, Any],
) -> list[_DigestEntry]:
    return [
        _DigestEntry(
            fields[row.posting_id], company_label(fields[row.posting_id].company, doc), row
        )
        for row in rows
        if row.posting_id in fields
    ]


def digest_texts(search: sqlite3.Connection, doc: Mapping[str, Any]) -> list[str]:
    """The text of every digest message for the pending alerts, in send order.

    For `careerhunter demo`: it reads `search` and sends nothing. The per-poll cap and
    the run grouping of a real poll are not applied, and no posting is marked alerted.
    """
    pending = search_store.pending_alerts(search)
    rows = lanes.alert_order(pending, doc)
    fields = search_store.digest_fields(search, [row.posting_id for row in rows])
    entries = _entries(fields, rows, doc)
    messages = _pack(entries)
    return [
        _digest_text(message, (index, len(messages)) if len(messages) > 1 else None, len(entries))
        for index, message in enumerate(messages, start=1)
    ]


def _digest_header(count: int, part: tuple[int, int] | None) -> str:
    head = f"New postings: {count}"
    return head if part is None else f"{head} (part {part[0]} of {part[1]})"


def _digest_text(message: list[_Numbered], part: tuple[int, int] | None, total: int) -> str:
    rows = "\n\n".join(_digest_row(numbered) for numbered in message)
    return f"{_digest_header(total, part)}\n\n{rows}"


def _pack(entries: list[_DigestEntry]) -> list[list[_Numbered]]:
    """Rows into messages of at most ROWS_PER_MESSAGE rows and TEXT_LIMIT units.

    The header is measured at its longest, so every message fits whatever its part
    number. A row is capped well under the limit, so each message holds at least one.
    """
    numbered = [_Numbered(index, entry) for index, entry in enumerate(entries, start=1)]
    reserve = _units(_digest_header(len(entries), (len(entries), len(entries)))) + 2
    messages: list[list[_Numbered]] = []
    current: list[_Numbered] = []
    used = reserve
    for item in numbered:
        size = _units(_digest_row(item)) + 2
        if current and (len(current) == ROWS_PER_MESSAGE or used + size > TEXT_LIMIT):
            messages.append(current)
            current, used = [], reserve
        current.append(item)
        used += size
    if current:
        messages.append(current)
    return messages


HEALTH_NAMES = {"lm_studio": "LM Studio", "searxng": "SearXNG", "boards": "Every board"}


def _failure_text(name: str, failures: int, held: int | None, kind: str | None = None) -> str:
    """The one message for an outage: the component, the count, the postings held.

    An `unauthorized` failure says so, with the command that stores the token, so a
    refused token is never read as a server that is down."""
    if held is None:
        waiting = "The number of postings waiting to be scored is not known."
    elif held == 1:
        waiting = "1 posting is waiting to be scored."
    else:
        waiting = f"{held} postings are waiting to be scored."
    runs = "1 run" if failures == 1 else f"{failures} runs"
    if kind == "unauthorized":
        return (
            f"Health: {name} has failed {runs} in a row: unauthorized, it refused the API "
            f"token (run: careerhunter secret set lm_studio_token). {waiting}"
        )
    return f"Health: {name} has failed {runs} in a row. {waiting}"


WEEKLY_KEY = "weekly_digest_sent"


def _digest_week(
    now: dt.datetime, zone: dt.tzinfo, weekday: int, hour: int
) -> tuple[dt.datetime, dt.datetime]:
    """The week the latest scheduled digest covers: [moment - 7 days, moment), where the
    moment is the last `weekday` at `hour`, local time in `zone`, not after `now`.

    The local wall time is kept across a daylight-saving change, so the week can be an
    hour shorter or longer.
    """
    local = now.astimezone(zone)
    day = local.date() - dt.timedelta(days=(local.weekday() - weekday) % 7)
    moment = _local_moment(day, hour, zone)
    if moment > now:
        day -= dt.timedelta(days=7)
        moment = _local_moment(day, hour, zone)
    return _local_moment(day - dt.timedelta(days=7), hour, zone), moment


def _local_moment(day: dt.date, hour: int, zone: dt.tzinfo) -> dt.datetime:
    """`day` at `hour` in `zone`, as an aware UTC moment (PEP 495, fold 0). A wall time
    a spring daylight-saving change skips is read with the offset before the change, so
    it lands just after the gap; a wall time an autumn change repeats is the first one."""
    return dt.datetime.combine(day, dt.time(hour), tzinfo=zone).astimezone(dt.UTC)


def _weekly_text(start: dt.datetime, end: dt.datetime, counts: dict[str, dict[str, int]]) -> str:
    """The digest text; `start` and `end` are local times in schedule.timezone."""
    first = start.strftime("%Y-%m-%d %H:%M")
    until = end.strftime("%Y-%m-%d %H:%M")
    last = {"Off-lane": 1, "No lane": 2}  # lane names first, then these, in this order
    lanes = sorted(counts, key=lambda name: (last.get(name, 0), name))
    total = {key: sum(counts[lane][key] for lane in lanes) for key in search_store.WEEK_COUNTS}
    lines = [f"Weekly digest, {first} to {until}"]
    for lane in lanes:
        lines.append(f"{_clip(lane, _CAPS['lane'])}: {_week_line(counts[lane])}")
    lines.append(f"All lanes: {_week_line(total)}")
    return "\n".join(lines)


def _week_line(counts: Mapping[str, int]) -> str:
    return (
        f"{counts['alerts']} alerts, {counts['applied']} applied, {counts['skipped']} skipped, "
        f"{counts['responses']} responses, {counts['outcomes']} outcomes"
    )


@dataclass
class _Stitch:
    """A /log whose JD is still arriving: its first line, parsed, and the JD so far."""

    url: str
    method: str
    dating: Dating
    company: str | None
    title: str | None
    parts: list[str]
    last: dt.datetime


def _log_fields(raw: object) -> dict[str, str]:
    """The posting a bare /log found, as its own button stored it. Anything else is
    refused: a payload is ours, but it is still checked field by field."""
    keys = ("url", "company", "title", "location")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) for k in keys):
        raise Refusal("That button isn't recognized.")
    if not raw["url"] or not raw["company"] or not raw["title"]:
        raise Refusal("That button isn't recognized.")
    return {k: str(raw[k]) for k in keys}


class Bot:
    def __init__(
        self,
        datadir: DataDir,
        api: TelegramAPI,
        chat_id: int,
        *,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        fetcher: Callable[[str], Fetched] = fetch,
    ) -> None:
        self.datadir = datadir
        self.api = api
        self.chat_id = chat_id
        self.clock = clock
        self.fetcher = fetcher
        self._stitch: _Stitch | None = None  # in memory only: a crash writes nothing
        self._jd_saved_at: dt.datetime | None = None

    # --- plumbing ------------------------------------------------------------------

    def _now(self) -> str:
        return _utc(self.clock())

    def _later(self, ttl: dt.timedelta) -> str:
        return _utc(self.clock() + ttl)

    def _config(self) -> dict[str, Any]:
        return load_config(self.datadir.config_path)

    def _today(self, doc: dict[str, Any]) -> dt.date:
        return config_today(doc, self.clock())

    @contextmanager
    def _search(self) -> Iterator[sqlite3.Connection]:
        conn = db.connect(self.datadir.search_db_path)
        try:
            db.migrate(conn, "search")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _writable(
        self, doc: dict[str, Any]
    ) -> Iterator[tuple[sqlite3.Connection, ActiveClaim | None]]:
        settings = claims.settings_from_config(doc)
        claims.require_write_ready(self.datadir, settings)
        with self._search() as search, claims.active_claim(self.datadir, settings) as active:
            yield search, active

    def send(self, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> int:
        params: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        result = self.api.call("sendMessage", params)
        return int(result["message_id"])

    def _keyboard(
        self,
        search: sqlite3.Connection,
        text: str,
        rows: list[list[tuple[str, str, dict[str, Any]]]],
        ttl: dt.timedelta,
        *,
        per_row: bool = False,
    ) -> int:
        """Send `text` with buttons. One keyboard id for all of them, so any tap spends
        them all; with `per_row`, each row has a keyboard id of its own, so a tap spends
        only its own row and the others stay live."""
        keyboard_id = secrets.token_urlsafe(9)
        keyboards = [keyboard_id]
        expires = self._later(ttl)
        markup = []
        with db.transaction(search):
            for index, row in enumerate(rows):
                if per_row and index:
                    keyboard_id = secrets.token_urlsafe(9)
                    keyboards.append(keyboard_id)
                buttons = []
                for label, action, payload in row:
                    token = secrets.token_urlsafe(16)
                    search.execute(
                        "INSERT INTO bot_tokens (token, keyboard, chat_id, action, payload, "
                        "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (token, keyboard_id, self.chat_id, action, json.dumps(payload), expires),
                    )
                    buttons.append({"text": label, "callback_data": token})
                markup.append(buttons)
        message_id = self.send(text, markup)
        for keyboard in keyboards:
            search.execute(
                "UPDATE bot_tokens SET message_id = ? WHERE keyboard = ?", (message_id, keyboard)
            )
        return message_id

    def _prompt(
        self,
        search: sqlite3.Connection,
        text: str,
        kind: str,
        payload: dict[str, Any],
        ttl: dt.timedelta = STEP_TTL,
    ) -> None:
        result = self.api.call(
            "sendMessage",
            {"chat_id": self.chat_id, "text": text, "reply_markup": {"force_reply": True}},
        )
        search.execute(
            "INSERT INTO bot_prompts (chat_id, message_id, kind, payload, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.chat_id, int(result["message_id"]), kind, json.dumps(payload), self._later(ttl)),
        )

    def _expect_reply(
        self,
        search: sqlite3.Connection,
        message_id: int,
        kind: str,
        payload: dict[str, Any],
        ttl: dt.timedelta = STEP_TTL,
    ) -> None:
        """Accept a reply to a message already sent, as to a prompt."""
        search.execute(
            "INSERT INTO bot_prompts (chat_id, message_id, kind, payload, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.chat_id, message_id, kind, json.dumps(payload), self._later(ttl)),
        )

    def _ref(self, search: sqlite3.Connection, application_id: int) -> str:
        now = self._now()
        row = search.execute(
            "SELECT ref FROM bot_refs WHERE chat_id = ? AND application_id = ? AND expires_at > ?",
            (self.chat_id, application_id, now),
        ).fetchone()
        if row:
            return str(row["ref"])
        top = search.execute(
            "SELECT max(CAST(substr(ref, 2) AS INTEGER)) FROM bot_refs WHERE chat_id = ?",
            (self.chat_id,),
        ).fetchone()[0]
        ref = f"a{(top or 0) + 1}"
        search.execute(
            "INSERT INTO bot_refs (chat_id, ref, application_id, expires_at) VALUES (?, ?, ?, ?)",
            (self.chat_id, ref, application_id, self._later(REF_TTL)),
        )
        return ref

    def _resolve_ref(self, search: sqlite3.Connection, ref: str) -> int | None:
        if not REF_SHAPE.fullmatch(ref):
            return None
        row = search.execute(
            "SELECT application_id FROM bot_refs WHERE chat_id = ? AND ref = ? AND expires_at > ?",
            (self.chat_id, ref, self._now()),
        ).fetchone()
        return int(row["application_id"]) if row else None

    def _allowed(self, chat: Any, sender: Any) -> bool:
        return (
            isinstance(chat, dict)
            and isinstance(sender, dict)
            and chat.get("type") == "private"
            and chat.get("id") == self.chat_id
            and sender.get("id") == self.chat_id
        )

    # --- state between runs ------------------------------------------------------------

    def offset(self) -> int | None:
        with self._search() as search:
            row = search.execute(
                "SELECT value FROM bot_state WHERE key = 'update_offset'"
            ).fetchone()
        return int(row["value"]) if row else None

    def save_offset(self, offset: int) -> None:
        with self._search() as search:
            search.execute(
                "INSERT INTO bot_state (key, value) VALUES ('update_offset', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(offset),),
            )

    def heartbeat(self) -> None:
        path = self.datadir.logs / "bot.heartbeat"
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self._now() + "\n")
        os.replace(tmp, path)

    def _labelled_company(self, company: str) -> str:
        """Agency suffix when config can be read. A bad config still sends the alert."""
        try:
            return company_label(company, self._config())
        except (ConfigError, OSError, yaml.YAMLError, ValueError):
            return company

    def send_pending_alerts(self) -> None:
        """Send pending watcher and discovery postings as one digest per watch run.

        Config is read once per poll, for the lanes, the off-lane threshold, the cap and
        the agency labels. If it can't be read, no watcher alert is sent this poll: the
        hold is recorded in ``bot_state`` (for doctor) and logged only when it starts
        and when it ends, and the next poll tries again. Job ads sent to the bot are
        not held by this.

        Which postings: scored watcher and discovery postings with a fit, no skip and no
        alert, in ``lanes.alert_order`` (off-lane below ``search.off_lane_min_fit`` and postings
        not yet lane-checked stay pending). A run's postings wait until that run has
        finished, or started more than STALE_RUN ago (killed or crashed), so a run is
        never sent half-scored and a dead one never blocks its postings. Postings with
        no run (scored before search migration 0011) go first, then runs oldest first,
        so a later run cannot push an earlier run's postings back. At most
        ``alerts.max_per_poll`` postings go per poll; the rest carry over in order.
        A posting Telegram refused on its own goes behind the others, so it can't take
        the same slot every poll; the refusals are counted in search.db (search migration
        0017), so a restart remembers them. After REFUSALS_TO_GIVE_UP refusals the bot
        gives up on it: it is never sent again, stays unalerted, and doctor counts it.

        Each message goes out first and ``alerted_at`` is set only after Telegram
        accepts it. No answer, a bad reply or HTTP 429 stops the poll with everything
        unsent left pending; if the process or the answer was lost after Telegram
        accepted a message, the next poll sends it again. A message Telegram refused is
        sent again one posting per message, so one bad row cannot hold back the rest.
        """
        doc = self._alert_config()
        if doc is None:
            return
        try:
            with self._search() as search:
                pending = search_store.pending_alerts(search)
        except (OSError, sqlite3.Error) as exc:
            log.warning("pending alerts could not be read (%s)", type(exc).__name__)
            return
        digests = self._digests(pending, doc)
        for rows in digests:
            if not self._send_digest(rows, doc):
                return

    def _digests(
        self, pending: list[search_store.PendingAlert], doc: Mapping[str, Any]
    ) -> list[list[lanes.Ordered]]:
        """This poll's digests: one per run, runs in order, at most the cap in all."""
        by_id = {alert.posting_id: alert for alert in pending}
        stale_before = _utc(self.clock() - STALE_RUN)
        groups: dict[tuple[int, int], list[lanes.Ordered]] = {}
        refused: list[tuple[tuple[int, int], lanes.Ordered]] = []
        behind = self._refused_now()
        for item in lanes.alert_order(pending, doc):
            alert = by_id[item.posting_id]
            if alert.run_id is not None and not (
                alert.run_finished
                or (alert.run_started_at is not None and alert.run_started_at <= stale_before)
            ):
                continue  # its run is still going: wait for it
            key = (0, 0) if alert.run_id is None else (1, alert.run_id)
            if item.posting_id in behind:
                refused.append((key, item))
            else:
                groups.setdefault(key, []).append(item)
        ordered = [(key, item) for key in sorted(groups) for item in groups[key]] + refused
        chosen = ordered[: alerts_max_per_poll(doc)]
        digests: dict[tuple[int, int], list[lanes.Ordered]] = {}
        for key, item in chosen:
            digests.setdefault(key, []).append(item)
        return list(digests.values())

    def _send_digest(self, rows: list[lanes.Ordered], doc: Mapping[str, Any]) -> bool:
        """Send one run's digest. False when the poll should stop."""
        try:
            with self._search() as search:
                fields = search_store.digest_fields(search, [row.posting_id for row in rows])
        except (OSError, sqlite3.Error) as exc:
            log.warning("alert digest could not be read (%s)", type(exc).__name__)
            return False
        entries = _entries(fields, rows, doc)
        messages = _pack(entries)
        for index, message in enumerate(messages, start=1):
            part = (index, len(messages)) if len(messages) > 1 else None
            try:
                self._send_rows(message, part, total=len(entries))
            except TelegramRejected:
                log.warning("alert digest refused (TelegramRejected); sending its rows one by one")
                if not self._send_one_by_one(message, total=len(entries)):
                    return False
            except (TelegramError, OSError, sqlite3.Error) as exc:
                log.warning("alert digest failed (%s); left for the next poll", type(exc).__name__)
                return False
        return True

    def _refused_now(self) -> set[int]:
        """Postings refused before and not given up. Unreadable: none go behind."""
        try:
            with self._search() as search:
                return search_store.refused_postings(search)
        except (OSError, sqlite3.Error) as exc:
            log.warning("alert refusals could not be read (%s)", type(exc).__name__)
            return set()

    def _refused(self, posting_id: int) -> None:
        """Count a refusal of one posting sent alone; give up at REFUSALS_TO_GIVE_UP."""
        with self._search() as search, db.transaction(search):
            given_up = search_store.record_refusal(
                search, posting_id, self._now(), give_up_after=REFUSALS_TO_GIVE_UP
            )
        if given_up:
            log.warning(
                "an alert was refused %d times (TelegramRejected); given up, see doctor",
                REFUSALS_TO_GIVE_UP,
            )
        else:
            log.warning("an alert was refused (TelegramRejected); left for the next poll")

    def _send_one_by_one(self, message: list[_Numbered], total: int) -> bool:
        for entry in message:
            try:
                self._send_rows([entry], None, total=total)
            except TelegramRejected:
                try:
                    self._refused(entry.entry.fields.posting_id)
                except (OSError, sqlite3.Error) as exc:
                    log.warning("alert refusal not recorded (%s)", type(exc).__name__)
                    return False
            except (TelegramError, OSError, sqlite3.Error) as exc:
                log.warning("alert digest failed (%s); left for the next poll", type(exc).__name__)
                return False
        return True

    def _send_rows(
        self, message: list[_Numbered], part: tuple[int, int] | None, *, total: int
    ) -> None:
        """One message: its rows, each with its own Applied and Skipped keyboard.

        Each row's two tokens share a keyboard id of their own, so a tap spends only
        that row's buttons. The tokens are written first; the message ids and
        ``alerted_at`` only once Telegram has accepted the message, in one transaction.
        """
        text = _digest_text(message, part, total)
        expires = self._later(ALERT_TTL)
        keyboards: list[str] = []
        markup: list[list[dict[str, str]]] = []
        with self._search() as search:
            with db.transaction(search):
                for numbered in message:
                    keyboard_id = secrets.token_urlsafe(9)
                    keyboards.append(keyboard_id)
                    payload = json.dumps({"posting_id": numbered.entry.fields.posting_id})
                    buttons = []
                    for label, action in (("Applied", "apply"), ("Skipped", "skip")):
                        token = secrets.token_urlsafe(16)
                        search.execute(
                            "INSERT INTO bot_tokens (token, keyboard, chat_id, action, payload, "
                            "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (token, keyboard_id, self.chat_id, action, payload, expires),
                        )
                        buttons.append(
                            {"text": f"{label} {numbered.number}", "callback_data": token}
                        )
                    markup.append(buttons)
            message_id = self.send(text, markup)
            now = self._now()
            with db.transaction(search):
                for keyboard_id in keyboards:
                    search.execute(
                        "UPDATE bot_tokens SET message_id = ? WHERE keyboard = ?",
                        (message_id, keyboard_id),
                    )
                for numbered in message:
                    search_store.mark_alerted(search, numbered.entry.fields.posting_id, now)
                    search_store.clear_refusal(search, numbered.entry.fields.posting_id)

    def _alert_config(self) -> dict[str, Any] | None:
        """This poll's config, or None while it can't be read (the hold is recorded)."""
        try:
            doc = self._config()
        except (ConfigError, OSError, yaml.YAMLError, ValueError) as exc:
            self._record_hold(self._now(), type(exc).__name__)
            return None
        self._record_hold(None, None)
        return doc

    def _record_hold(self, stamp: str | None, reason: str | None) -> None:
        """Start or end the alert hold in ``bot_state``, logging only a change."""
        try:
            with self._search() as search:
                held = search_store.alerts_held_since(search)
                if (held is None) == (stamp is None):
                    return  # no change: nothing to write or log
                search_store.set_alerts_held_since(search, stamp)
        except (OSError, sqlite3.Error) as exc:
            log.warning("alert hold could not be recorded (%s)", type(exc).__name__)
            return
        if stamp is None:
            log.info("config.yaml can be read again; watcher alerts resumed")
        else:
            log.warning("config.yaml could not be read (%s); watcher alerts held", reason)

    def send_health(self) -> None:
        """One failure message and one recovery message per outage (#42).

        A component (the local model, SearXNG, or every board) that has failed
        `health.failure_runs` watch runs in a row gets one message naming it, the count
        and the postings held for scoring; when it next works, one message says so.
        Neither names a host or a path. What was sent is recorded only after Telegram
        accepts it, so a message that fails goes again next poll; if the answer is lost
        after Telegram accepted it, it can come twice. Nothing is sent while config.yaml
        can't be read.
        """
        doc = self._alert_config()
        if doc is None:
            return
        limit = health_failure_runs(doc)
        try:
            with self._search() as search:
                states = search_store.component_health(search)
        except (OSError, sqlite3.Error) as exc:
            log.warning("health could not be read (%s)", type(exc).__name__)
            return
        for health in states:
            name = HEALTH_NAMES[health.component]
            try:
                if health.recovery_due:
                    self.send(f"Health: {name} is working again.")
                    with self._search() as search, db.transaction(search):
                        search_store.mark_recovery_told(search, health.component)
                if health.failures >= limit and health.alerted_failures is None:
                    self.send(
                        _failure_text(name, health.failures, health.held, health.last_failure)
                    )
                    with self._search() as search, db.transaction(search):
                        search_store.mark_failure_told(
                            search, health.component, health.failures, limit
                        )
            except (TelegramError, OSError, sqlite3.Error) as exc:
                log.warning(
                    "health message failed (%s); left for the next poll", type(exc).__name__
                )
                return

    def send_weekly_digest(self) -> None:
        """Counts by lane for the past week, once a week (#43).

        At `alerts.weekly_digest` (a day and an hour in schedule.timezone), the first
        poll at or after that moment sends one message covering the seven days before
        it: per lane, the alerts, Applied, Skipped, responses and outcomes. Counts and
        lane names only: no titles, notes or gap names. The week is recorded in
        `bot_state` once Telegram accepts the message, so it goes once; a failed send
        goes again next poll. A bot that was down for longer sends only the latest week.
        Nothing is sent while config.yaml can't be read.
        """
        doc = self._alert_config()
        if doc is None:
            return
        when = weekly_digest(doc)
        if when is None:
            return
        zone = config_timezone(doc)
        start, end = _digest_week(self.clock(), zone, *when)
        week = end.astimezone(zone).date().isoformat()
        try:
            with self._search() as search:
                if search_store.bot_state(search, WEEKLY_KEY) == week:
                    return
                counts = search_store.weekly_counts(search, _utc(start), _utc(end))
        except (OSError, sqlite3.Error) as exc:
            log.warning("weekly digest could not be read (%s)", type(exc).__name__)
            return
        text = _weekly_text(start.astimezone(zone), end.astimezone(zone), counts)
        try:
            self.send(text)
            with self._search() as search, db.transaction(search):
                search_store.set_bot_state(search, WEEKLY_KEY, week)
        except (TelegramError, OSError, sqlite3.Error) as exc:
            log.warning("weekly digest failed (%s); left for the next poll", type(exc).__name__)

    def send_pending_discovery(self) -> None:
        """Send pending discovery rows that are due. Name and URL only.

        Five or fewer are one message each. More than five are one digest of at
        most eight, and the rest wait for a later poll. ``notified_at`` is set
        only after Telegram accepts the message. Each row of a digest has its own
        Approve and Reject tokens, so one tap leaves the other rows' buttons live (#41).
        Nothing is sent while config.yaml can't be read, like the watcher alerts.
        """
        if self._alert_config() is None:
            return
        cutoff = _utc(self.clock() - ALERT_TTL)
        try:
            with self._search() as search:
                rows = discovery.due(search, notified_before=cutoff)
        except (OSError, sqlite3.Error) as exc:
            log.warning("pending discovery could not be read (%s)", type(exc).__name__)
            return
        if len(rows) <= DISCOVERY_EACH:
            batches: list[list[discovery.Queued]] = [[row] for row in rows]
        else:
            batches = [list(rows[:DISCOVERY_DIGEST])]
        for batch in batches:
            try:
                with self._search() as search:
                    self._send_discovery(search, batch)
            except (TelegramError, OSError, sqlite3.Error) as exc:
                log.warning(
                    "discovery send failed (%s); left for the next poll", type(exc).__name__
                )

    def _send_discovery(self, search: sqlite3.Connection, rows: list[discovery.Queued]) -> None:
        text = "\n\n".join(f"{row.suggested_name}\n{row.url}" for row in rows)
        buttons = [
            [
                ("Approve", "discover_approve", {"candidate_id": row.id}),
                ("Reject", "discover_reject", {"candidate_id": row.id}),
            ]
            for row in rows
        ]
        self._keyboard(search, text, buttons, ALERT_TTL, per_row=True)
        discovery.mark_notified(search, [row.id for row in rows], self._now())

    # --- entry point -------------------------------------------------------------------

    def handle(self, update: dict[str, Any]) -> str:
        """Handle one update. Returns a short event label for the log (no content)."""
        if not isinstance(update, dict):
            return "ignored:malformed"
        if "callback_query" in update:
            return self._callback(update["callback_query"])
        message = update.get("message")
        if isinstance(message, dict):
            return self._message(message)
        return "ignored:kind"

    def handle_safely(self, update: dict[str, Any]) -> str:
        if self._stitch is not None and not self._keeps_stitch(update):
            self.close_stitch()
        return self._guarded(lambda: self.handle(update))

    # --- stitching a long JD ---------------------------------------------------------

    @property
    def stitching(self) -> bool:
        return self._stitch is not None

    def _is_fragment(self, message: Mapping[str, Any]) -> bool:
        """Plain text from the allowlisted chat: not a command, a reply, a forward or a file."""
        text = message.get("text")
        return (
            self._allowed(message.get("chat"), message.get("from"))
            and isinstance(text, str)
            and not text.startswith("/")
            and "reply_to_message" not in message
            and not any(key.startswith("forward") for key in message)
            and not any(kind in message for kind in FILE_KINDS)
        )

    def _in_window(self, since: dt.datetime, seconds: float) -> bool:
        return self.clock() - since <= dt.timedelta(seconds=seconds)

    def _keeps_stitch(self, update: object) -> bool:
        """True when this update leaves an open stitch open: a fragment in time, or a
        message from another chat, which is ignored anyway."""
        assert self._stitch is not None  # noqa: S101 - only called while stitching
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return False
        if not self._allowed(message.get("chat"), message.get("from")):
            return True
        return self._is_fragment(message) and self._in_window(self._stitch.last, JD_STITCH_SECONDS)

    def tick(self) -> None:
        """Close the stitch once JD_STITCH_SECONDS have passed since its last message."""
        if self._stitch is not None and not self._in_window(self._stitch.last, JD_STITCH_SECONDS):
            self.close_stitch()

    def close_stitch(self) -> str:
        """Record the /log whose JD was being stitched. Returns the event label."""
        stitch, self._stitch = self._stitch, None
        if stitch is None:
            return "log:none"
        log.info("job description stitched from %d messages", len(stitch.parts))
        event = self._guarded(lambda: self._finish_stitch(stitch))
        log.info("update handled: %s", event)
        return event

    def _finish_stitch(self, stitch: _Stitch) -> str:
        # Telegram splits at a blank line and drops the whitespace there, so a blank line
        # goes back between the parts; otherwise words fuse across the split.
        joined = STITCH_SEPARATOR.join(stitch.parts)
        jd: jobdesc.JobDescription | None = None
        if joined.strip():
            try:
                jd = jobdesc.from_paste(joined)
            except jobdesc.JobDescError as exc:
                raise Refusal(f"Not recorded: {exc}") from None
        doc = self._read_config()
        self._log_now(
            stitch.url, stitch.method, stitch.dating, stitch.company, stitch.title, doc, jd
        )
        return "log:stitched"

    def _guarded(self, work: Callable[[], str]) -> str:
        try:
            return work()
        except Refusal as exc:
            self.send(str(exc))
            return "refused"
        except PartialWriteError:
            log.error("partial write: the EI record was saved, search.db was not")
            self.send("Saved in the EI record, but search.db failed. Run careerhunter doctor.")
            return "error:partial-write"
        except ClaimError:
            self.send("Not recorded: the EI record refused it. Run careerhunter doctor.")
            return "error:claim"
        except Exception as exc:
            log.error("handler failed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):  # the failure is already logged
                self.send("Something went wrong and nothing more was recorded. See the bot log.")
            return f"error:{type(exc).__name__}"

    def _message(self, message: dict[str, Any]) -> str:
        chat, sender = message.get("chat"), message.get("from")
        if not self._allowed(chat, sender):
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            log.info(
                "ignored update from a chat not on the allowlist (chat %s)", chat_hash(chat_id)
            )
            return "ignored:chat"
        if any(key.startswith("forward") for key in message):
            return "ignored:forward"
        if self._stitch is not None and self._is_fragment(message):
            self._stitch.parts.append(str(message["text"]))
            self._stitch.last = self.clock()
            return "log:fragment"
        reply_to = message.get("reply_to_message")
        if isinstance(reply_to, dict) and isinstance(reply_to.get("message_id"), int):
            return self._prompt_reply(message, reply_to["message_id"])
        text = message.get("text")
        if isinstance(text, str) and text.startswith("/"):
            first, newline, body = text.partition("\n")
            if newline and _is_log(first):
                self._cmd_log(first.strip().partition(" ")[2], body=body)
                return "command:log"
            return self._command(text)
        caption = message.get("caption")
        document = message.get("document")
        if isinstance(document, dict) and isinstance(caption, str) and _is_log(caption):
            if len(caption) > MAX_CAPTION or "\n" in caption.strip():
                raise Refusal(JD_NEEDS_FULL_LOG)
            self._cmd_log(caption.strip().partition(" ")[2], document=document)
            return "command:log"
        if "document" in message or "photo" in message:
            self.send("To attach a job ad, reply to the message that asked for it.")
            return "file:unprompted"
        if (
            isinstance(text, str)
            and self._jd_saved_at is not None
            and self._in_window(self._jd_saved_at, LATE_FRAGMENT_SECONDS)
        ):
            self.send(JD_LATE)
            return "log:late-fragment"
        self.send(HELP)
        return "help"

    # --- commands ----------------------------------------------------------------------

    def _command(self, text: str) -> str:
        head, _, rest = text.strip().partition(" ")
        command = head.split("@", 1)[0].lower()
        rest = rest.strip()
        if command in ("/start", "/help"):
            self.send(HELP)
        elif command == "/log":
            self._cmd_log(rest)
        elif command == "/status":
            self._cmd_status(rest)
        elif command == "/activity":
            self._cmd_activity(rest)
        elif command == "/recent":
            self._cmd_recent()
        else:
            self.send("I don't know that command.\n\n" + HELP)
            return "command:unknown"
        return f"command:{command[1:]}"

    def _cmd_log(
        self, rest: str, *, body: str | None = None, document: dict[str, Any] | None = None
    ) -> None:
        """A self-sourced application (#34). `/log <link> | method | date` records it at
        once; a bare `/log <link>` asks for the method, then the date. Either way the
        posting and the application are written together, by one ledger call, only at
        the end. `| Company | Title` after the date fills in what the page doesn't show.

        `body` is the text after the /log line: the JD, which may go on in the messages
        that follow, so it is recorded when the stitch window closes. `document` is a JD
        file whose caption was the /log line. Both need the method and the date.

        Fails closed without config.yaml: no date and no claim check, so nothing is
        fetched or written, and the reply is the fixed CONFIG_UNREADABLE text.
        """
        parts = [p.strip() for p in rest.split("|")]
        url, extra = parts[0], parts[1:]
        if not url or len(extra) not in (0, 2, 4) or any(not p for p in extra):
            raise Refusal(LOG_USAGE)
        if len(url) > selfsourced.MAX_URL or len(" ".join(extra)) > MAX_TEXT:
            raise Refusal("That's too long.")
        if (body is not None or document is not None) and not extra:
            raise Refusal(JD_NEEDS_FULL_LOG)
        doc = self._read_config()  # fails closed: no date and no claim check without it
        today = self._today(doc)
        method: str | None = None
        dating: Dating | None = None
        company, title = (extra[2], extra[3]) if len(extra) == 4 else (None, None)
        try:
            if extra:
                method = selfsourced.parse_method(extra[0])
                dating = selfsourced.parse_day(extra[1], today)
        except selfsourced.LogError as exc:
            raise Refusal(str(exc)) from None
        if urls.no_fetch(url) and not (company and title):
            raise Refusal(LINKEDIN_REFUSAL)
        if body is not None and method is not None and dating is not None:
            self._stitch = _Stitch(url, method, dating, company, title, [body], self.clock())
            return
        jd = self._document_jd(document) if document is not None else None
        if method is not None and dating is not None:
            self._log_now(url, method, dating, company, title, doc, jd)
            return
        posting = self._describe(url, today, company, title)
        found = {
            "url": posting.url or url,
            "company": posting.company,
            "title": posting.title,
            "location": posting.location,
        }
        if method is None or dating is None:
            rows = [
                [(a, "log_method", {"log": found, "method": m}) for a, m in pair]
                for pair in (METHOD_BUTTONS[i : i + 2] for i in range(0, len(METHOD_BUTTONS), 2))
            ]
            with self._search() as search:
                self._keyboard(
                    search,
                    f"{posting.company} — {posting.title}\nHow did you apply?",
                    rows,
                    STEP_TTL,
                )
            return
        self._finish_log(found, method, dating, doc)

    def _describe(
        self, url: str, today: dt.date, company: str | None, title: str | None
    ) -> Posting:
        try:
            return selfsourced.describe(url, self.fetcher, today, company=company, title=title)
        except selfsourced.LinkedInNamesMissing:
            raise Refusal(LINKEDIN_REFUSAL) from None
        except selfsourced.NamesMissing as exc:
            raise Refusal(f"{exc} Send: /log <link> | method | date | Company | Title") from None
        except selfsourced.LogError as exc:
            raise Refusal(str(exc)) from None

    def _log_now(
        self,
        url: str,
        method: str,
        dating: Dating,
        company: str | None,
        title: str | None,
        doc: dict[str, Any],
        jd: jobdesc.JobDescription | None,
    ) -> None:
        """Fetch what the page shows, then record the /log with its JD, if any."""
        posting = self._describe(url, self._today(doc), company, title)
        found = {
            "url": posting.url or url,
            "company": posting.company,
            "title": posting.title,
            "location": posting.location,
        }
        self._finish_log(found, method, dating, doc, jd)

    def _download(self, file: Mapping[str, Any]) -> bytes:
        if not isinstance(file.get("file_id"), str):
            raise Refusal("Please send the job description as a PDF, HTML or text file.")
        if int(file.get("file_size") or 0) > MAX_UPLOAD:
            raise Refusal("That file is over 20 MB.")
        info = self.api.call("getFile", {"file_id": file["file_id"]})
        return self.api.download(str(info.get("file_path", "")), MAX_UPLOAD)

    def _document_jd(self, document: Mapping[str, Any]) -> jobdesc.JobDescription:
        """A JD file sent to the bot, read from its bytes. An encrypted PDF is refused."""
        try:
            return jobdesc.from_file(self._download(document))
        except jobdesc.JobDescError as exc:
            raise Refusal(str(exc)) from None

    def _cmd_status(self, rest: str) -> None:
        parts = rest.split()
        with self._search() as search:
            if not parts:
                rows = search_store.recent_applications(search, 10)
                if not rows:
                    raise Refusal("No applications yet.")
                buttons = [
                    [
                        (
                            f"{self._ref(search, int(r['id']))} {r['company']} — {r['title']}"[:60],
                            "status_pick",
                            {"application_id": int(r["id"])},
                        )
                    ]
                    for r in rows
                ]
                self._keyboard(search, "Which application?", buttons, STEP_TTL)
                return
            if len(parts) != 2:
                raise Refusal("Send: /status <ref> <status>, or just /status to choose.")
            ref, status = parts[0].lower(), enums.normalize(parts[1])
            if status not in LATER_STATUSES:
                raise Refusal("The status must be one of: " + ", ".join(LATER_STATUSES))
            application_id = self._resolve_ref(search, ref)
        if application_id is None:
            raise Refusal("I don't know that reference. Send /recent for current ones.")
        self._finish_status(application_id, status)

    def _cmd_activity(self, rest: str) -> None:
        kind_text, _, remainder = rest.partition(" ")
        kind = enums.normalize(kind_text) if kind_text else ""
        if kind not in enums.ACTIVITY_TYPES:
            raise Refusal(
                "Send: /activity <type> [YYYY-MM-DD] <description>. The type is one of: "
                + ", ".join(enums.ACTIVITY_TYPES)
            )
        doc = self._config()
        first, _, after = remainder.strip().partition(" ")
        dating = None
        description = remainder.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", first):
            dating = parse_date(first, self._today(doc))
            description = after.strip()
        if not description:
            raise Refusal("Add a short description after the type (and optional date).")
        if len(description) > MAX_TEXT:
            raise Refusal("That description is too long.")
        if dating is not None:
            self._finish_activity(kind, description, dating)
            return
        payload = {"type": kind, "description": description}
        rows = [[(label, "activity_date", {**payload, "choice": c})] for label, c in DATE_BUTTONS]
        with self._search() as search:
            self._keyboard(search, "When was it?", rows, STEP_TTL)

    def _cmd_recent(self) -> None:
        with self._search() as search:
            rows = search_store.recent_applications(search, 10)
            if not rows:
                raise Refusal("No applications yet.")
            lines = [
                f"{self._ref(search, int(r['id']))} {r['company']} — {r['title']}: "
                f"{_words(r['status'] or 'applied')}"
                for r in rows
            ]
        self.send("\n".join(lines))

    # --- buttons -----------------------------------------------------------------------

    def _take_token(self, data: Any, message_id: Any) -> tuple[str, dict[str, Any]] | None:
        """Check a button's token and use up its whole keyboard. None if it's not valid."""
        if not isinstance(data, str) or not TOKEN_SHAPE.fullmatch(data):
            return None
        now = self._now()
        with self._search() as search:
            row = search.execute("SELECT * FROM bot_tokens WHERE token = ?", (data,)).fetchone()
            if (
                row is None
                or row["chat_id"] != self.chat_id
                or row["message_id"] is None
                or row["message_id"] != message_id
                or row["used_at"] is not None
                or row["expires_at"] <= now
            ):
                return None
            with db.transaction(search):
                taken = search.execute(
                    "UPDATE bot_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
                    (now, data),
                ).rowcount
                search.execute(
                    "UPDATE bot_tokens SET used_at = ? WHERE keyboard = ? AND used_at IS NULL",
                    (now, row["keyboard"]),
                )
            if taken != 1:
                return None
            payload = json.loads(row["payload"])
        return str(row["action"]), payload if isinstance(payload, dict) else {}

    def _live_buttons(self, message: Mapping[str, Any]) -> list[list[dict[str, str]]]:
        """The message's buttons whose tokens are still unused: a digest's other rows.

        A button is kept only when its token is in ``bot_tokens`` for this chat and
        message, unused and unexpired; its label is the one Telegram echoes back from
        the bot's own message. A plain alert or step keyboard is spent whole, so it
        comes back empty.
        """
        markup = message.get("reply_markup")
        rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
        if not isinstance(rows, list):
            return []
        with self._search() as search:
            live = {
                str(row["token"])
                for row in search.execute(
                    "SELECT token FROM bot_tokens WHERE chat_id = ? AND message_id = ? "
                    "AND used_at IS NULL AND expires_at > ?",
                    (self.chat_id, message.get("message_id"), self._now()),
                )
            }
        kept: list[list[dict[str, str]]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            buttons = [
                {"text": str(button.get("text", "")), "callback_data": button["callback_data"]}
                for button in row
                if isinstance(button, dict) and button.get("callback_data") in live
            ]
            if buttons:
                kept.append(buttons)
        return kept

    def _callback(self, query: Any) -> str:
        if not isinstance(query, dict):
            return "ignored:malformed"
        raw = query.get("message")
        message: dict[str, Any] = raw if isinstance(raw, dict) else {}
        chat, sender = message.get("chat"), query.get("from")
        if not self._allowed(chat, sender):
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            log.info(
                "ignored update from a chat not on the allowlist (chat %s)", chat_hash(chat_id)
            )
            return "ignored:chat"
        taken = self._take_token(query.get("data"), message.get("message_id"))
        if taken is None:
            self.api.call(
                "answerCallbackQuery",
                {"callback_query_id": query.get("id"), "text": "That button has expired."},
            )
            return "callback:rejected"
        self.api.call("answerCallbackQuery", {"callback_query_id": query.get("id")})
        with contextlib.suppress(TelegramError):  # the keyboard is spent; take it off
            self.api.call(
                "editMessageReplyMarkup",
                {
                    "chat_id": self.chat_id,
                    "message_id": message.get("message_id"),
                    "reply_markup": {"inline_keyboard": self._live_buttons(message)},
                },
            )
        action, payload = taken
        try:
            self._dispatch(action, payload)
        except discovery.DiscoverError as exc:
            log.info("discovery decision refused (%s)", type(exc).__name__)
            self.send("Not changed.")
            return f"callback:{action}:refused"
        return f"callback:{action}"

    def _dispatch(self, action: str, p: dict[str, Any]) -> None:
        if action == "apply":
            rows = [
                [(a, "apply_method", {"posting_id": p["posting_id"], "method": m}) for a, m in pair]
                for pair in (METHOD_BUTTONS[i : i + 2] for i in range(0, len(METHOD_BUTTONS), 2))
            ]
            with self._search() as search:
                self._keyboard(search, "How did you apply?", rows, STEP_TTL)
        elif action == "apply_method":
            if p.get("method") not in enums.METHODS:
                raise Refusal("That method isn't recognized.")
            rows = [[(label, "apply_date", {**p, "choice": c})] for label, c in DATE_BUTTONS]
            with self._search() as search:
                self._keyboard(search, "When did you apply?", rows, STEP_TTL)
        elif action == "apply_date":
            if p.get("choice") == "other":
                with self._search() as search:
                    self._prompt(
                        search,
                        "Reply with the date you applied, as YYYY-MM-DD.",
                        "apply_date",
                        {"posting_id": p["posting_id"], "method": p["method"]},
                    )
                return
            dating = choice_dating(str(p.get("choice")), self._today(self._config()))
            self._finish_apply(int(p["posting_id"]), str(p["method"]), dating)
        elif action == "log_method":
            if p.get("method") not in enums.METHODS:
                raise Refusal("That method isn't recognized.")
            found = _log_fields(p.get("log"))
            rows = [
                [(label, "log_date", {"log": found, "method": p["method"], "choice": c})]
                for label, c in DATE_BUTTONS
            ]
            with self._search() as search:
                self._keyboard(search, "When did you apply?", rows, STEP_TTL)
        elif action == "log_date":
            found = _log_fields(p.get("log"))
            if p.get("choice") == "other":
                with self._search() as search:
                    self._prompt(
                        search,
                        "Reply with the date you applied, as YYYY-MM-DD.",
                        "apply_date",
                        {"log": found, "method": p.get("method")},
                    )
                return
            doc = self._read_config()
            dating = choice_dating(str(p.get("choice")), self._today(doc))
            self._finish_log(found, str(p.get("method")), dating, doc)
        elif action == "skip":
            rows = [
                [(a, "skip_reason", {"posting_id": p["posting_id"], "reason": r}) for a, r in pair]
                for pair in (SKIP_BUTTONS[i : i + 2] for i in range(0, len(SKIP_BUTTONS), 2))
            ]
            with self._search() as search:
                self._keyboard(search, "Why skip it?", rows, STEP_TTL)
        elif action == "skip_reason":
            if p.get("reason") == "other":
                with self._search() as search:
                    self._prompt(
                        search,
                        "Reply with the reason, in a few words.",
                        "skip_reason",
                        {"posting_id": p["posting_id"]},
                    )
                return
            self._finish_skip(int(p["posting_id"]), str(p["reason"]))
        elif action == "status_pick":
            rows = [
                [(_words(s), "status_set", {"application_id": p["application_id"], "status": s})]
                for s in LATER_STATUSES
            ]
            with self._search() as search:
                self._keyboard(search, "New status?", rows, STEP_TTL)
        elif action == "status_set":
            self._finish_status(int(p["application_id"]), str(p["status"]))
        elif action in ("discover_approve", "discover_reject"):
            decision = "approve" if action == "discover_approve" else "reject"
            self._finish_discovery(p.get("candidate_id"), decision)
        elif action == "activity_date":
            if p.get("choice") == "other":
                with self._search() as search:
                    self._prompt(
                        search,
                        "Reply with the date of the activity, as YYYY-MM-DD.",
                        "activity_date",
                        {"type": p["type"], "description": p["description"]},
                    )
                return
            dating = choice_dating(str(p.get("choice")), self._today(self._config()))
            self._finish_activity(str(p["type"]), str(p["description"]), dating)
        else:
            raise Refusal("That button isn't recognized.")

    # --- replies to the bot's questions ------------------------------------------------

    def _prompt_reply(self, message: dict[str, Any], message_id: int) -> str:
        now = self._now()
        with self._search() as search:
            row = search.execute(
                "SELECT * FROM bot_prompts WHERE chat_id = ? AND message_id = ?",
                (self.chat_id, message_id),
            ).fetchone()
            usable = row is not None and row["used_at"] is None and row["expires_at"] > now
            if usable:
                taken = search.execute(
                    "UPDATE bot_prompts SET used_at = ? WHERE chat_id = ? AND message_id = ? "
                    "AND used_at IS NULL",
                    (now, self.chat_id, message_id),
                ).rowcount
                usable = taken == 1
        raw_text = message.get("text")
        text: str = raw_text if isinstance(raw_text, str) else ""
        if not usable:
            if text.startswith("/"):
                return self._command(text)
            raise Refusal("That question has expired. Start again from the command.")
        assert row is not None  # noqa: S101 - usable implies a row
        kind, payload = str(row["kind"]), json.loads(row["payload"])
        try:
            if kind == "apply_date" and "log" in payload:
                doc = self._read_config()
                dating = parse_date(text, self._today(doc))
                found = _log_fields(payload["log"])
                self._finish_log(found, str(payload.get("method")), dating, doc)
            elif kind == "apply_date":
                dating = parse_date(text, self._today(self._config()))
                self._finish_apply(int(payload["posting_id"]), str(payload["method"]), dating)
            elif kind == "activity_date":
                dating = parse_date(text, self._today(self._config()))
                self._finish_activity(str(payload["type"]), str(payload["description"]), dating)
            elif kind == "skip_reason":
                reason = " ".join(text.split())
                if not reason or len(reason) > MAX_TEXT:
                    raise Refusal("Please reply with a short reason.")
                self._finish_skip(int(payload["posting_id"]), reason)
            elif kind == "snapshot" and payload.get("jd") is True:
                self._attach_jd(message, str(payload["correlation_id"]))
            elif kind == "snapshot":
                self._attach_upload(message, str(payload["correlation_id"]))
        except Refusal as exc:
            # Ask again, so a typo doesn't lose the whole flow.
            with self._search() as search:
                self._prompt(search, f"{exc} Reply to this message to try again.", kind, payload)
            return f"reply:{kind}:retry"
        return f"reply:{kind}"

    # --- the writes --------------------------------------------------------------------

    def _finish_discovery(self, candidate_id: object, decision: str) -> None:
        """The same approve or reject as the CLI. search.db only."""
        if isinstance(candidate_id, bool) or not isinstance(candidate_id, int) or candidate_id < 1:
            raise discovery.DiscoverError("not a candidate")
        with self._search() as search:
            row = ledger.record_discovery_decision(search, candidate_id, decision)
        word = "Approved" if decision == "approve" else "Rejected"
        self.send(f"{word}: {row.suggested_name}\n{row.url}")

    def _finish_apply(self, posting_id: int, method: str, dating: Dating) -> None:
        if method not in enums.METHODS:
            raise Refusal("That method isn't recognized.")
        doc = self._config()
        today = self._today(doc)
        with self._writable(doc) as (search, active):
            posting = search_store.posting_by_id(search, posting_id)
            if posting is None:
                raise Refusal("That posting isn't in search.db any more.")
            if active is not None and check_dating(active.claim, dating)[0] is Verdict.REFUSE:
                raise Refusal("Not recorded: that date is outside the active EI claim.")
            path = posting.snapshot_path
            if path is None and posting.url:
                captured = snapshots.capture(posting.url, self.fetcher)
                if captured is not None:
                    path = snapshots.save(self.datadir.root, captured.data, captured.ext, today)
            app = Application(replace(posting, snapshot_path=path), dating, method)
            try:
                recorded = ledger.record_application(search, active, app, lane_config=doc)
            except sqlite3.IntegrityError:
                raise Refusal("That application is already recorded.") from None
            if path and posting.snapshot_path is None:
                search_store.set_posting_snapshot(search, posting_id, path)
            # The watcher keeps no description (it is used in memory for the filters and
            # scoring only), so an application from an alert always asks for its JD.
            self._confirm(
                search,
                recorded.correlation_id,
                posting.company,
                posting.title,
                bool(path),
                jd="missing",
            )

    def _finish_log(
        self,
        found: dict[str, str],
        method: str,
        dating: Dating,
        doc: dict[str, Any],
        jd: jobdesc.JobDescription | None = None,
    ) -> None:
        """Write a self-sourced posting and its application in one ledger call (#34)."""
        if method not in enums.METHODS:
            raise Refusal("That method isn't recognized.")
        today = self._today(doc)
        posting = Posting(
            company=found["company"],
            title=found["title"],
            first_seen=today.isoformat(),
            location=found["location"],
            url=found["url"],
            source="self_sourced",
        )
        with self._writable(doc) as (search, active):
            try:
                logged = selfsourced.record(
                    self.datadir.root,
                    search,
                    active,
                    posting,
                    method,
                    dating,
                    today,
                    self.fetcher,
                    jd,
                    lane_config=doc,
                )
            except selfsourced.LogError as exc:
                raise Refusal(str(exc)) from None
            state = (
                "saved"
                if logged.jd_saved
                else ("no_text" if logged.jd_file_without_text else "missing")
            )
            self._confirm(
                search,
                logged.correlation_id,
                logged.company,
                logged.title,
                logged.snapshot_saved,
                earlier=logged.earlier,
                jd=state,
            )

    def _confirm(
        self,
        search: sqlite3.Connection,
        correlation_id: str,
        company: str,
        title: str,
        saved: bool,
        *,
        jd: str,
        earlier: str | None = None,
    ) -> None:
        """`Recorded: … (ref aN).`, then the request for the JD when none was saved.

        `jd` is `saved`, `missing` or `no_text`, for /log and for Applied alike. A saved
        JD can be replaced by replying to the Recorded message; otherwise the JD is asked
        for, in place of the copy of the ad (a JD file is that copy too). `earlier` is
        the one line naming the date the same role was logged before (#34).
        """
        app_row = search_store.application_by_correlation(search, correlation_id)
        assert app_row is not None  # noqa: S101 - just recorded
        ref = self._ref(search, int(app_row["id"]))
        text = f"Recorded: {company} — {title} (ref {ref})."
        if saved and jd != "saved":
            text += " Job ad saved."
        if jd == "saved":
            text += " Job description saved."
        if earlier:
            text += "\n" + earlier
        message_id = self.send(text)
        payload = {"correlation_id": correlation_id, "jd": True}
        if jd == "saved":
            self._jd_saved_at = self.clock()
            self._expect_reply(search, message_id, "snapshot", payload, ALERT_TTL)
            return
        self._prompt(
            search, JD_REQUEST if jd == "missing" else JD_NO_TEXT, "snapshot", payload, ALERT_TTL
        )

    def _read_config(self) -> dict[str, Any]:
        """config.yaml for a self-sourced write; unreadable fails closed with fixed text."""
        try:
            return self._config()
        except (ConfigError, OSError, yaml.YAMLError, ValueError):
            raise Refusal(CONFIG_UNREADABLE) from None

    def _finish_skip(self, posting_id: int, reason: str) -> None:
        doc = self._config()
        with self._writable(doc) as (search, _):
            posting = search_store.posting_by_id(search, posting_id)
            if posting is None:
                raise Refusal("That posting isn't in search.db any more.")
            ledger.record_skip(search, posting, self._today(doc).isoformat(), reason)
        self.send(f"Skipped: {posting.company} — {posting.title}.")

    def _finish_status(self, application_id: int, status: str) -> None:
        if status not in LATER_STATUSES:
            raise Refusal("That status isn't recognized.")
        doc = self._config()
        with self._writable(doc) as (search, active):
            summary = search_store.application_summary(search, application_id)
            if summary is None:
                raise Refusal("That application isn't in search.db any more.")
            ledger.record_status(
                search, active, summary["correlation_id"], status, self._today(doc).isoformat()
            )
        self.send(f"{summary['company']} — {summary['title']}: {_words(status)}.")

    def _finish_activity(self, kind: str, description: str, dating: Dating) -> None:
        if kind not in enums.ACTIVITY_TYPES:
            raise Refusal("That activity type isn't recognized.")
        doc = self._config()
        with self._writable(doc) as (_, active):
            if active is None:
                raise Refusal("Not recorded: no EI claim is active.")
            if check_dating(active.claim, dating)[0] is Verdict.REFUSE:
                raise Refusal("Not recorded: that date is outside the active EI claim.")
            new = ledger.record_activity(active, Activity(dating, kind, description))
        if new:
            self.send(f"Recorded activity: {_words(kind)}.")
        else:
            self.send("That activity is already recorded.")

    def _attach_jd(self, message: dict[str, Any], correlation_id: str) -> None:
        """A JD sent as a reply, as text or as a file, attached through the ledger."""
        document, text = message.get("document"), message.get("text")
        if isinstance(document, dict):
            jd = self._document_jd(document)
        elif isinstance(text, str) and text.strip():
            try:
                jd = jobdesc.from_paste(text)
            except jobdesc.JobDescError as exc:
                raise Refusal(str(exc)) from None
        else:
            raise Refusal(
                "Please send the job description as text, or as a PDF, HTML or text file."
            )
        doc = self._config()
        with self._writable(doc) as (search, active):
            saved = jobdesc.save(self.datadir.root, jd, self._today(doc))
            try:
                ledger.attach_job_description(search, active, correlation_id, saved)
            except ledger.AttachRefused as exc:
                raise Refusal(str(exc)) from None
        if not jd.has_text:
            raise Refusal(JD_NO_TEXT_AGAIN)
        self._jd_saved_at = self.clock()
        self.send("Job description saved.")

    def _attach_upload(self, message: dict[str, Any], correlation_id: str) -> None:
        document, photo = message.get("document"), message.get("photo")
        file: Any = document if isinstance(document, dict) else None
        if file is None and isinstance(photo, list) and photo:
            sizes = [p for p in photo if isinstance(p, dict)]
            file = max(sizes, key=lambda p: p.get("file_size", 0)) if sizes else None
        if not isinstance(file, dict) or not isinstance(file.get("file_id"), str):
            raise Refusal("Please send the job ad as a PDF or an image.")
        if int(file.get("file_size") or 0) > MAX_UPLOAD:
            raise Refusal("That file is over 20 MB.")
        info = self.api.call("getFile", {"file_id": file["file_id"]})
        data = self.api.download(str(info.get("file_path", "")), MAX_UPLOAD)
        ext = snapshots.upload_type(data)
        if ext is None:
            raise Refusal("That isn't a PDF, PNG or JPEG.")
        doc = self._config()
        with self._writable(doc) as (search, active):
            path = snapshots.save(self.datadir.root, data, ext, self._today(doc))
            ledger.attach_snapshot(search, active, correlation_id, path)
        self.send("Job ad saved.")


# --- starting and running ---------------------------------------------------------------


class _Redact(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._token)
        record.args = None
        return True


def setup_logging(datadir: DataDir, token: str) -> None:
    """Events only, never message text, into logs/bot.log (0600, rotated)."""
    path = datadir.logs / "bot.log"
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5)
    ]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.addFilter(_Redact(token))
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _is_log(line: str) -> bool:
    words = line.split(maxsplit=1)
    return bool(words) and words[0].split("@", 1)[0].lower() == "/log"


def startup(
    datadir: DataDir,
    *,
    secret: Callable[[str], str | None] = get_secret,
    api_factory: Callable[[str], TelegramAPI] = HttpTelegram,
) -> tuple[TelegramAPI, int, str]:
    """Every check the bot needs before it may poll. Raises BotStartupError otherwise."""
    try:
        doc = load_config(datadir.config_path)
    except (ConfigError, OSError) as exc:
        raise BotStartupError(f"config.yaml: {exc}") from None
    chat_id = telegram_chat_id(doc)
    if chat_id is None:
        raise BotStartupError(
            "telegram.chat_id isn't set in config.yaml (find it with: careerhunter bot discover)"
        )
    try:
        claims.require_write_ready(datadir, claims.settings_from_config(doc))
    except ClaimError as exc:
        raise BotStartupError(str(exc)) from None
    token = _token(secret)
    api = api_factory(token)
    try:
        me = api.call("getMe")
    except TelegramError as exc:
        raise BotStartupError(f"Telegram refused the token or couldn't be reached: {exc}") from None
    if not isinstance(me, dict) or me.get("is_bot") is not True:
        raise BotStartupError("Telegram's getMe didn't describe a bot")
    return api, chat_id, token


def _token(secret: Callable[[str], str | None]) -> str:
    try:
        token = secret("telegram_bot_token")
    except SecretError as exc:
        raise BotStartupError(str(exc)) from None
    if not token:
        raise BotStartupError(
            "telegram_bot_token isn't in the Keychain "
            "(run: careerhunter secret set telegram_bot_token)"
        )
    return token


def run(
    datadir: DataDir,
    *,
    secret: Callable[[str], str | None] = get_secret,
    api_factory: Callable[[str], TelegramAPI] = HttpTelegram,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    fetcher: Callable[[str], Fetched] = fetch,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
) -> int:
    os.umask(0o077)  # everything the bot creates is owner-only
    api, chat_id, token = startup(datadir, secret=secret, api_factory=api_factory)
    setup_logging(datadir, token)
    bot = Bot(datadir, api, chat_id, clock=clock, fetcher=fetcher)
    log.info("started; polling Telegram")
    offset, failures, polls = bot.offset(), 0, 0
    while max_polls is None or polls < max_polls:
        polls += 1
        params: dict[str, Any] = {
            "timeout": STITCH_POLL_TIMEOUT if bot.stitching else POLL_TIMEOUT,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        try:
            updates = api.call("getUpdates", params)
        except TelegramError as exc:
            failures += 1
            log.warning("getUpdates failed (%s in a row): %s", failures, exc)
            sleep(min(60.0, 2.0**failures))
            bot.tick()
            continue
        failures = 0
        batch = [u for u in updates if isinstance(u, dict)] if isinstance(updates, list) else []
        # A long paste's fragments in one batch are added in update_id order.
        batch.sort(key=lambda u: u["update_id"] if isinstance(u.get("update_id"), int) else 0)
        for update in batch:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(update_id, int):
                # Acknowledge first, so an update that crashes the handler isn't retried
                # for ever. Buttons are single-use anyway, so a replay could do nothing.
                offset = update_id + 1
                bot.save_offset(offset)
            log.info("update handled: %s", bot.handle_safely(update))
        bot.tick()
        bot.heartbeat()
        bot.send_pending_alerts()
        bot.send_pending_discovery()
        bot.send_health()
        bot.send_weekly_digest()
    return 0


def discover(
    datadir: DataDir,
    *,
    secret: Callable[[str], str | None] = get_secret,
    api_factory: Callable[[str], TelegramAPI] = HttpTelegram,
) -> list[tuple[int, str]]:
    """The chats that have messaged the bot, for finding telegram.chat_id. Never replies,
    never shows message content, and leaves the updates for the real bot."""
    api = api_factory(_token(secret))
    try:
        updates = api.call("getUpdates", {"timeout": 0})
    except TelegramError as exc:
        raise BotStartupError(f"Telegram: {exc}") from None
    found: dict[int, str] = {}
    for update in updates if isinstance(updates, list) else []:
        source = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        chat = source.get("chat") if isinstance(source, dict) else None
        if isinstance(chat, dict) and isinstance(chat.get("id"), int):
            found[chat["id"]] = str(chat.get("type", "?"))
    return sorted(found.items())
