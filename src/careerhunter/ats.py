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

import datetime as dt
import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from careerhunter import htmltext
from careerhunter.fetch import Fetched, FetchError, FetchTooLarge

Fetcher = Callable[[str], Fetched]
Poster = Callable[[str, object], Fetched]  # one POST of a JSON payload (fetch.post_json)
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
# Workday answers HTTP 400 to a page larger than 20 (checked on a live public board).
WORKDAY_PAGE = 20

BOARDS = ("greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr", "workday")
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
# A BambooHR slug and a Workday tenant are subdomains: one DNS label each.
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_WORKDAY_HOST = re.compile(r"^wd[0-9]{1,3}$")
# A Workday posting path: /job/<location>/<title>_<id>, URL-safe segments only.
_WORKDAY_PATH = re.compile(r"^/job(?:/[A-Za-z0-9._~%+-]+)+$")
_WORKDAY_DAYS_AGO = re.compile(r"Posted ([0-9]{1,3}) Days Ago")
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
    # The posted date the board gives, in UTC; or, for Workday's "30+ Days Ago", only
    # how many days old it is at least. Both None: the board gave no date it could read.
    posted: dt.date | None = None
    posted_min_days: int | None = None
    # `<ats_type>:<board>:<posting id>`: the posting's identity on its board, however
    # its URL reads (search migration 0015).
    key: str | None = None


@dataclass(frozen=True)
class Skip:
    reason: str


TooOld = Callable[[Job], bool]  # a posting already too old from its list date


@dataclass(frozen=True)
class Board:
    """One board's identity. Only Workday has a host and a site."""

    ats: str
    slug: str
    host: str | None = None
    site: str | None = None

    @property
    def key(self) -> str:
        """`<ats_type>:<slug>`, or `workday:<tenant>:<host>:<site>`, case-folded."""
        parts = [self.ats, self.slug] + (
            [self.host or "", self.site or ""] if self.ats == "workday" else []
        )
        return ":".join(parts).casefold()


@dataclass(frozen=True)
class PostingRef:
    """One posting on a board, as a discovery hit names it (#41).

    `ref` is the posting's id on its board, or Workday's `/job/...` path.
    """

    board: Board
    ref: str


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

    def __init__(
        self, fetcher: Fetcher, *, poster: Poster | None = None, max_bytes: int | None = None
    ) -> None:
        self._fetcher = fetcher
        self._poster = poster
        self._max_bytes = BOARD_MAX_BYTES if max_bytes is None else max_bytes
        self.used = 0

    @property
    def can_post(self) -> bool:
        return self._poster is not None

    def get_json(self, url: str) -> object:
        return _loads(self.get(url).body)

    def get(self, url: str) -> Fetched:
        """One GET, checked and counted like the others, with the reply as it came."""
        fetched = self._fetcher(url)
        self._checked(url, fetched)
        return fetched

    def post_json(self, url: str, payload: object) -> object:
        if self._poster is None:
            raise BoardParseError("no POST client")
        return _loads(self._checked(url, self._poster(url, payload)))

    def _checked(self, url: str, fetched: Fetched) -> bytes:
        if fetched.status != 200:
            raise BoardHTTPError(url, fetched.status, fetched.retry_after)
        self.used += len(fetched.body)
        if self.used > self._max_bytes:
            raise FetchTooLarge(f"the board is larger than {self._max_bytes // (1024 * 1024)} MB")
        return fetched.body


def list_url(board: Board) -> str | None:
    """The (first) list URL for a board, or None when its identity is not safe to use."""
    if board.ats != "workday":
        return board_url(board.ats, board.slug)
    if (
        _HOST_LABEL.fullmatch(board.slug) is None
        or board.host is None
        or _WORKDAY_HOST.fullmatch(board.host) is None
        or board.site is None
        or _SLUG.fullmatch(board.site) is None
    ):
        return None
    return f"{_workday_base(board)}/jobs"


def board_url(ats_type: str, slug: str) -> str | None:
    """The (first) list URL for one board, built only from a single safe path segment.

    Workday needs its host and site too: see `list_url`.
    """
    if ats_type not in BOARDS or ats_type == "workday" or _SLUG.fullmatch(slug) is None:
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


def posting_key(posting: PostingRef) -> str:
    """The posting's identity on its board, the same as `Job.key` for that posting."""
    return f"{posting.board.key}:{posting.ref}"


def _key(ats: str, slug: str, ident: str | None) -> str | None:
    return None if ident is None else f"{ats}:{slug.casefold()}:{ident}"


def posting_url(posting: PostingRef) -> str | None:
    """The URL the watcher stores for this posting when it reads it from its board, so a
    posting found by discovery and later on its board is one posting. None when the
    reference is not safe to use. Greenhouse's own `absolute_url` can be on the
    company's domain; the board URL here is then only the first check."""
    board, ref = posting.board, posting.ref
    if list_url(board) is None:
        return None
    if board.ats == "workday":
        if _WORKDAY_PATH.fullmatch(ref) is None or "/../" in f"{ref}/":
            return None
        return f"https://{board.slug}.{board.host}.myworkdayjobs.com/{board.site}{ref}"
    if _SLUG.fullmatch(ref) is None:
        return None
    if board.ats in ("greenhouse", "smartrecruiters", "bamboohr") and not ref.isdigit():
        return None
    return {
        "greenhouse": f"https://boards.greenhouse.io/{board.slug}/jobs/{ref}",
        "lever": f"https://jobs.lever.co/{board.slug}/{ref}",
        "ashby": f"https://jobs.ashbyhq.com/{board.slug}/{ref}",
        "smartrecruiters": f"https://jobs.smartrecruiters.com/{board.slug}/{ref}",
        "bamboohr": f"https://{board.slug}.bamboohr.com/careers/{ref}",
    }[board.ats]


def read_posting(posting: PostingRef, client: Client, *, today: dt.date | None = None) -> Job:
    """One posting, fetched once through its ATS's API (#41).

    Greenhouse, Lever, SmartRecruiters, BambooHR and Workday each have a single-posting
    endpoint (checked on live public boards); Ashby's public API has none, so its board
    is read and the job picked by id. Raises like `read`: a fetch error as it is, a
    non-200 reply as BoardHTTPError, and a body that is not that posting (or a posting
    that is not on its board) as BoardParseError.
    """
    board, ref = posting.board, posting.ref
    if posting_url(posting) is None:
        raise BoardParseError("not a posting this module reads")
    found: Job | Skip
    if board.ats == "greenhouse":
        data = client.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs/{ref}"
        )
        found = _greenhouse_job(data, board.slug)
    elif board.ats == "lever":
        data = client.get_json(f"https://api.lever.co/v0/postings/{board.slug}/{ref}?mode=json")
        found = _lever_job(data, board.slug)
    elif board.ats == "ashby":
        jobs = read(board, client).jobs
        matches = [job for job in jobs if isinstance(job, Job) and job.requisition_id == ref]
        if not matches:
            raise BoardParseError("the posting is not on its board")
        found = matches[0]
    elif board.ats == "smartrecruiters":
        data = client.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{board.slug}/postings/{ref}"
        )
        found = _smartrecruiters_job(data, board.slug)  # the detail has the list's fields
        if isinstance(found, Job):
            found = _smartrecruiters_detail(found, data)
    elif board.ats == "bamboohr":
        data = client.get_json(f"https://{board.slug}.bamboohr.com/careers/{ref}/detail")
        found = _bamboohr_posting(data, board.slug, ref)
    else:
        found = _workday_posting(client.get_json(f"{_workday_base(board)}{ref}"), board, ref, today)
    if isinstance(found, Skip):
        raise BoardParseError(found.reason)
    return found


def _bamboohr_posting(data: object, slug: str, ref: str) -> Job | Skip:
    """A posting from its detail alone: result.jobOpening has the title and location."""
    result = _mapping(data.get("result")) if isinstance(data, dict) else None
    opening = _mapping(result.get("jobOpening")) if result is not None else None
    if opening is None:
        raise BoardParseError("unexpected JSON shape")
    title = _text(opening.get("jobOpeningName"))
    if title is None:
        return Skip("a job has no title")
    stub = Job(
        title=title,
        location=_bamboohr_place(opening),
        work_model=None,
        url=f"https://{slug}.bamboohr.com/careers/{ref}",
        requisition_id=ref,
        company_field=None,
        key=_key("bamboohr", slug, ref),
    )
    return _bamboohr_detail(stub, data)


def _workday_posting(data: object, board: Board, ref: str, today: dt.date | None) -> Job | Skip:
    """A posting from its detail alone: jobPostingInfo has the title, location and date."""
    info = _mapping(data.get("jobPostingInfo")) if isinstance(data, dict) else None
    if info is None:
        raise BoardParseError("unexpected JSON shape")
    title = _text(info.get("title"))
    if title is None:
        return Skip("a job has no title")
    posted, at_least = _workday_posted(info.get("postedOn"), today)
    stub = Job(
        title=title,
        location="",
        work_model=None,
        url=f"https://{board.slug}.{board.host}.myworkdayjobs.com/{board.site}{ref}",
        requisition_id=None,
        company_field=None,
        posted=posted,
        posted_min_days=at_least,
        key=f"{board.key}:{ref}",
    )
    return _workday_detail(stub, data)


@dataclass(frozen=True)
class Confirmed:
    """What one board-API request says about a discovered board (#41).

    `exists` is True for a board that answered, False for one that does not exist
    (gone), and None when the answer cannot tell: SmartRecruiters answers an unknown
    company with an empty list, the same as a company with no openings. `names` are the
    company names the reply gives, for the exclusion check.
    """

    exists: bool | None
    names: tuple[str, ...] = ()


def confirm_board(board: Board, client: Client) -> Confirmed:
    """One request to the board's API: does it exist, and what is it called?

    Gone is HTTP 404 on every ATS (checked on live public boards), HTTP 422 from Workday
    (an unknown tenant), and a BambooHR reply that was redirected off the company's
    own subdomain (BambooHR sends an unknown company to its own site). Any other status
    raises BoardHTTPError, a fetch error passes through, and a body that is not the
    board's JSON raises BoardParseError.
    """
    url = list_url(board)
    if url is None:
        raise BoardParseError("not a board this module reads")
    gone_statuses = (404, 422) if board.ats == "workday" else (404,)
    try:
        if board.ats == "workday":
            data = client.post_json(
                url, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
            )
            if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list):
                raise BoardParseError("unexpected JSON shape")
            return Confirmed(True)
        if board.ats == "bamboohr":
            fetched = client.get(url)
            if (urllib.parse.urlsplit(fetched.url).hostname or "") != f"{board.slug}.bamboohr.com":
                return Confirmed(False)
            _bamboohr_list(board.slug, _loads(fetched.body))
            return Confirmed(True)
        if board.ats == "greenhouse":
            data = client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board.slug}")
            name = _text(data.get("name")) if isinstance(data, dict) else None
            if not isinstance(data, dict):
                raise BoardParseError("unexpected JSON shape")
            return Confirmed(True, (name,) if name else ())
        if board.ats == "lever":
            data = client.get_json(
                f"https://api.lever.co/v0/postings/{board.slug}?mode=json&limit=1"
            )
            if not isinstance(data, list):
                raise BoardParseError("unexpected JSON shape")
            return Confirmed(True)
        if board.ats == "smartrecruiters":
            data = client.get_json(
                f"https://api.smartrecruiters.com/v1/companies/{board.slug}/postings?limit=1"
            )
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, list):
                raise BoardParseError("unexpected JSON shape")
            names = tuple(
                name
                for row in content
                if isinstance(row, dict)
                and isinstance(row.get("company"), dict)
                and (name := _text(row["company"].get("name")))
            )
            return Confirmed(True if content else None, names)
        data = client.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise BoardParseError("unexpected JSON shape")
        return Confirmed(True)
    except BoardHTTPError as exc:
        if exc.status in gone_statuses:
            return Confirmed(False)
        raise


def _nothing_known(url: str) -> bool:
    return False


def _never_too_old(job: Job) -> bool:
    return False


def read(
    board: Board,
    client: Client,
    *,
    known: Known = _nothing_known,
    detail_limit: int | None = None,
    today: dt.date | None = None,
    too_old: TooOld = _never_too_old,
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

    `today` turns Workday's relative dates into dates. A posting the list already shows
    as `too_old` gets no detail request either: it is returned without a description,
    to be stored with its skip.
    """
    url = list_url(board)
    if url is None:
        raise BoardParseError("not a board this module reads")
    limit = DETAIL_LIMIT if detail_limit is None else detail_limit
    if board.ats == "smartrecruiters":
        stubs, notes = _smartrecruiters_list_pages(board.slug, client)
        found = _with_details(stubs, client, known, too_old, limit, _smartrecruiters_detail)
        found.notes[:0] = notes
        return found
    if board.ats == "bamboohr":
        stubs = _bamboohr_list(board.slug, client.get_json(url))
        return _with_details(stubs, client, known, too_old, limit, _bamboohr_detail)
    if board.ats == "workday":
        stubs, notes = _workday_list_pages(board, url, client, today)
        found = _with_details(stubs, client, known, too_old, limit, _workday_detail)
        found.notes[:0] = notes
        return found
    return BoardRead(_parse(board.ats, board.slug, client.get_json(url)))


def _with_details(
    stubs: list[Job | Skip],
    client: Client,
    known: Known,
    too_old: TooOld,
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
        if (item.url is not None and known(item.url)) or too_old(item):
            out.jobs.append(item)  # stored already, or skipped for its age: no detail
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
        posted=_utc_date(job.get("releasedDate")),
        key=_key("smartrecruiters", slug, ident),
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
        key=_key("bamboohr", slug, ident),
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
    return replace(
        stub,
        description=htmltext.to_text(html) if html else None,
        detail_url=None,
        posted=_plain_date(opening.get("datePosted")),
    )


# --- Workday ----------------------------------------------------------------------------
# The candidate-experience JSON behind each Workday careers site, checked against a live
# public board (Workday documents no public jobs API). A board is a tenant on a wdN host
# with a site: https://{tenant}.{wdN}.myworkdayjobs.com/{site}.
# POST /wday/cxs/{tenant}/{site}/jobs with {"appliedFacets": {}, "limit": 20, "offset":
# N, "searchText": ""} answers {total, jobPostings, facets}; `total` is only filled in on
# the first page, so paging stops at a short page. Each posting has title,
# externalPath, locationsText, postedOn ("Posted Today", "Posted 3 Days Ago",
# "Posted 30+ Days Ago") and bulletFields. GET /wday/cxs/{tenant}/{site}{externalPath}
# answers {jobPostingInfo, hiringOrganization}; jobPostingInfo has title, jobDescription
# (HTML), location, postedOn, startDate and externalUrl. The posting page is
# https://{tenant}.{wdN}.myworkdayjobs.com/{site}{externalPath}. No remote-work field
# was present, so no work model is read. An unknown site answers 404, an unknown tenant
# 422. hiringOrganization.name is a legal entity, not the employer's name, so it is not
# used.


def _workday_base(board: Board) -> str:
    return f"https://{board.slug}.{board.host}.myworkdayjobs.com/wday/cxs/{board.slug}/{board.site}"


def _workday_list_pages(
    board: Board, url: str, client: Client, today: dt.date | None
) -> tuple[list[Job | Skip], list[str]]:
    """Every page, by offset, until a short page, a repeated page or the page limit.

    A page whose externalPaths are the previous page's, in order, means the board is
    ignoring the offset: paging stops there, what was read is kept, and a note says so,
    the same as the page limit.
    """
    jobs: list[Job | Skip] = []
    offset = 0
    previous: list[object] | None = None
    for _page in range(MAX_PAGES):
        payload = {"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": ""}
        data = client.post_json(url, payload)
        rows = data.get("jobPostings") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise BoardParseError("unexpected JSON shape")
        paths = [row.get("externalPath") if isinstance(row, dict) else None for row in rows]
        if rows and paths == previous:
            return jobs, [
                f"repeated page at offset {offset} (the same postings as the page before); "
                "later postings left for the next run"
            ]
        previous = paths
        jobs.extend(_workday_job(item, board, today) for item in rows)
        offset += len(rows)
        if len(rows) < WORKDAY_PAGE:
            return jobs, []
    return jobs, [f"page limit reached ({MAX_PAGES} pages); later postings left for the next run"]


def _workday_job(item: object, board: Board, today: dt.date | None) -> Job | Skip:
    job = _mapping(item)
    if job is None:
        return Skip("a job was not an object")
    title = _text(job.get("title"))
    if title is None:
        return Skip("a job has no title")
    path = _text(job.get("externalPath"))
    if path is None or _WORKDAY_PATH.fullmatch(path) is None or "/../" in f"{path}/":
        return Skip(f"{title!r} has no usable path")
    posted, at_least = _workday_posted(job.get("postedOn"), today)
    return Job(
        title=title,
        location=_text(job.get("locationsText")) or "",
        work_model=None,
        url=f"https://{board.slug}.{board.host}.myworkdayjobs.com/{board.site}{path}",
        requisition_id=None,  # the URL is the identity; bulletFields are not documented
        company_field=None,
        detail_url=f"{_workday_base(board)}{path}",
        posted=posted,
        posted_min_days=at_least,
        key=f"{board.key}:{path}",
    )


def _workday_posted(value: object, today: dt.date | None) -> tuple[dt.date | None, int | None]:
    """A date from the forms seen on a live board: "Posted Today", "Posted N Days Ago"
    and "Posted 30+ Days Ago" (at least 30 days old). Any other text gives no date."""
    text = _text(value)
    if text is None or today is None:
        return None, None
    if text == "Posted Today":
        return today, None
    if text == "Posted 30+ Days Ago":
        return None, 30
    match = _WORKDAY_DAYS_AGO.fullmatch(text)
    if match is None:
        return None, None
    return today - dt.timedelta(days=int(match.group(1))), None


def _workday_detail(stub: Job, data: object) -> Job:
    info = _mapping(data.get("jobPostingInfo")) if isinstance(data, dict) else None
    if info is None:
        raise BoardParseError("unexpected JSON shape")
    html = _text(info.get("jobDescription"))
    # The list's locationsText can be a count ("6 Locations"); the detail names the
    # primary location.
    location = _text(info.get("location")) or stub.location
    return replace(
        stub,
        location=location,
        description=htmltext.to_text(html) if html else None,
        detail_url=None,
    )


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
        posted=_utc_date(job.get("first_published")),
        key=_key("greenhouse", slug, _path_id(job.get("id"))),
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
        posted=_epoch_ms_date(job.get("createdAt")),
        key=_key("lever", slug, _path_id(job.get("id"))),
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
        posted=_utc_date(job.get("publishedAt")),
        key=_key("ashby", slug, _path_id(job.get("id"))),
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


# --- posted dates ---------------------------------------------------------------------
# Greenhouse first_published, Ashby publishedAt and SmartRecruiters releasedDate are ISO
# timestamps with an offset; Lever createdAt is milliseconds since the epoch; BambooHR
# datePosted is a plain date. A value that does not read as one gives no date.


def _utc_date(value: object) -> dt.date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None  # no offset: the day is not known for sure
    return moment.astimezone(dt.UTC).date()


def _epoch_ms_date(value: object) -> dt.date | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000, dt.UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def _plain_date(value: object) -> dt.date | None:
    text = _text(value)
    if text is None or len(text) != 10:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None
