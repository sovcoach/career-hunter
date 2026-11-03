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

from careerhunter import db, search_store

MAX_QUERIES = 12
MAX_BODY = 1_048_576
MISS = "discovery miss: unreachable"
OFF = "discovery off"
_TIMEOUT = 20.0
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
_GREENHOUSE = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
_QUERY_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
)
_BOARD_URL = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
}

Transport = Callable[[str], tuple[int, bytes]]


class DiscoverError(Exception):
    """A closed message. It carries no query and no result URL."""


@dataclass(frozen=True)
class Hit:
    """One reduced company. `url` is the board root or the career-page URL."""

    kind: str
    url: str
    ats_type: str | None
    ats_slug: str | None
    suggested_name: str


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


def default_transport(url: str) -> tuple[int, bytes]:
    """GET once. A redirect status is returned as-is and is not followed."""
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
    conn = http.client.HTTPConnection(host, port, timeout=_TIMEOUT)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        response = conn.getresponse()
        return response.status, response.read(MAX_BODY + 1)
    except OSError as exc:
        raise DiscoverError(MISS) from exc
    finally:
        conn.close()


def build_queries(config: Mapping[str, Any]) -> list[str]:
    """At most 12 `site:` queries. Titles, then locations, then hosts."""
    titles = _titles(config)
    locations = _locations(config) or [""]
    found: list[str] = []
    for title in titles:
        for location in locations:
            for host in _hosts(config):
                text = f"site:{host} {title}"
                if location:
                    text = f"{text} {location}"
                found.append(text)
                if len(found) == MAX_QUERIES:
                    return found
    return found


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
        ats, slug = board
        return Hit("board", _BOARD_URL[ats].format(slug=slug), ats, slug, slug)
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
    queries = build_queries(config)
    if not queries:
        return Outcome(None, ())
    talk = transport or default_transport
    career_hosts = _career_hosts(config)
    hits: list[Hit] = []
    seen: set[tuple[str, ...]] = set()
    for query in queries:
        try:
            target = request_url(base, query)
            status, body = talk(target)
        except (DiscoverError, OSError):
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
    return Outcome(None, tuple(_store(conn, config, hits, today, career_hosts)))


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
    search = config.get("search")
    if not isinstance(search, Mapping):
        return []
    lanes = search.get("lanes")
    if isinstance(lanes, list) and lanes:
        titles: list[str] = []
        for lane in lanes:
            if not isinstance(lane, Mapping):
                continue
            raw = lane.get("titles")
            if not isinstance(raw, list):
                continue
            for item in raw:
                title = _one_title(item)
                if title:
                    titles.append(title)
        return titles
    raw_titles = search.get("target_titles")
    if not isinstance(raw_titles, list):
        return []
    return [item.strip() for item in raw_titles if isinstance(item, str) and item.strip()]


def _one_title(item: object) -> str | None:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, list):
        for variant in item:
            if isinstance(variant, str) and variant.strip():
                return variant.strip()
    return None


def _locations(config: Mapping[str, Any]) -> list[str]:
    search = config.get("search")
    if not isinstance(search, Mapping):
        return []
    raw = search.get("locations")
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _hosts(config: Mapping[str, Any]) -> list[str]:
    return [*_QUERY_HOSTS, *(host.casefold() for host in _career_hosts(config))]


def _segments(path: str) -> list[str] | None:
    segments = [urllib.parse.unquote(part) for part in path.split("/") if part]
    if any(part in {".", ".."} or "\\" in part or not part.strip() for part in segments):
        return None
    return segments


def _board(host: str, segments: list[str], query: str) -> tuple[str, str] | None:
    if host in _GREENHOUSE:
        if segments == ["embed", "job_board"]:
            slug = _query_slug(query)
            return ("greenhouse", slug) if slug else None
        slug = _greenhouse_slug(segments)
        return ("greenhouse", slug) if slug else None
    if host == "jobs.lever.co":
        slug = _one_or_two(segments)
        return ("lever", slug) if slug else None
    if host == "jobs.ashbyhq.com":
        slug = _one_or_two(segments)
        return ("ashby", slug) if slug else None
    return None


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
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    results = payload.get("results", [])
    if not isinstance(results, list):
        return None
    urls: list[str] = []
    for item in results:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            urls.append(item["url"])
    return urls


def _hit_key(hit: Hit) -> tuple[str, ...]:
    if hit.kind == "board" and hit.ats_type and hit.ats_slug:
        return ("board", hit.ats_type, hit.ats_slug.casefold())
    return ("career", hit.url)


def _store(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    hits: Sequence[Hit],
    today: str,
    career_hosts: Sequence[str],
) -> list[Queued]:
    watched_boards = _watched_boards(config)
    watched_careers = _watched_careers(config, career_hosts)
    known_boards = {
        (str(row["ats_type"]), str(row["ats_slug"]).casefold())
        for row in conn.execute(
            "SELECT ats_type, ats_slug FROM discovery_candidates WHERE kind = 'board'"
        )
    }
    known_careers = {
        str(row["url"])
        for row in conn.execute("SELECT url FROM discovery_candidates WHERE kind = 'career'")
    }
    stored: list[Queued] = []
    with db.transaction(conn):
        for hit in hits:
            if hit.kind == "board":
                if hit.ats_type is None or hit.ats_slug is None:
                    continue
                key = (hit.ats_type, hit.ats_slug.casefold())
                if key in watched_boards or key in known_boards:
                    continue
                known_boards.add(key)
            else:
                if hit.url in watched_careers or hit.url in known_careers:
                    continue
                known_careers.add(hit.url)
            cur = conn.execute(
                "INSERT INTO discovery_candidates "
                "(kind, url, ats_type, ats_slug, suggested_name, status, first_seen) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (hit.kind, hit.url, hit.ats_type, hit.ats_slug, hit.suggested_name, today),
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


def _watched_boards(config: Mapping[str, Any]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for item in _watch_items(config):
        ats = item.get("ats_type")
        slug = item.get("ats_slug")
        if isinstance(ats, str) and isinstance(slug, str) and slug.strip():
            found.add((ats, slug.strip().casefold()))
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
        "SELECT id, kind, url, ats_type, ats_slug, suggested_name, status "
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
    name = str(row["suggested_name"])
    existing = conn.execute(
        "SELECT id, ats_type, ats_slug FROM companies WHERE name = ?",
        (name,),
    ).fetchone()
    if existing is None:
        search_store.upsert_company(
            conn, name, ats_type=ats, ats_slug=slug, watchlist=1, source="discovery"
        )
        return
    if _ats_disagrees(existing["ats_type"], existing["ats_slug"], ats, slug):
        raise DiscoverError(
            f"discovery candidate {int(row['id'])} disagrees with the stored company"
        )
    conn.execute(
        "UPDATE companies SET ats_type = coalesce(ats_type, ?), ats_slug = coalesce(ats_slug, ?), "
        "watchlist = 1 WHERE id = ?",
        (ats, slug, existing["id"]),
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


def _ats_disagrees(stored_type: object, stored_slug: object, ats: str, slug: str) -> bool:
    type_text = stored_type if isinstance(stored_type, str) else None
    slug_text = stored_slug if isinstance(stored_slug, str) else None
    return (type_text is not None and type_text != ats) or (
        slug_text is not None and slug_text != slug
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
