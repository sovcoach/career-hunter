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
  Before every write the bot re-reads config and re-checks the claims, as the CLI does.

Privacy (ADR-015): messages the bot sends carry only company, title, link, status
words and references. Never notes, fit, EI record details or claim labels.

Dates: after Applied and a method, the bot asks when, with buttons (today, yesterday,
this week, last week, or another date typed as YYYY-MM-DD). Exact days record precision
`exact`; the week buttons record `week`. /activity takes an optional YYYY-MM-DD, else
asks the same way. Future dates are refused, and everything passes the claim check.
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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from careerhunter import claims, db, enums, ledger, search_store, snapshots
from careerhunter.claims import ActiveClaim, ClaimError, Verdict, check_dating
from careerhunter.config import ConfigError, load_config, telegram_chat_id
from careerhunter.config import today as config_today
from careerhunter.fetch import Fetched, FetchError, fetch
from careerhunter.ledger import PartialWriteError
from careerhunter.models import Activity, Application, Dating, Posting
from careerhunter.search_store import IdentityConflict
from careerhunter.secrets_store import SecretError, get_secret
from careerhunter.seed import monday_of
from careerhunter.storage_guard import DataDir
from careerhunter.telegram import HttpTelegram, TelegramAPI, TelegramError, redact

log = logging.getLogger("careerhunter.bot")

POLL_TIMEOUT = 50
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{16,64}")
REF_SHAPE = re.compile(r"a[1-9][0-9]{0,6}")
ALERT_TTL = dt.timedelta(days=7)
STEP_TTL = dt.timedelta(days=1)
REF_TTL = dt.timedelta(days=7)
MAX_UPLOAD = 20 * 1024 * 1024
MAX_TEXT = 500

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

HELP = (
    "Commands:\n"
    "/log <link>: record a posting you found (or /log <link> | Company | Title)\n"
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
    ) -> int:
        keyboard_id = secrets.token_urlsafe(9)
        expires = self._later(ttl)
        markup = []
        with db.transaction(search):
            for row in rows:
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
        search.execute(
            "UPDATE bot_tokens SET message_id = ? WHERE keyboard = ?", (message_id, keyboard_id)
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
        try:
            return self.handle(update)
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
        reply_to = message.get("reply_to_message")
        if isinstance(reply_to, dict) and isinstance(reply_to.get("message_id"), int):
            return self._prompt_reply(message, reply_to["message_id"])
        text = message.get("text")
        if isinstance(text, str) and text.startswith("/"):
            return self._command(text)
        if "document" in message or "photo" in message:
            self.send("To attach a job ad, reply to the message that asked for it.")
            return "file:unprompted"
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

    def _cmd_log(self, rest: str) -> None:
        parts = [p.strip() for p in rest.split("|")]
        url, extra = parts[0], parts[1:]
        if not url or len(extra) not in (0, 2) or any(not p for p in extra):
            raise Refusal("Send: /log <link>, or /log <link> | Company | Title")
        if len(url) > 2000 or len(" ".join(extra)) > MAX_TEXT:
            raise Refusal("That's too long.")
        doc = self._config()
        today = self._today(doc)
        info = snapshots.PostingInfo()
        try:
            page = self.fetcher(url)
            info = snapshots.posting_info(page.body)
        except FetchError as exc:
            if not extra:
                raise Refusal(f"I couldn't read that link: {exc}.") from None
        company = extra[0] if extra else info.company
        title = extra[1] if extra else info.title
        if not company or not title:
            raise Refusal(
                "I couldn't find the company and title on that page. "
                "Send: /log <link> | Company | Title"
            )
        posting = Posting(
            company=company,
            title=title,
            first_seen=today.isoformat(),
            location=info.location or "",
            url=url,
            source="self_sourced",
        )
        with self._writable(doc) as (search, _):
            try:
                with db.transaction(search):
                    _, posting_id, _, _ = search_store.find_or_create_posting(
                        search, posting, Dating("exact", day=today.isoformat())
                    )
            except IdentityConflict:
                raise Refusal("That link is already recorded for another company.") from None
            self.alert(search, posting_id)

    def alert(self, search: sqlite3.Connection, posting_id: int) -> None:
        """A posting alert with Applied and Skipped buttons: company, title, link only."""
        posting = search_store.posting_by_id(search, posting_id)
        if posting is None:
            raise Refusal("That posting isn't in search.db any more.")
        text = f"{posting.company} — {posting.title}" + (f"\n{posting.url}" if posting.url else "")
        payload = {"posting_id": posting_id}
        self._keyboard(
            search, text, [[("Applied", "apply", payload), ("Skipped", "skip", payload)]], ALERT_TTL
        )

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
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        action, payload = taken
        self._dispatch(action, payload)
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
            if kind == "apply_date":
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
            elif kind == "snapshot":
                self._attach_upload(message, str(payload["correlation_id"]))
        except Refusal as exc:
            # Ask again, so a typo doesn't lose the whole flow.
            with self._search() as search:
                self._prompt(search, f"{exc} Reply to this message to try again.", kind, payload)
            return f"reply:{kind}:retry"
        return f"reply:{kind}"

    # --- the writes --------------------------------------------------------------------

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
                recorded = ledger.record_application(search, active, app)
            except sqlite3.IntegrityError:
                raise Refusal("That application is already recorded.") from None
            if path and posting.snapshot_path is None:
                search_store.set_posting_snapshot(search, posting_id, path)
            app_row = search_store.application_by_correlation(search, recorded.correlation_id)
            assert app_row is not None  # noqa: S101 - just recorded
            ref = self._ref(search, int(app_row["id"]))
            text = f"Recorded: {posting.company} — {posting.title} (ref {ref})."
            if path:
                self.send(text + " Job ad saved.")
            else:
                self.send(text)
                self._prompt(
                    search,
                    "No copy of the job ad was saved. Reply to this message with a PDF or "
                    "screenshot of it.",
                    "snapshot",
                    {"correlation_id": recorded.correlation_id},
                    ALERT_TTL,
                )

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
            "timeout": POLL_TIMEOUT,
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
            continue
        failures = 0
        for update in updates if isinstance(updates, list) else []:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(update_id, int):
                # Acknowledge first, so an update that crashes the handler isn't retried
                # for ever. Buttons are single-use anyway, so a replay could do nothing.
                offset = update_id + 1
                bot.save_offset(offset)
            log.info("update handled: %s", bot.handle_safely(update))
        bot.heartbeat()
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
