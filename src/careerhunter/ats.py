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
from dataclasses import dataclass, field, replace
from typing import Any

from careerhunter import htmltext
from careerhunter.fetch import Fetched, FetchError, FetchTooLarge

Fetcher = Callable[[str], Fetched]
Known = Callable[[str], bool]  # a posting URL already stored: its detail is not fetched

# The body cap for one board, all of its requests together. Greenhouse boards carry
# every description (content=true), so this is larger than a job ad's cap. The CLI
# also gives it to the fetcher as the cap for each single request.
BOARD_MAX_BYTES = 20 * 1024 * 1024
# A board whose list has no descriptions gets one detail request per new posting, at
# most this many per board per run. The rest are left for the next run, so one large
# board cannot take the run budget from the others.
DETAIL_LIMIT = 40
# List pages read per board per run, for the boards that page.
MAX_PAGES = 25
SMARTRECRUITERS_PAGE = 100

BOARDS = ("greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr")
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
# A BambooHR slug is the company's subdomain: one DNS label.
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# The public postings API says "on-site"; the hire API says "onsite".
_LEVER_WORK = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
_ASHBY_WORK = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}
# SmartRecruiters job ad sections, in the order a reader sees them. Each is {title, text}
# with HTML text (developers.smartrecruiters.com/docs/objects, and a live public board).
_SMARTRECRUITERS_SECTIONS = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)


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
    detail_url: str | None = None  # where the description is, when the list has none


@dataclass(frozen=True)
class Skip:
    reason: str


@dataclass(frozen=True)
class Board:
    """One board's identity. Only Workday has a host and a site."""

    ats: str
    slug: str
    host: str | None = None
    site: str | None = None


@dataclass
class BoardRead:
    """What one board gave this run.

    `notes` are log lines: postings or pages left for the next run, and details that
    could not be read. `stopped` is the fetch or HTTP error that stopped the detail
    requests early; the postings read before it are kept, the others are left out
    (not stored) so the next run reads them as new.
    """

    jobs: list[Job | Skip]
    notes: list[str] = field(default_factory=list)
    stopped: Exception | None = None


class Client:
    """One board's requests, under one byte budget for all of them."""

    def __init__(self, fetcher: Fetcher, *, max_bytes: int | None = None) -> None:
        self._fetcher = fetcher
        self._max_bytes = BOARD_MAX_BYTES if max_bytes is None else max_bytes
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
    """The (first) list URL for one board, built only from a single safe path segment."""
    if ats_type not in BOARDS or _SLUG.fullmatch(slug) is None:
        return None
    if ats_type == "bamboohr":
        if _HOST_LABEL.fullmatch(slug) is None:
            return None
        return f"https://{slug}.bamboohr.com/careers/list"
    if ats_type == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if ats_type == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    if ats_type == "smartrecruiters":
        return _smartrecruiters_list(slug, 0)
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def _nothing_known(url: str) -> bool:
    return False


def read(
    board: Board,
    client: Client,
    *,
    known: Known = _nothing_known,
    detail_limit: int | None = None,
) -> BoardRead:
    """Every job on one board this run.

    A list request that fails raises: a fetch error as it is, a non-200 reply as
    BoardHTTPError, a bad body as BoardParseError, and the board stores nothing. Detail
    requests are made only for postings whose URL is not `known`, at most
    `detail_limit` (DETAIL_LIMIT by default). A detail that answers with a non-200
    status other than 429, or with a body that does not parse, leaves out that posting
    only, with a note. A fetch
    error or HTTP 429 stops the details: see `BoardRead.stopped`. Going over the byte
    budget always raises, so a board over the cap stores nothing.
    """
    url = board_url(board.ats, board.slug)
    if url is None:
        raise BoardParseError("not a board this module reads")
    limit = DETAIL_LIMIT if detail_limit is None else detail_limit
    if board.ats == "smartrecruiters":
        stubs, notes = _smartrecruiters_list_pages(board.slug, client)
        found = _with_details(stubs, client, known, limit, _smartrecruiters_detail)
        found.notes[:0] = notes
        return found
    if board.ats == "bamboohr":
        stubs = _bamboohr_list(board.slug, client.get_json(url))
        return _with_details(stubs, client, known, limit, _bamboohr_detail)
    return BoardRead(_parse(board.ats, board.slug, client.get_json(url)))


def _with_details(
    stubs: list[Job | Skip],
    client: Client,
    known: Known,
    limit: int,
    parse_detail: Callable[[Job, object], Job],
) -> BoardRead:
    """Fill in each new posting's description from its detail request."""
    out = BoardRead([])
    fetched = 0
    left = 0
    for item in stubs:
        if isinstance(item, Skip) or item.detail_url is None:
            out.jobs.append(item)
            continue
        if item.url is not None and known(item.url):
            out.jobs.append(item)  # stored already: no detail needed
            continue
        if out.stopped is not None or fetched >= limit:
            left += 1
            continue
        fetched += 1
        try:
            out.jobs.append(parse_detail(item, client.get_json(item.detail_url)))
        except FetchTooLarge:
            raise
        except BoardHTTPError as exc:
            if exc.status == 429:
                out.stopped = exc
                left += 1
            else:
                out.notes.append(f"{item.title!r}: detail HTTP {exc.status}; not recorded")
        except BoardParseError as exc:
            out.notes.append(f"{item.title!r}: detail {exc}; not recorded")
        except FetchError as exc:
            out.stopped = exc
            left += 1
    if left:
        why = "detail limit" if out.stopped is None else f"details stopped: {_kind(out.stopped)}"
        out.notes.append(f"{left} postings left for the next run ({why})")
    return out


def _kind(exc: Exception) -> str:
    if isinstance(exc, BoardHTTPError):
        return f"HTTP {exc.status}"
    return type(exc).__name__


# --- SmartRecruiters ------------------------------------------------------------------
# The public Posting API (developers.smartrecruiters.com/docs/endpoints, checked
# against a live public board): GET /v1/companies/{company}/postings pages with
# `limit` and `offset` and answers {offset, limit, totalFound, content}; each posting
# has id, name, refNumber, releasedDate, company {identifier, name} and location
# {city, region, country, remote, hybrid, fullLocation}. The list has no description:
# GET /v1/companies/{company}/postings/{id} has it in jobAd.sections. The posting
# page is https://jobs.smartrecruiters.com/{company}/{id}.
# An unknown company answers 200 with an empty list, not 404.


def _smartrecruiters_list(slug: str, offset: int) -> str:
    return (
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        f"?limit={SMARTRECRUITERS_PAGE}&offset={offset}"
    )


def _smartrecruiters_list_pages(slug: str, client: Client) -> tuple[list[Job | Skip], list[str]]:
    """Every page, by offset. The offset moves by the rows each page really gave."""
    jobs: list[Job | Skip] = []
    offset = 0
    for _page in range(MAX_PAGES):
        data = client.get_json(_smartrecruiters_list(slug, offset))
        content = data.get("content") if isinstance(data, dict) else None
        total = data.get("totalFound") if isinstance(data, dict) else None
        if not isinstance(content, list) or not isinstance(total, int) or not _count(total):
            raise BoardParseError("unexpected JSON shape")
        jobs.extend(_smartrecruiters_job(item, slug) for item in content)
        offset += len(content)
        if not content or offset >= total:
            return jobs, []
    return jobs, [f"page limit reached ({MAX_PAGES} pages); later postings left for the next run"]


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _smartrecruiters_job(item: object, slug: str) -> Job | Skip:
    job = _mapping(item)
    if job is None:
        return Skip("a job was not an object")
    title = _text(job.get("name"))
    if title is None:
        return Skip("a job has no title")
    ident = _path_id(job.get("id"))
    if ident is None:
        return Skip(f"{title!r} has no usable id")
    location = _mapping(job.get("location")) or {}
    company = _mapping(job.get("company")) or {}
    return Job(
        title=title,
        location=_smartrecruiters_place(location),
        work_model=_smartrecruiters_work(location),
        url=f"https://jobs.smartrecruiters.com/{slug}/{ident}",
        requisition_id=ident,
        company_field=_text(company.get("name")),
        detail_url=f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{ident}",
    )


def _smartrecruiters_place(location: dict[str, Any]) -> str:
    full = _text(location.get("fullLocation"))
    if full is not None:
        return full
    parts = [_text(location.get(key)) for key in ("city", "region", "country")]
    return ", ".join(part for part in parts if part)


def _smartrecruiters_work(location: dict[str, Any]) -> str | None:
    """`remote` and `hybrid` are booleans. Neither true says nothing for sure: None."""
    if location.get("remote") is True:
        return "remote"
    if location.get("hybrid") is True:
        return "hybrid"
    return None


def _smartrecruiters_detail(stub: Job, data: object) -> Job:
    detail = _mapping(data)
    ad = _mapping(detail.get("jobAd")) if detail is not None else None
    sections = _mapping(ad.get("sections")) if ad is not None else None
    if sections is None:
        raise BoardParseError("unexpected JSON shape")
    parts: list[str | None] = []
    for key in _SMARTRECRUITERS_SECTIONS:
        section = _mapping(sections.get(key))
        html = _text(section.get("text")) if section is not None else None
        parts.append(htmltext.to_text(html) if html else None)
    # The posting URL stays the one built from the list, so the next run knows it is
    # stored without asking for its detail again.
    return replace(stub, description=_joined(parts), detail_url=None)


# --- BambooHR ---------------------------------------------------------------------------
# The public careers site of each company, checked against a live public board (BambooHR
# documents no public careers API; its documented job API needs credentials):
# GET https://{company}.bamboohr.com/careers/list answers {meta, result}, each job with
# id (a string), jobOpeningName, location {city, state}, atsLocation {country, state,
# province, city}, isRemote and locationType. The list has no description: GET
# /careers/{id}/detail has result.jobOpening with description (HTML), datePosted and
# jobOpeningShareUrl. The posting page is https://{company}.bamboohr.com/careers/{id}.
# What isRemote and the locationType codes mean could not be verified, so no work model
# is read. An unknown company is redirected to BambooHR's own site, which is not JSON.


def _bamboohr_list(slug: str, data: object) -> list[Job | Skip]:
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        raise BoardParseError("unexpected JSON shape")
    return [_bamboohr_job(item, slug) for item in result]


def _bamboohr_job(item: object, slug: str) -> Job | Skip:
    job = _mapping(item)
    if job is None:
        return Skip("a job was not an object")
    title = _text(job.get("jobOpeningName"))
    if title is None:
        return Skip("a job has no title")
    ident = _path_id(job.get("id"))
    if ident is None:
        return Skip(f"{title!r} has no usable id")
    return Job(
        title=title,
        location=_bamboohr_place(job),
        work_model=None,
        url=f"https://{slug}.bamboohr.com/careers/{ident}",
        requisition_id=ident,
        company_field=None,
        detail_url=f"https://{slug}.bamboohr.com/careers/{ident}/detail",
    )


def _bamboohr_place(job: dict[str, Any]) -> str:
    """City and state, then the country when the ATS location gives one."""
    location = _mapping(job.get("location")) or {}
    ats_location = _mapping(job.get("atsLocation")) or {}
    parts = [
        _text(location.get("city")) or _text(ats_location.get("city")),
        _text(location.get("state"))
        or _text(ats_location.get("state"))
        or _text(ats_location.get("province")),
        _text(ats_location.get("country")),
    ]
    return ", ".join(part for part in parts if part)


def _bamboohr_detail(stub: Job, data: object) -> Job:
    result = _mapping(data.get("result")) if isinstance(data, dict) else None
    opening = _mapping(result.get("jobOpening")) if result is not None else None
    if opening is None:
        raise BoardParseError("unexpected JSON shape")
    html = _text(opening.get("description"))
    return replace(stub, description=htmltext.to_text(html) if html else None, detail_url=None)


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
