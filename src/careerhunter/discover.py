"""Queue company candidates from a local SearXNG.

The client is not `fetch.py`. It allows only plain http to a loopback host, follows
no redirects, and reads result URLs only. Titles, snippets, and the response body are
not stored. A down server yields one closed miss line and writes nothing.

Etiquette (#40). Queries of one pass are `discovery.query_delay_seconds` apart. An
HTTP 429 from SearXNG, or an engine SearXNG reports as unresponsive for a CAPTCHA or
too many requests, stops the pass at once with a closed line. A stopped pass and a
miss both write no queue row, leave the cursor where it was, and count as a failed
pass: the next pass waits `discovery.interval_minutes` times 2 to the number of failed
passes in a row, at most BACKOFF_CAP, and a pass that succeeds clears the count.

Approve and reject update this queue, and a board approval can fill a company row.
They do not rewrite config files or open an EI record. The bot may call them.
Sending the message is separate: `notified_at` is set only after Telegram accepts it.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import re
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from careerhunter import db, loopback, search_store
from careerhunter.ats import (
    Board,
    BoardHTTPError,
    BoardParseError,
    Confirmed,
    PostingRef,
    posting_url,
)
from careerhunter.config import (
    discovery_interval_minutes,
    discovery_queries_per_run,
    discovery_query_delay_seconds,
)
from careerhunter.deadline import Deadline
from careerhunter.fetch import FetchError

MAX_BODY = 1_048_576
MISS = "discovery miss: unreachable"
# The server answered, with another status or a body that is not a usable result list.
BAD_REPLY = "discovery miss: bad-reply"
OFF = "discovery off"
THROTTLED = "discovery stopped: rate limited or asked for a CAPTCHA"
BACKOFF_CAP = dt.timedelta(days=7)
# The share of the watch run's budget one discovery pass may use, in seconds. Config
# keeps queries_per_run times query_delay_seconds well inside it (config.py).
PASS_SECONDS = 150.0
# Engine errors in SearXNG's `unresponsive_engines` that mean "slow down". The English
# texts from searx/webutils.py; a suspended engine's text starts with "Suspended: ".
_THROTTLE_WORDS = ("captcha", "too many requests")
_TOTAL = 30.0
_TIMEOUT = 20.0
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_GREENHOUSE = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
_SMARTRECRUITERS = frozenset({"jobs.smartrecruiters.com", "careers.smartrecruiters.com"})
_SMARTRECRUITERS_POSTING = re.compile(r"^[0-9]+(?:-.*)?$")
# BambooHR subdomains that are BambooHR's own, not a company's careers site.
_BAMBOOHR_OWN = frozenset({"www", "api", "app", "help", "partners", "marketplace"})
_WORKDAY_HOST = re.compile(
    r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)\.(wd[0-9]{1,3})\.myworkdayjobs\.com$"
)
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# One query per host. `greenhouse.io` covers both board hosts in one query.
_QUERY_HOSTS = (
    "greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "jobs.smartrecruiters.com",
    "bamboohr.com",
    "myworkdayjobs.com",
)
_BOARD_URL = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "bamboohr": "https://{slug}.bamboohr.com/careers",
    "workday": "https://{slug}.{host}.myworkdayjobs.com/{site}",
}

Transport = Callable[[str], tuple[int, bytes]]
Confirm = Callable[[Board], Confirmed]  # one board-API request (ats.confirm_board)
CONFIRM_PER_PASS = 10  # boards confirmed in one pass; the rest wait to be found again


class DiscoverError(Exception):
    """A closed message. It carries no query and no result URL."""


@dataclass(frozen=True)
class Hit:
    """One reduced company. `url` is the board root or the career-page URL.

    A Workday board also has its wdN host and its site.
    """

    kind: str
    url: str
    ats_type: str | None
    ats_slug: str | None
    suggested_name: str
    ats_host: str | None = None
    ats_site: str | None = None


@dataclass(frozen=True)
class Queued:
    """One pending row, safe to print. No snippet and no response body."""

    id: int
    kind: str
    suggested_name: str
    ats_type: str | None
    ref: str
    url: str


@dataclass(frozen=True)
class Outcome:
    """`notice` is a closed stderr line. `rows` are the rows inserted on this run.

    `postings` are the posting URLs among the hits, for the watch run to fetch (#41).
    """

    notice: str | None
    rows: tuple[Queued, ...]
    postings: tuple[PostingRef, ...] = ()
    notes: tuple[str, ...] = ()  # boards not queued and why: excluded, gone, unconfirmed


@dataclass(frozen=True)
class Pass:
    """What a discovery pass inside the watch run hands back: its lines and postings."""

    lines: list[str]
    postings: tuple[PostingRef, ...] = ()


def request_url(base: str, query: str) -> str:
    """The loopback search URL for one query. Raises DiscoverError for any other host."""
    try:
        parts = urllib.parse.urlsplit(base)
        port = parts.port
    except ValueError as exc:
        raise DiscoverError(MISS) from exc
    host = parts.hostname
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or host not in _LOOPBACK
        or parts.query
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise DiscoverError(MISS)
    path = parts.path or ""
    trimmed = path.rstrip("/")
    if trimmed.endswith("/search"):
        path = trimmed
    elif path in ("", "/"):
        path = "/search"
    else:
        path = f"{trimmed}/search"
    qs = urllib.parse.urlencode({"q": query, "format": "json"})
    return urllib.parse.urlunsplit(("http", parts.netloc, path, qs, ""))


def default_transport(url: str, *, budget: Deadline | None = None) -> tuple[int, bytes]:
    """GET once, within _TOTAL. A redirect status is returned as-is and is not followed."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or 80
    except ValueError as exc:
        raise DiscoverError(MISS) from exc
    host = parts.hostname
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or host not in _LOOPBACK
    ):
        raise DiscoverError(MISS)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    try:
        return loopback.exchange(
            "GET",
            host,
            port,
            path,
            body=None,
            headers={"Accept": "application/json"},
            total=_TOTAL,
            per_op=_TIMEOUT,
            max_bytes=MAX_BODY,
            budget=budget,
        )
    except loopback.LoopbackError as exc:
        raise DiscoverError(MISS) from exc


def build_queries(config: Mapping[str, Any]) -> list[str]:
    """Every `site:` query, in rotation order (#40).

    The full product of titles (lanes in priority order, every variant), places (from
    `search.location_rules`: on-site and hybrid places, then remote regions) and hosts
    (each ATS once, then `discovery.career_hosts`). Each run takes the next slice of it
    (`take`). No titles means no queries; no places means queries without one.
    """
    places = _places(config) or [""]
    found: list[str] = []
    for title in _titles(config):
        for place in places:
            for host in _hosts(config):
                text = f"site:{host} {title}"
                found.append(f"{text} {place}" if place else text)
    return found


def take(queries: Sequence[str], cursor: int, count: int) -> tuple[list[str], int]:
    """The next `count` queries from `cursor`, wrapping around, and the cursor after them.

    Never more than the whole list, so one run asks no query twice. A cursor past the
    end (the list got shorter) wraps first.
    """
    if not queries:
        return [], 0
    start = cursor % len(queries)
    size = min(count, len(queries))
    chosen = [queries[(start + offset) % len(queries)] for offset in range(size)]
    return chosen, (start + size) % len(queries)


def classify(url: str, career_hosts: Sequence[str]) -> Hit | None:
    """A board or an allowlisted career page. Anything else is dropped."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.hostname is None
        or parts.port not in (None, 443)
    ):
        return None
    host = parts.hostname.casefold()
    segments = _segments(parts.path)
    if segments is None:
        return None
    board = _board(host, segments, parts.query)
    if board is not None:
        ats, slug, wd_host, site = board
        url = _BOARD_URL[ats].format(slug=slug, host=wd_host, site=site)
        return Hit("board", url, ats, slug, slug, wd_host, site)
    allowed = {item.casefold() for item in career_hosts if isinstance(item, str)}
    if host not in allowed:
        return None
    path = "/" + "/".join(segments) if segments else "/"
    return Hit("career", f"https://{host}{path}", None, None, host)


def format_row(row: Queued) -> str:
    """id, kind, suggested name, ATS type, and the slug or career URL."""
    ats = row.ats_type if row.ats_type else "-"
    return f"{row.id} {row.kind} {row.suggested_name} {ats} {row.ref}"


def pending(conn: sqlite3.Connection) -> list[Queued]:
    """Pending rows, oldest id first."""
    rows = conn.execute(
        "SELECT id, kind, suggested_name, ats_type, ats_slug, url "
        "FROM discovery_candidates WHERE status = 'pending' ORDER BY id"
    )
    return [_queued(row) for row in rows]


def due(conn: sqlite3.Connection, *, notified_before: str) -> list[Queued]:
    """Pending boards never sent, or last sent at or before `notified_before`.

    Career pages are no longer sent (#41); old pending ones stay listed by `pending`.
    """
    rows = conn.execute(
        "SELECT id, kind, suggested_name, ats_type, ats_slug, url "
        "FROM discovery_candidates "
        "WHERE status = 'pending' AND kind = 'board' "
        "AND (notified_at IS NULL OR notified_at <= ?) "
        "ORDER BY id",
        (notified_before,),
    )
    return [_queued(row) for row in rows]


def mark_notified(conn: sqlite3.Connection, candidate_ids: Sequence[int], notified_at: str) -> None:
    """Record that Telegram accepted the message for these rows."""
    for candidate_id in candidate_ids:
        conn.execute(
            "UPDATE discovery_candidates SET notified_at = ? WHERE id = ?",
            (notified_at, candidate_id),
        )


def approve(conn: sqlite3.Connection, candidate_id: int) -> Queued:
    """Approve one pending row. A board is watchable. A career page is only stored."""
    row = _pending_row(conn, candidate_id)
    with db.transaction(conn):
        if str(row["kind"]) == "board":
            _approve_board(conn, row)
        else:
            _approve_career(conn, row)
        _mark(conn, candidate_id, "approved")
    return _queued(row)


def reject(conn: sqlite3.Connection, candidate_id: int) -> Queued:
    """Mark one pending row rejected. No company is inserted."""
    row = _pending_row(conn, candidate_id)
    with db.transaction(conn):
        _mark(conn, candidate_id, "rejected")
    return _queued(row)


def _utc_now() -> dt.datetime:
    """The wall clock. Looked up at each call, so tests can replace it."""
    return dt.datetime.now(dt.UTC)


def _sleep(seconds: float) -> None:
    """The pause between queries. Looked up at each call, so tests can replace it."""
    time.sleep(seconds)


def run(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    *,
    today: str,
    transport: Transport | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], dt.datetime] | None = None,
    confirm: Confirm | None = None,
    exclusions: Mapping[str, Any] | None = None,
) -> Outcome:
    """Search, then insert new pending rows. A miss or a stop writes nothing.

    A pass inside a backoff sends nothing and says until when. `sleep` waits between
    queries; `now` stamps the backoff. Both default to the module's own, looked up at
    each call.

    The queue (#41): a board hit is queued only after `confirm` (one board-API request)
    says it exists, and never when it, or a company name the confirming reply gives,
    is in `exclusions.companies`. A board that does not exist is stored as `gone` and
    never queued again. Career pages are not queued: `discovery.career_hosts` only adds
    `site:` hosts. Without `confirm`, no board is queued.
    """
    clock = now or _utc_now
    base = _configured_url(config)
    if base is None:
        return Outcome(OFF, ())
    waiting = _backoff_until(conn)
    if waiting is not None and _stamp(clock()) < waiting:
        return Outcome(f"discovery backing off until {waiting}", ())
    every = build_queries(config)
    if not every:
        return Outcome(None, ())
    queries, after = take(every, _cursor(conn), discovery_queries_per_run(config))
    talk = transport or default_transport
    career_hosts = _career_hosts(config)
    delay = discovery_query_delay_seconds(config)
    hits: list[Hit] = []
    seen: set[tuple[str, ...]] = set()
    postings: dict[str, PostingRef] = {}  # by the posting's URL, first one kept
    pause = sleep or _sleep
    for index, query in enumerate(queries):
        if index:
            pause(delay)
        answer = _ask(talk, base, query)
        if isinstance(answer, str):
            _record_failure(conn, config, clock())
            return Outcome(answer, ())
        for raw in answer:
            ref = posting(raw)
            if ref is not None:
                postings.setdefault(posting_url(ref) or raw, ref)
            hit = classify(raw, career_hosts)
            if hit is None:
                continue
            key = _hit_key(hit)
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
    # The queue rows and the moved cursor go in one transaction: a miss moves neither,
    # so the same queries are asked again next time.
    boards = [hit for hit in hits if hit.kind == "board"]
    accepted, gone, notes = _confirmed(
        conn, config, boards, confirm, exclusions or {}, start=_cursor(conn)
    )
    stored = _store(conn, config, accepted, today, career_hosts, cursor=after, gone=gone)
    return Outcome(None, tuple(stored), tuple(postings.values()), tuple(notes))


def _folded(text: str) -> str:
    """Case-folded letters and digits only: how company names are compared."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _excluded_names(exclusions: Mapping[str, Any]) -> set[str]:
    raw = exclusions.get("companies")
    return (
        {_folded(name) for name in raw if isinstance(name, str) and _folded(name)}
        if isinstance(raw, list)
        else set()
    )


def _confirmed(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    boards: Sequence[Hit],
    confirm: Confirm | None,
    exclusions: Mapping[str, Any],
    *,
    start: int = 0,
) -> tuple[list[Hit], list[Hit], list[str]]:
    """The new boards to queue and the gone ones to record, with a note for each other.

    Boards already watched or in the queue (pending, approved, rejected or gone) and
    boards excluded by their slug are left out before CONFIRM_PER_PASS counts, so every
    pass reaches new boards. The rest are asked about from `start` (the cursor), so
    boards that keep failing at the front of the same results cannot take the limit
    every pass. Runs before the queue's transaction, so no write lock is held across a
    request.
    """
    watched = _watched_boards(config)
    known = _known_boards(conn)
    excluded = _excluded_names(exclusions)
    accepted: list[Hit] = []
    gone: list[Hit] = []
    notes: list[str] = []
    asked = 0
    candidates: list[tuple[Hit, str, str]] = []
    for hit in boards:
        if hit.ats_type is None or hit.ats_slug is None:
            continue
        key = _board_key(hit.ats_type, hit.ats_slug, hit.ats_host, hit.ats_site)
        if key in watched or key in known:
            continue
        known.add(key)
        if {_folded(hit.ats_slug), _folded(hit.suggested_name)} & excluded:
            notes.append(f"discover: {hit.suggested_name} ({hit.ats_type}): excluded; not queued")
            continue
        candidates.append((hit, hit.ats_type, hit.ats_slug))
    if candidates:
        first = start % len(candidates)
        candidates = candidates[first:] + candidates[:first]
    for hit, ats_type, slug in candidates:
        who = f"discover: {hit.suggested_name} ({ats_type})"
        if confirm is None or asked >= CONFIRM_PER_PASS:
            notes.append(f"{who}: not confirmed this pass; not queued")
            continue
        asked += 1
        board = Board(ats_type, slug, hit.ats_host, hit.ats_site)
        try:
            answer = confirm(board)
        except (FetchError, BoardHTTPError, BoardParseError) as exc:
            kind = f"HTTP {exc.status}" if isinstance(exc, BoardHTTPError) else type(exc).__name__
            notes.append(f"{who}: not confirmed ({kind}); not queued")
            continue
        if answer.exists is False:
            gone.append(hit)
            notes.append(f"{who}: the board does not exist; never queued")
        elif {_folded(name) for name in answer.names} & excluded:
            notes.append(f"{who}: excluded; not queued")
        elif answer.exists is None:
            notes.append(f"{who}: no postings to confirm it; not queued")
        else:
            accepted.append(hit)
    return accepted, gone, notes


def _known_boards(conn: sqlite3.Connection) -> set[tuple[str, ...]]:
    """Every board key already in the queue, whatever its status (gone included)."""
    return {
        _board_key(str(row["ats_type"]), str(row["ats_slug"]), row["ats_host"], row["ats_site"])
        for row in conn.execute(
            "SELECT ats_type, ats_slug, ats_host, ats_site FROM discovery_candidates "
            "WHERE kind = 'board'"
        )
    }


def posting(url: str) -> PostingRef | None:
    """The posting a result URL names on a supported ATS, or None (#41).

    Greenhouse `/<board>/jobs/<id>`, Lever and Ashby `/<board>/<id>`, SmartRecruiters
    `/<company>/<id>-<title>`, BambooHR `/careers/<id>` and Workday
    `[/<locale>]/<site>/job/...`. The posting's board is the one `classify` reduces
    the same URL to.
    """
    hit = classify(url, [])
    if hit is None or hit.kind != "board" or hit.ats_type is None or hit.ats_slug is None:
        return None
    parts = urllib.parse.urlsplit(url.strip())
    segments = _segments(parts.path) or []
    ref: str | None = None
    if hit.ats_type == "greenhouse" and len(segments) == 3 and segments[1] == "jobs":
        ref = segments[2]
    elif hit.ats_type in ("lever", "ashby") and len(segments) == 2:
        ref = segments[1]
    elif hit.ats_type == "smartrecruiters" and len(segments) == 2:
        ref = segments[1].split("-", 1)[0]
    elif hit.ats_type == "bamboohr" and len(segments) == 2:
        ref = segments[1]
    elif hit.ats_type == "workday" and "job" in segments:
        at = segments.index("job")
        ref = "/" + "/".join(urllib.parse.quote(part, safe="") for part in segments[at:])
    if ref is None:
        return None
    board = Board(hit.ats_type, hit.ats_slug, hit.ats_host, hit.ats_site)
    found = PostingRef(board, ref)
    return found if posting_url(found) is not None else None


def _ask(talk: Transport, base: str, query: str) -> list[str] | str:
    """One query's result URLs, or the closed line that ends the pass.

    A transport failure or a reply over the cap is `unreachable`; another status or a
    reply that is not a usable result list is `bad-reply` (#42). Both are a miss. HTTP
    429 or an engine throttled is a stop.
    """
    try:
        status, body = talk(request_url(base, query))
    except (DiscoverError, OSError, http.client.HTTPException):
        return MISS
    if status == 429:
        return THROTTLED
    if len(body) > MAX_BODY:
        return MISS
    if status != 200:
        return BAD_REPLY
    if _throttled(body):
        return THROTTLED
    urls = _result_urls(body)
    return BAD_REPLY if urls is None else urls


def _throttled(body: bytes) -> bool:
    """Whether SearXNG reports an engine that hit a CAPTCHA or too many requests.

    `unresponsive_engines` is a list of [engine, error text] pairs.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    engines = payload.get("unresponsive_engines") if isinstance(payload, dict) else None
    for pair in engines if isinstance(engines, list) else []:
        if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[1], str):
            text = pair[1].casefold()
            if any(word in text for word in _THROTTLE_WORDS):
                return True
    return False


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backoff_until(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT backoff_until FROM discovery_state WHERE id = 1").fetchone()
    return None if row is None or row[0] is None else str(row[0])


def backoff_for(failures: int, interval_minutes: int) -> dt.timedelta:
    """The wait after `failures` failed passes in a row: the interval doubled each time."""
    wait: dt.timedelta = dt.timedelta(minutes=interval_minutes) * (2 ** min(failures, 16))
    return wait if wait < BACKOFF_CAP else BACKOFF_CAP


def _record_failure(
    conn: sqlite3.Connection, config: Mapping[str, Any], moment: dt.datetime
) -> None:
    with db.transaction(conn):
        row = conn.execute("SELECT failures FROM discovery_state WHERE id = 1").fetchone()
        failures = (0 if row is None else int(row[0])) + 1
        until = moment + backoff_for(failures, discovery_interval_minutes(dict(config)))
        conn.execute(
            "UPDATE discovery_state SET failures = ?, backoff_until = ? WHERE id = 1",
            (failures, _stamp(until)),
        )


def claim_pass(conn: sqlite3.Connection, moment: dt.datetime, gap: dt.timedelta) -> bool:
    """Claim a discovery pass, unless one was claimed less than `gap` before `moment`.

    One conditional UPDATE inside BEGIN IMMEDIATE, so two processes can never both
    claim: the second one sees the first one's stamp. Returns whether this one did.
    """
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE discovery_state SET last_pass_at = ? "
            "WHERE id = 1 AND (last_pass_at IS NULL OR last_pass_at <= ?)",
            (_stamp(moment), _stamp(moment - gap)),
        )
    return cur.rowcount == 1


def _cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT cursor FROM discovery_state WHERE id = 1").fetchone()
    return 0 if row is None else int(row[0])


def _configured_url(config: Mapping[str, Any]) -> str | None:
    section = config.get("discovery")
    if not isinstance(section, Mapping):
        return None
    value = section.get("searxng_url")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _career_hosts(config: Mapping[str, Any]) -> list[str]:
    section = config.get("discovery")
    if not isinstance(section, Mapping):
        return []
    raw = section.get("career_hosts")
    if not isinstance(raw, list):
        return []
    return [host.strip() for host in raw if isinstance(host, str) and host.strip()]


def _titles(config: Mapping[str, Any]) -> list[str]:
    """Lanes by priority, best first, every variant; else `search.target_titles`.

    A title already listed (ignoring case) is not listed again.
    """
    search = config.get("search")
    if not isinstance(search, Mapping):
        return []
    lanes = search.get("lanes")
    raw: list[object] = []
    if isinstance(lanes, list) and lanes:
        ranked = sorted((lane for lane in lanes if isinstance(lane, Mapping)), key=_lane_priority)
        for lane in ranked:
            entries = lane.get("titles")
            for entry in entries if isinstance(entries, list) else []:
                raw.extend(entry if isinstance(entry, list) else [entry])
    else:
        targets = search.get("target_titles")
        raw = list(targets) if isinstance(targets, list) else []
    return _unique(raw)


def _lane_priority(lane: Mapping[str, Any]) -> int:
    priority = lane.get("priority")
    return priority if isinstance(priority, int) else 0


def _places(config: Mapping[str, Any]) -> list[str]:
    """On-site and hybrid places, then remote regions, from `search.location_rules`."""
    search = config.get("search")
    rules = search.get("location_rules") if isinstance(search, Mapping) else None
    if not isinstance(rules, Mapping):
        return []
    raw: list[object] = []
    for key in ("onsite_hybrid", "remote"):
        values = rules.get(key)
        raw.extend(values if isinstance(values, list) else [])
    return _unique(raw)


def _unique(values: Sequence[object]) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip().casefold() not in seen:
            seen.add(value.strip().casefold())
            found.append(value.strip())
    return found


def _hosts(config: Mapping[str, Any]) -> list[str]:
    return [*_QUERY_HOSTS, *(host.casefold() for host in _career_hosts(config))]


def _segments(path: str) -> list[str] | None:
    segments = [urllib.parse.unquote(part) for part in path.split("/") if part]
    if any(part in {".", ".."} or "\\" in part or not part.strip() for part in segments):
        return None
    return segments


_Board = tuple[str, str, str | None, str | None]  # ats_type, slug, Workday host, site


def _board(host: str, segments: list[str], query: str) -> _Board | None:
    if host in _GREENHOUSE:
        if segments == ["embed", "job_board"]:
            slug = _query_slug(query)
            return ("greenhouse", slug, None, None) if slug else None
        slug = _greenhouse_slug(segments)
        return ("greenhouse", slug, None, None) if slug else None
    if host == "jobs.lever.co":
        slug = _one_or_two(segments)
        return ("lever", slug, None, None) if slug else None
    if host == "jobs.ashbyhq.com":
        slug = _one_or_two(segments)
        return ("ashby", slug, None, None) if slug else None
    if host in _SMARTRECRUITERS:
        slug = _smartrecruiters_slug(segments)
        return ("smartrecruiters", slug, None, None) if slug else None
    if host.endswith(".bamboohr.com"):
        slug = _bamboohr_slug(host.removesuffix(".bamboohr.com"), segments)
        return ("bamboohr", slug, None, None) if slug else None
    if host.endswith(".myworkdayjobs.com"):
        return _workday(host, segments)
    return None


def _smartrecruiters_slug(segments: list[str]) -> str | None:
    """`/<company>` or `/<company>/<id>-<title>`."""
    if not segments or len(segments) > 2 or _slug(segments[0]) is None:
        return None
    if len(segments) == 2 and _SMARTRECRUITERS_POSTING.fullmatch(segments[1]) is None:
        return None
    return segments[0]


def _bamboohr_slug(sub: str, segments: list[str]) -> str | None:
    """`<company>.bamboohr.com/careers` or `/careers/<id>`."""
    if _LABEL.fullmatch(sub) is None or sub.casefold() in _BAMBOOHR_OWN:
        return None
    if not segments or segments[0] != "careers" or len(segments) > 2:
        return None
    if len(segments) == 2 and not segments[1].isdigit():
        return None
    return sub.casefold()


def _workday(host: str, segments: list[str]) -> _Board | None:
    """`<tenant>.<wdN>.myworkdayjobs.com/[<locale>/]<site>[/job/...]`."""
    match = _WORKDAY_HOST.fullmatch(host)
    if match is None:
        return None
    rest = segments[1:] if segments and _LOCALE.fullmatch(segments[0]) else segments
    if not rest or _slug(rest[0]) is None or rest[0] in ("wday", "job"):
        return None
    if len(rest) > 1 and (rest[1] != "job" or len(rest) < 3):
        return None
    return ("workday", match.group(1).casefold(), match.group(2), rest[0])


def _greenhouse_slug(segments: list[str]) -> str | None:
    if not segments or _slug(segments[0]) is None:
        return None
    if len(segments) == 1:
        return segments[0]
    if segments[1] != "jobs" or len(segments) > 3:
        return None
    if len(segments) == 3 and not segments[2]:
        return None
    return segments[0]


def _one_or_two(segments: list[str]) -> str | None:
    if not segments or len(segments) > 2 or _slug(segments[0]) is None:
        return None
    if len(segments) == 2 and not segments[1]:
        return None
    return segments[0]


def _query_slug(query: str) -> str | None:
    values = urllib.parse.parse_qs(query).get("for", [])
    if len(values) != 1:
        return None
    return _slug(values[0])


def _slug(value: str) -> str | None:
    if _SLUG.fullmatch(value) is None:
        return None
    return value


def _result_urls(body: bytes) -> list[str] | None:
    """The result URLs, or None when the reply is not a SearXNG result list.

    A result without a string url is skipped. A non-empty list where every result is
    skipped is not a usable reply either.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    urls: list[str] = []
    for item in results:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            urls.append(item["url"])
    if results and not urls:
        return None
    return urls


def _hit_key(hit: Hit) -> tuple[str, ...]:
    if hit.kind == "board" and hit.ats_type and hit.ats_slug:
        return ("board", *_board_key(hit.ats_type, hit.ats_slug, hit.ats_host, hit.ats_site))
    return ("career", hit.url)


def _board_key(ats: str, slug: str, host: str | None, site: str | None) -> tuple[str, ...]:
    """Case-folded; Workday's host and site are part of it."""
    if ats == "workday":
        return (ats, slug.casefold(), (host or "").casefold(), (site or "").casefold())
    return (ats, slug.casefold())


def _store(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    hits: Sequence[Hit],
    today: str,
    career_hosts: Sequence[str],
    *,
    cursor: int | None = None,
    gone: Sequence[Hit] = (),
) -> list[Queued]:
    """Insert new pending rows, and `gone` rows for boards that do not exist."""
    watched_boards = _watched_boards(config)
    watched_careers = _watched_careers(config, career_hosts)
    known_boards = _known_boards(conn)
    known_careers = {
        str(row["url"])
        for row in conn.execute("SELECT url FROM discovery_candidates WHERE kind = 'career'")
    }
    stored: list[Queued] = []
    with db.transaction(conn):
        if cursor is not None:  # a pass that succeeded: move on and clear any backoff
            conn.execute(
                "UPDATE discovery_state SET cursor = ?, failures = 0, backoff_until = NULL "
                "WHERE id = 1",
                (cursor,),
            )
        for hit in gone:
            conn.execute(
                "INSERT INTO discovery_candidates (kind, url, ats_type, ats_slug, ats_host, "
                "ats_site, suggested_name, status, first_seen) "
                "VALUES ('board', ?, ?, ?, ?, ?, ?, 'gone', ?)",
                (
                    hit.url,
                    hit.ats_type,
                    hit.ats_slug,
                    hit.ats_host,
                    hit.ats_site,
                    hit.suggested_name,
                    today,
                ),
            )
        for hit in hits:
            if hit.kind == "board":
                if hit.ats_type is None or hit.ats_slug is None:
                    continue
                key = _board_key(hit.ats_type, hit.ats_slug, hit.ats_host, hit.ats_site)
                if key in watched_boards or key in known_boards:
                    continue
                known_boards.add(key)
            else:
                if hit.url in watched_careers or hit.url in known_careers:
                    continue
                known_careers.add(hit.url)
            cur = conn.execute(
                "INSERT INTO discovery_candidates (kind, url, ats_type, ats_slug, ats_host, "
                "ats_site, suggested_name, status, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    hit.kind,
                    hit.url,
                    hit.ats_type,
                    hit.ats_slug,
                    hit.ats_host,
                    hit.ats_site,
                    hit.suggested_name,
                    today,
                ),
            )
            stored.append(
                Queued(
                    id=int(cur.lastrowid or 0),
                    kind=hit.kind,
                    suggested_name=hit.suggested_name,
                    ats_type=hit.ats_type,
                    ref=hit.ats_slug if hit.kind == "board" and hit.ats_slug else hit.url,
                    url=hit.url,
                )
            )
    return stored


def _watched_boards(config: Mapping[str, Any]) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for item in _watch_items(config):
        ats = item.get("ats_type")
        slug = item.get("ats_slug")
        host = item.get("ats_host")
        site = item.get("ats_site")
        if isinstance(ats, str) and isinstance(slug, str) and slug.strip():
            found.add(
                _board_key(
                    ats,
                    slug.strip(),
                    host if isinstance(host, str) else None,
                    site if isinstance(site, str) else None,
                )
            )
    return found


def _watched_careers(config: Mapping[str, Any], career_hosts: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for item in _watch_items(config):
        raw = item.get("careers_url")
        if not isinstance(raw, str):
            continue
        hit = classify(raw.strip(), career_hosts)
        if hit is not None and hit.kind == "career":
            found.add(hit.url)
    return found


def _watch_items(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = config.get("watchlist")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _pending_row(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, kind, url, ats_type, ats_slug, ats_host, ats_site, suggested_name, status "
        "FROM discovery_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None or candidate_id < 1:
        raise DiscoverError(f"discovery candidate {candidate_id} was not found")
    status = str(row["status"])
    if status != "pending":
        raise DiscoverError(f"discovery candidate {candidate_id} is {status}")
    return row


def _mark(conn: sqlite3.Connection, candidate_id: int, status: str) -> None:
    cur = conn.execute(
        "UPDATE discovery_candidates SET status = ? WHERE id = ? AND status = 'pending'",
        (status, candidate_id),
    )
    if cur.rowcount != 1:
        raise DiscoverError(f"discovery candidate {candidate_id} is not pending")


def _approve_board(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    ats = row["ats_type"]
    slug = row["ats_slug"]
    if not isinstance(ats, str) or not isinstance(slug, str):
        raise DiscoverError(f"discovery candidate {int(row['id'])} was not found")
    host = row["ats_host"]
    site = row["ats_site"]
    name = str(row["suggested_name"])
    existing = conn.execute(
        "SELECT id, ats_type, ats_slug, ats_host, ats_site FROM companies WHERE name = ?",
        (name,),
    ).fetchone()
    if existing is None:
        search_store.upsert_company(
            conn,
            name,
            ats_type=ats,
            ats_slug=slug,
            ats_host=host,
            ats_site=site,
            watchlist=1,
            source="discovery",
        )
        return
    stored = (
        existing["ats_type"],
        existing["ats_slug"],
        existing["ats_host"],
        existing["ats_site"],
    )
    if _ats_disagrees(stored, (ats, slug, host, site)):
        raise DiscoverError(
            f"discovery candidate {int(row['id'])} disagrees with the stored company"
        )
    conn.execute(
        "UPDATE companies SET ats_type = coalesce(ats_type, ?), ats_slug = coalesce(ats_slug, ?), "
        "ats_host = coalesce(ats_host, ?), ats_site = coalesce(ats_site, ?), "
        "watchlist = 1 WHERE id = ?",
        (ats, slug, host, site, existing["id"]),
    )


def _approve_career(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    name = str(row["suggested_name"])
    url = str(row["url"])
    existing = conn.execute(
        "SELECT id, careers_url FROM companies WHERE name = ?",
        (name,),
    ).fetchone()
    if existing is None:
        search_store.upsert_company(conn, name, careers_url=url, watchlist=0, source="discovery")
        return
    stored = existing["careers_url"]
    if isinstance(stored, str) and stored != url:
        raise DiscoverError(
            f"discovery candidate {int(row['id'])} disagrees with the stored company"
        )
    if stored is None:
        conn.execute(
            "UPDATE companies SET careers_url = ? WHERE id = ?",
            (url, existing["id"]),
        )


def _ats_disagrees(stored: tuple[object, ...], wanted: tuple[object, ...]) -> bool:
    """A stored value that is set and differs. A null stored value is filled in."""
    return any(
        isinstance(have, str) and have != want for have, want in zip(stored, wanted, strict=True)
    )


def _queued(row: sqlite3.Row) -> Queued:
    kind = str(row["kind"])
    slug = row["ats_slug"]
    ref = str(slug) if kind == "board" and isinstance(slug, str) else str(row["url"])
    ats = row["ats_type"]
    return Queued(
        id=int(row["id"]),
        kind=kind,
        suggested_name=str(row["suggested_name"]),
        ats_type=str(ats) if isinstance(ats, str) else None,
        ref=ref,
        url=str(row["url"]),
    )
