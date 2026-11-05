"""ATS boards: the requests for one board, and its jobs parsed from the replies.

The watcher reads a board through `read`, which asks a `Client` for each request. A
Greenhouse, Lever or Ashby board is one GET of its list URL, built only from a slug
that is one path segment (`A-Za-z0-9_-`).

The client holds one board's byte budget: every reply for the board counts against
BOARD_MAX_BYTES together, and a board over it raises `FetchTooLarge`, so nothing from
it is stored and nothing is cut to fit. Each single request is capped by the fetcher
too. A reply that is not HTTP 200 raises `BoardHTTPError` with its status and the URL
it came from; a body that is not that board's JSON raises `BoardParseError`. A fetch
error from the fetcher passes through unchanged, so a transport failure is never
mistaken for a bad reply.

Greenhouse is fetched with `content=true`, so every gate sees its descriptions; the
HTML is turned into text with the standard library (`htmltext.py`). A Lever posting's
text adds its lists, closing text and salary description; Ashby is fetched with
`includeCompensation=true` and adds each job's salary summary. A description is held
in memory for the filter and the scoring prompt, and never stored.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from careerhunter import htmltext
from careerhunter.fetch import Fetched, FetchTooLarge

Fetcher = Callable[[str], Fetched]

# The body cap for one board, all of its requests together. Greenhouse boards carry
# every description (content=true), so this is larger than a job ad's cap. The CLI
# also gives it to the fetcher as the cap for each single request.
BOARD_MAX_BYTES = 20 * 1024 * 1024

BOARDS = ("greenhouse", "lever", "ashby")
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
# The public postings API says "on-site"; the hire API says "onsite".
_LEVER_WORK = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
_ASHBY_WORK = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


class BoardParseError(Exception):
    """The body is not that board's JSON. Nothing from the board is stored."""


class BoardHTTPError(Exception):
    """A reply other than HTTP 200. Its body is not read further."""

    def __init__(self, url: str, status: int, retry_after: str | None) -> None:
        super().__init__(f"HTTP {status}")
        self.url = url
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Job:
    title: str
    location: str
    work_model: str | None
    url: str | None
    requisition_id: str | None
    company_field: str | None
    description: str | None = None


@dataclass(frozen=True)
class Skip:
    reason: str


class Client:
    """One board's requests, under one byte budget for all of them."""

    def __init__(self, fetcher: Fetcher, *, max_bytes: int = BOARD_MAX_BYTES) -> None:
        self._fetcher = fetcher
        self._max_bytes = max_bytes
        self.used = 0

    def get_json(self, url: str) -> object:
        return _loads(self._checked(url, self._fetcher(url)))

    def _checked(self, url: str, fetched: Fetched) -> bytes:
        if fetched.status != 200:
            raise BoardHTTPError(url, fetched.status, fetched.retry_after)
        self.used += len(fetched.body)
        if self.used > self._max_bytes:
            raise FetchTooLarge(f"the board is larger than {self._max_bytes // (1024 * 1024)} MB")
        return fetched.body


def board_url(ats_type: str, slug: str) -> str | None:
    """The list URL for one board, built only from a single safe path segment."""
    if ats_type not in BOARDS or _SLUG.fullmatch(slug) is None:
        return None
    if ats_type == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if ats_type == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def read(ats: str, slug: str, client: Client) -> list[Job | Skip]:
    """Every job on one board. Raises on a fetch error, a non-200 reply or a bad body."""
    url = board_url(ats, slug)
    if url is None:
        raise BoardParseError("not a board this module reads")
    return _parse(ats, slug, client.get_json(url))


def _parse(ats: str, slug: str, data: object) -> list[Job | Skip]:
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


def _greenhouse_job(item: object, slug: str) -> Job | Skip:
    job = _mapping(item)
    if job is None:
        return Skip("a job was not an object")
    title = _text(job.get("title"))
    if title is None:
        return Skip("a job has no title")
    # The list payload has no documented company field. Honor company_name when present.
    return Job(
        title=title,
        location=_nested_text(job.get("location"), "name"),
        work_model=None,
        url=_http_url(job.get("absolute_url"))
        or _built(f"https://boards.greenhouse.io/{slug}/jobs/", job.get("id")),
        requisition_id=_text(job.get("requisition_id")),
        company_field=_text(job.get("company_name")),
        description=_greenhouse_text(job.get("content")),
    )


def _greenhouse_text(content: object) -> str | None:
    """`content` (with `?content=true`) is entity-escaped HTML; plain text, or None."""
    text = _text(content)
    if text is None:
        return None
    return htmltext.to_text(text, escaped=True) or None


def _lever_job(item: object, slug: str) -> Job | Skip:
    job = _mapping(item)
    if job is None:
        return Skip("a job was not an object")
    title = _text(job.get("text"))
    if title is None:
        return Skip("a job has no title")
    return Job(
        title=title,
        location=_nested_text(job.get("categories"), "location"),
        work_model=_mapped(job.get("workplaceType"), _LEVER_WORK),
        url=_http_url(job.get("hostedUrl"))
        or _built(f"https://jobs.lever.co/{slug}/", job.get("id")),
        requisition_id=_id_text(job.get("id")),
        company_field=None,
        description=_lever_text(job),
    )


def _lever_text(job: dict[str, Any]) -> str | None:
    """The description, the extra lists, the closing text and the salary description.

    Field names from the Lever postings API README (github.com/lever/postings-api):
    `descriptionPlain`, `lists` (`{text, content}` with HTML content),
    `additionalPlain` and `salaryDescriptionPlain`. `salaryRange` is not read: the
    README does not document its `interval` values.
    """
    parts = [_text(job.get("descriptionPlain"))]
    for item in job.get("lists") or []:
        if isinstance(item, dict):
            parts.append(_text(item.get("text")))
            content = _text(item.get("content"))
            parts.append(htmltext.to_text(content) if content else None)
    parts.append(_text(job.get("additionalPlain")))
    parts.append(_text(job.get("salaryDescriptionPlain")))
    return _joined(parts)


def _ashby_text(job: dict[str, Any]) -> str | None:
    """The description and, with `includeCompensation=true`, the salary summary.

    Field names from Ashby's public job posting API docs
    (developers.ashbyhq.com/docs/public-job-posting-api): `descriptionPlain` and
    `compensation.scrapeableCompensationSalarySummary` on each job, such as
    "$120K - $150K". The summary gets a "Salary:" label so it reads as pay.
    """
    compensation = job.get("compensation")
    summary = (
        _text(compensation.get("scrapeableCompensationSalarySummary"))
        if isinstance(compensation, dict)
        else None
    )
    return _joined([_text(job.get("descriptionPlain")), f"Salary: {summary}" if summary else None])


def _joined(parts: list[str | None]) -> str | None:
    """Non-empty parts, one per line (so a sentence never runs into the next); or None."""
    return "\n".join(part for part in parts if part) or None


def _ashby_job(item: object, slug: str) -> Job | Skip:
    job = _mapping(item)
    if job is None:
        return Skip("a job was not an object")
    title = _text(job.get("title"))
    if title is None:
        return Skip("a job has no title")
    return Job(
        title=title,
        location=_text(job.get("location")) or "",
        work_model=_ashby_work(job),
        url=_http_url(job.get("jobUrl"))
        or _built(f"https://jobs.ashbyhq.com/{slug}/", job.get("id")),
        requisition_id=_id_text(job.get("id")),
        company_field=None,
        description=_ashby_text(job),
    )


def _ashby_work(job: dict[str, Any]) -> str | None:
    # Ashby sets isRemote for hybrid as well as remote, so workplaceType wins when
    # the board sends it. isRemote is only the fallback when that field is absent.
    if "workplaceType" in job:
        return _mapped(job.get("workplaceType"), _ASHBY_WORK)
    if job.get("isRemote") is True:
        return "remote"
    return None


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
