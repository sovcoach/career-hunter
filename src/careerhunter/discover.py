"""Queue company candidates from a local SearXNG.

The client is not `fetch.py`. It allows only plain http to a loopback host, follows
no redirects, and reads result URLs only. Titles, snippets, and the response body are
not stored. A down server yields one closed miss line and writes nothing.

Approve and reject update this queue, and a board approval can fill a company row.
They do not rewrite config files or open an EI record. The bot may call them.
Sending the message is separate: `notified_at` is set only after Telegram accepts it.
"""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from careerhunter import db, loopback, search_store
from careerhunter.config import discovery_queries_per_run
from careerhunter.deadline import Deadline

MAX_BODY = 1_048_576
MISS = "discovery miss: unreachable"
OFF = "discovery off"
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
    """`notice` is a closed stderr line. `rows` are the rows inserted on this run."""

    notice: str | None
    rows: tuple[Queued, ...]


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
    """Pending rows never sent, or last sent at or before `notified_before`."""
    rows = conn.execute(
        "SELECT id, kind, suggested_name, ats_type, ats_slug, url "
        "FROM discovery_candidates "
        "WHERE status = 'pending' AND (notified_at IS NULL OR notified_at <= ?) "
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


def run(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    *,
    today: str,
    transport: Transport | None = None,
) -> Outcome:
    """Search, then insert new pending rows. A miss writes nothing."""
    base = _configured_url(config)
    if base is None:
        return Outcome(OFF, ())
    every = build_queries(config)
    if not every:
        return Outcome(None, ())
    queries, after = take(every, _cursor(conn), discovery_queries_per_run(config))
    talk = transport or default_transport
    career_hosts = _career_hosts(config)
    hits: list[Hit] = []
    seen: set[tuple[str, ...]] = set()
    for query in queries:
        try:
            target = request_url(base, query)
            status, body = talk(target)
        except (DiscoverError, OSError, http.client.HTTPException):
            return Outcome(MISS, ())
        if status != 200 or len(body) > MAX_BODY:
            return Outcome(MISS, ())
        urls = _result_urls(body)
        if urls is None:
            return Outcome(MISS, ())
        for raw in urls:
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
    return Outcome(None, tuple(_store(conn, config, hits, today, career_hosts, cursor=after)))


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
) -> list[Queued]:
    watched_boards = _watched_boards(config)
    watched_careers = _watched_careers(config, career_hosts)
    known_boards = {
        _board_key(str(row["ats_type"]), str(row["ats_slug"]), row["ats_host"], row["ats_site"])
        for row in conn.execute(
            "SELECT ats_type, ats_slug, ats_host, ats_site FROM discovery_candidates "
            "WHERE kind = 'board'"
        )
    }
    known_careers = {
        str(row["url"])
        for row in conn.execute("SELECT url FROM discovery_candidates WHERE kind = 'career'")
    }
    stored: list[Queued] = []
    with db.transaction(conn):
        if cursor is not None:
            conn.execute("UPDATE discovery_state SET cursor = ? WHERE id = 1", (cursor,))
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
