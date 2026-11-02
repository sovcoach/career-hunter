"""Fetch Greenhouse, Lever and Ashby boards, filter the new postings, then score them.

One `careerhunter watch` run reads the watchlist and fetches those three boards, one
company after another, through `fetch.py`. It writes companies, postings, skip reasons
and local scores in `search.db`. It does not call Telegram, write applications, EI
rows or snapshots. The bot sends an alert for each scored posting when it polls.
`run` returns the log lines; the CLI prints them to stderr.

A posting that is already stored is not inserted again. Before the fetch, watcher rows
that still have no skip are filtered again, and rows that are still unscored are
scored. A network error, a parse error, HTTP 429, or a model that does not answer is
a log line, not a skip.

The list URL is built only from `ats_slug`. `careers_url` is ignored. A slug has to be
one path segment (`A-Za-z0-9_-`). Workday and every other `ats_type` are not fetched.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from careerhunter import db, filters, search_store
from careerhunter.fetch import Fetched, FetchError
from careerhunter.filters import PostingText
from careerhunter.models import Dating, Posting
from careerhunter.score import Score, ScoreError

Fetcher = Callable[[str], Fetched]
Scorer = Callable[[PostingText], Score | None]

_BOARDS = ("greenhouse", "lever", "ashby")
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
# A board's own company label that carries no information. The watchlist name is used.
_PLACEHOLDERS = frozenset(
    {
        "confidential",
        "confidential company",
        "unknown",
        "n/a",
        "na",
        "none",
        "tbd",
        "private",
        "stealth",
        "unnamed",
        "not specified",
        "-",
    }
)
# The public postings API says "on-site"; the hire API says "onsite".
_LEVER_WORK = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
_ASHBY_WORK = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


class BoardParseError(Exception):
    """The body is not that board's JSON. Nothing from the board is stored."""


@dataclass(frozen=True)
class _Job:
    title: str
    location: str
    work_model: str | None
    url: str | None
    requisition_id: str | None
    company_field: str | None
    description: str | None = None


@dataclass(frozen=True)
class _Skip:
    reason: str


@dataclass(frozen=True)
class _Aligned:
    ok: bool
    stored_type: str | None = None
    stored_slug: str | None = None


def board_url(ats_type: str, slug: str) -> str | None:
    """The list URL for one board, built only from a single safe path segment."""
    if ats_type not in _BOARDS or _SLUG.fullmatch(slug) is None:
        return None
    if ats_type == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    if ats_type == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"


def run(
    conn: sqlite3.Connection,
    watchlist: Sequence[Any],
    *,
    today: str,
    fetcher: Fetcher,
    config: Mapping[str, Any] | None = None,
    exclusions: Mapping[str, Any] | None = None,
    scorer: Scorer | None = None,
) -> list[str]:
    """Fetch each watched board once. `today` is the configured date, `YYYY-MM-DD`."""
    dt.date.fromisoformat(today)
    rules = config or {}
    skipped = exclusions or {}
    lines: list[str] = []
    _recheck(conn, watchlist, rules, skipped, today, lines)
    _score_waiting(conn, scorer, today, lines)
    blocked: set[str] = set()
    for item in watchlist:
        entry = _entry(item)
        if entry is None:
            lines.append("watch: a watchlist entry has no name; skipped")
            continue
        name, ats, slug, role_filters = entry
        _watch_one(
            conn,
            lines,
            blocked,
            name,
            ats,
            slug,
            role_filters,
            rules,
            skipped,
            today,
            fetcher,
            scorer,
        )
    return lines


def _entry(item: object) -> tuple[str, str | None, str | None, Mapping[str, Any] | None] | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    ats = item.get("ats_type")
    slug = item.get("ats_slug")
    role_filters = item.get("role_filters")
    return (
        name.strip(),
        ats if isinstance(ats, str) else None,
        slug.strip() if isinstance(slug, str) else None,
        role_filters if isinstance(role_filters, dict) else None,
    )


def _watch_one(
    conn: sqlite3.Connection,
    lines: list[str],
    blocked: set[str],
    name: str,
    ats: str | None,
    slug: str | None,
    role_filters: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    exclusions: Mapping[str, Any],
    today: str,
    fetcher: Fetcher,
    scorer: Scorer | None,
) -> None:
    if ats is None:
        lines.append(_line(name, None, "no ats_type; skipped"))
        return
    if ats == "workday":
        lines.append(_line(name, ats, "not fetched yet; skipped"))
        return
    if ats not in _BOARDS:
        lines.append(_line(name, ats, "not a board this command fetches; skipped"))
        return
    if not slug:
        lines.append(_line(name, ats, "no ats_slug; skipped"))
        return
    url = board_url(ats, slug)
    if url is None:
        lines.append(_line(name, ats, "ats_slug is not a single path segment; skipped"))
        return
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in blocked:
        lines.append(
            _line(name, ats, f"{host} already returned HTTP 429 this run; left for the next run")
        )
        return
    jobs = _fetch_jobs(lines, blocked, name, ats, slug, url, host, fetcher)
    if jobs is None:
        return
    with db.transaction(conn):
        aligned = _align(conn, name, ats, slug)
    if not aligned.ok:
        lines.append(
            _line(
                name,
                ats,
                "stored ats identity disagrees "
                f"(ats_type={_shown(aligned.stored_type)}, "
                f"ats_slug={_shown(aligned.stored_slug)}); skipped",
            )
        )
        return
    _record_jobs(conn, lines, name, ats, jobs, role_filters, config, exclusions, today, scorer)


def _fetch_jobs(
    lines: list[str],
    blocked: set[str],
    name: str,
    ats: str,
    slug: str,
    url: str,
    host: str,
    fetcher: Fetcher,
) -> list[_Job | _Skip] | None:
    """Parse one board. None means nothing from this board is stored."""
    try:
        fetched = fetcher(url)
    except FetchError:
        lines.append(_line(name, ats, "FetchError; nothing recorded"))
        return None
    if fetched.status == 429:
        # Do not read the body, sleep, or retry. The next watch tries again.
        blocked.add(host)
        lines.append(
            _line(
                name,
                ats,
                f"HTTP 429, retry-after {_retry(fetched.retry_after)}; left for the next run",
            )
        )
        return None
    if fetched.status != 200:
        lines.append(_line(name, ats, f"HTTP {fetched.status}; nothing recorded"))
        return None
    try:
        return _parse(ats, slug, fetched.body)
    except BoardParseError as exc:
        lines.append(_line(name, ats, f"HTTP 200, {exc}; nothing recorded"))
        return None


def _record_jobs(
    conn: sqlite3.Connection,
    lines: list[str],
    name: str,
    ats: str,
    jobs: list[_Job | _Skip],
    role_filters: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    exclusions: Mapping[str, Any],
    today: str,
    scorer: Scorer | None,
) -> None:
    new = 0
    existing = 0
    mismatches: set[str] = set()
    dating = Dating("exact", day=today)
    for item in jobs:
        if isinstance(item, _Skip):
            lines.append(_line(name, ats, f"{item.reason}; skipped"))
            continue
        if item.url is None and item.requisition_id is None:
            # No month-fallback insert: a board job needs a url or a requisition id.
            lines.append(
                _line(name, ats, f"{item.title!r} has neither a url nor a requisition id; skipped")
            )
            continue
        company, differed = _company_name(item.company_field, name)
        if differed is not None and differed.casefold() not in mismatches:
            mismatches.add(differed.casefold())
            lines.append(
                _line(
                    name,
                    ats,
                    f"posting company {differed!r} differs from watchlist name {name!r}",
                )
            )
        verdict = filters.decide(
            PostingText(
                company=company,
                title=item.title,
                location=item.location,
                work_model=item.work_model,
                description=item.description,
                requisition_id=item.requisition_id,
            ),
            config=config,
            exclusions=exclusions,
            role_filters=role_filters,
            applied=False,
        )
        posting = Posting(
            company=company,
            title=item.title,
            first_seen=today,
            location=item.location,
            work_model=item.work_model,
            schedule=None if verdict.reason else verdict.schedule,
            url=item.url,
            requisition_id=item.requisition_id,
            source="watcher",
        )
        try:
            with db.transaction(conn):
                found = search_store.find_or_create_posting(conn, posting, dating)
                posting_id, inserted = found[1], found[3]
                if (
                    inserted
                    and verdict.reason
                    and not search_store.posting_has_skip(conn, posting_id)
                ):
                    search_store.insert_skip(conn, posting_id, today, verdict.reason)
        except search_store.IdentityConflict as exc:
            lines.append(
                _line(name, ats, f"identity conflict on {item.title!r}; continuing: {exc}")
            )
            continue
        if not inserted:
            existing += 1
            continue
        new += 1
        if verdict.reason:
            lines.append(_line(name, ats, f"{item.title!r} skipped ({verdict.reason})"))
            continue
        seen = PostingText(
            company=company,
            title=item.title,
            location=item.location,
            work_model=item.work_model,
            description=item.description,
            requisition_id=item.requisition_id,
        )
        _score_one(conn, posting_id, seen, scorer, today, lines, name, ats)
    lines.append(_line(name, ats, f"{new} new, {existing} already recorded"))


def _recheck(
    conn: sqlite3.Connection,
    watchlist: Sequence[Any],
    config: Mapping[str, Any],
    exclusions: Mapping[str, Any],
    today: str,
    lines: list[str],
) -> None:
    """Filter watcher postings that have no skip and no score yet."""
    roles = {
        entry[0].casefold(): entry[3] for item in watchlist if (entry := _entry(item)) is not None
    }
    rows = conn.execute(
        "SELECT p.id, c.name AS company, p.title, p.location, p.work_model, "
        "p.requisition_id, p.schedule FROM postings p "
        "JOIN companies c ON c.id = p.company_id "
        "WHERE p.source = 'watcher' AND p.llm_output IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id)"
    ).fetchall()
    for row in rows:
        verdict = filters.decide(
            PostingText(
                company=row["company"],
                title=row["title"],
                location=row["location"],
                work_model=row["work_model"],
                requisition_id=row["requisition_id"],
            ),
            config=config,
            exclusions=exclusions,
            role_filters=roles.get(str(row["company"]).casefold()),
            applied=search_store.posting_has_application(conn, int(row["id"])),
        )
        with db.transaction(conn):
            if verdict.reason:
                if not search_store.posting_has_skip(conn, int(row["id"])):
                    search_store.insert_skip(conn, int(row["id"]), today, verdict.reason)
            elif verdict.schedule != row["schedule"]:
                search_store.set_posting_schedule(conn, int(row["id"]), verdict.schedule)
        if verdict.reason:
            lines.append(
                _line(str(row["company"]), None, f"{row['title']!r} skipped ({verdict.reason})")
            )


def _score_waiting(
    conn: sqlite3.Connection,
    scorer: Scorer | None,
    today: str,
    lines: list[str],
) -> None:
    """Score watcher postings that passed the filter and have no model text yet."""
    if scorer is None:
        return
    rows = conn.execute(
        "SELECT p.id, c.name AS company, p.title, p.location, p.work_model, p.requisition_id "
        "FROM postings p JOIN companies c ON c.id = p.company_id "
        "WHERE p.source = 'watcher' AND p.llm_output IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id)"
    ).fetchall()
    for row in rows:
        _score_one(
            conn,
            int(row["id"]),
            PostingText(
                company=row["company"],
                title=row["title"],
                location=row["location"],
                work_model=row["work_model"],
                requisition_id=row["requisition_id"],
            ),
            scorer,
            today,
            lines,
            str(row["company"]),
            None,
        )


def _score_one(
    conn: sqlite3.Connection,
    posting_id: int,
    posting: PostingText,
    scorer: Scorer | None,
    today: str,
    lines: list[str],
    name: str,
    ats: str | None,
) -> None:
    if scorer is None:
        return
    try:
        result = scorer(posting)
    except (ScoreError, OSError):
        result = None
    if result is None:
        lines.append(_line(name, ats, f"{posting.title!r} left unscored"))
        return
    metadata = None if result.tier is None else json.dumps({"tier": result.tier})
    with db.transaction(conn):
        search_store.set_posting_score(
            conn,
            posting_id,
            gaps=result.gaps,
            llm_output=result.llm_output,
            scout_metadata=metadata,
        )
        if result.excluded and not search_store.posting_has_skip(conn, posting_id):
            search_store.insert_skip(conn, posting_id, today, "gap_tier")
    if result.excluded:
        lines.append(_line(name, ats, f"{posting.title!r} skipped (gap_tier)"))


def _align(conn: sqlite3.Connection, name: str, ats_type: str, ats_slug: str) -> _Aligned:
    """Fill a watchlist company, or refuse when its stored ATS identity disagrees.

    A disagreement writes nothing. Source is never changed. The caller runs this only
    after a board parsed, so a failed fetch leaves the company untouched.
    """
    row = conn.execute(
        "SELECT id, ats_type, ats_slug FROM companies WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        search_store.upsert_company(
            conn, name, ats_type=ats_type, ats_slug=ats_slug, watchlist=1, source="watcher"
        )
        return _Aligned(True)
    stored_type = row["ats_type"]
    stored_slug = row["ats_slug"]
    type_text = stored_type if isinstance(stored_type, str) else None
    slug_text = stored_slug if isinstance(stored_slug, str) else None
    if (type_text is not None and type_text != ats_type) or (
        slug_text is not None and slug_text != ats_slug
    ):
        return _Aligned(False, type_text, slug_text)
    conn.execute(
        "UPDATE companies SET ats_type = coalesce(ats_type, ?), ats_slug = coalesce(ats_slug, ?), "
        "watchlist = 1 WHERE id = ?",
        (ats_type, ats_slug, row["id"]),
    )
    return _Aligned(True)


def _parse(ats: str, slug: str, body: bytes) -> list[_Job | _Skip]:
    data = _loads(body)
    if ats == "lever":
        if not isinstance(data, list):
            raise BoardParseError("unexpected JSON shape")
        return [_lever_job(item, slug) for item in data]
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise BoardParseError("unexpected JSON shape")
    if ats == "greenhouse":
        return [_greenhouse_job(item, slug) for item in jobs]
    return [_ashby_job(item, slug) for item in jobs]


def _loads(body: bytes) -> object:
    try:
        loaded: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BoardParseError("invalid JSON") from exc
    return loaded


def _greenhouse_job(item: object, slug: str) -> _Job | _Skip:
    job = _mapping(item)
    if job is None:
        return _Skip("a job was not an object")
    title = _text(job.get("title"))
    if title is None:
        return _Skip("a job has no title")
    # The list payload has no documented company field. Honor company_name when present.
    return _Job(
        title=title,
        location=_nested_text(job.get("location"), "name"),
        work_model=None,
        url=_http_url(job.get("absolute_url"))
        or _built(f"https://boards.greenhouse.io/{slug}/jobs/", job.get("id")),
        requisition_id=_text(job.get("requisition_id")),
        company_field=_text(job.get("company_name")),
    )


def _lever_job(item: object, slug: str) -> _Job | _Skip:
    job = _mapping(item)
    if job is None:
        return _Skip("a job was not an object")
    title = _text(job.get("text"))
    if title is None:
        return _Skip("a job has no title")
    return _Job(
        title=title,
        location=_nested_text(job.get("categories"), "location"),
        work_model=_mapped(job.get("workplaceType"), _LEVER_WORK),
        url=_http_url(job.get("hostedUrl"))
        or _built(f"https://jobs.lever.co/{slug}/", job.get("id")),
        requisition_id=_id_text(job.get("id")),
        company_field=None,
        description=_text(job.get("descriptionPlain")),
    )


def _ashby_job(item: object, slug: str) -> _Job | _Skip:
    job = _mapping(item)
    if job is None:
        return _Skip("a job was not an object")
    title = _text(job.get("title"))
    if title is None:
        return _Skip("a job has no title")
    return _Job(
        title=title,
        location=_text(job.get("location")) or "",
        work_model=_ashby_work(job),
        url=_http_url(job.get("jobUrl"))
        or _built(f"https://jobs.ashbyhq.com/{slug}/", job.get("id")),
        requisition_id=_id_text(job.get("id")),
        company_field=None,
        description=_text(job.get("descriptionPlain")),
    )


def _ashby_work(job: dict[str, Any]) -> str | None:
    # Ashby sets isRemote for hybrid as well as remote, so workplaceType wins when
    # the board sends it. isRemote is only the fallback when that field is absent.
    if "workplaceType" in job:
        return _mapped(job.get("workplaceType"), _ASHBY_WORK)
    if job.get("isRemote") is True:
        return "remote"
    return None


def _company_name(field: str | None, watchlist_name: str) -> tuple[str, str | None]:
    """The name to store, and the posting name when it differs from the watchlist.

    Equal after case-folding keeps the watchlist spelling. `companies.name` is UNIQUE
    NOCASE, so that is not a second company and not a mismatch.
    """
    if field is None or field.casefold() in _PLACEHOLDERS:
        return watchlist_name, None
    if field.casefold() == watchlist_name.casefold():
        return watchlist_name, None
    return field, field


def _built(prefix: str, ident: object) -> str | None:
    text = _path_id(ident)
    if text is None:
        return None
    return prefix + text


def _path_id(value: object) -> str | None:
    text = _id_text(value)
    if text is not None and _SLUG.fullmatch(text):
        return text
    return None


def _id_text(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    return _text(value)


def _http_url(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return text


def _nested_text(value: object, key: str) -> str:
    if isinstance(value, dict):
        return _text(value.get(key)) or ""
    return ""


def _mapped(value: object, table: dict[str, str]) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return table.get(text.casefold())


def _mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _line(name: str, ats: str | None, message: str) -> str:
    who = f"{name} ({ats})" if ats else name
    return f"watch: {who}: {message}"


def _shown(value: str | None) -> str:
    return value if value is not None else "null"


def _retry(value: str | None) -> str:
    if value is None:
        return "none"
    cleaned = " ".join(value.split())
    if not cleaned:
        return "none"
    return cleaned[:80]
