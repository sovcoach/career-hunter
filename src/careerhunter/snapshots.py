"""Job-ad snapshots: a saved copy of each ad applied to, for the EI record (ADR-017).

Capturing a page, best source first:

1. **The page itself**, fetched safely (fetch.py). It is kept if it carries a
   schema.org `JobPosting` (JSON-LD), or enough readable text to be the ad.
2. **The ATS's public JSON**, for pages that are only a JavaScript shell. Workday's
   `/wday/cxs/...` endpoint, and the documented Greenhouse, Lever and Ashby APIs, return
   the posting's description. It is saved as a small HTML page that embeds the JSON.
3. Otherwise, **nothing**: the bot then asks for a PDF or screenshot of the ad, which
   is checked to really be a PDF, PNG or JPEG and saved the same way.

Files go in snapshots/<YYYY>/<random>.<ext> under the data directory, created owner-only
(0600, folders 0700) and never overwritten. Paths are stored relative to the data
directory, as the schema requires.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from careerhunter.fetch import Fetched, FetchError

MIN_READABLE_CHARS = 1500
UPLOAD_TYPES = {b"%PDF-": "pdf", b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}

Fetcher = Callable[[str], Fetched]


@dataclass(frozen=True)
class Capture:
    data: bytes
    ext: str
    source: str  # "page" or "ats_json"


@dataclass(frozen=True)
class PostingInfo:
    title: str | None = None
    company: str | None = None
    location: str | None = None


class _PageParser(HTMLParser):
    """Collects JSON-LD blocks, the <title>, and the visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.title = ""
        self.text: list[str] = []
        self._in: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and ("type", "application/ld+json") in attrs:
            self._in = "ld"
            self.json_ld.append("")
        elif tag in ("script", "style", "noscript"):
            self._in = "skip"
        elif tag == "title":
            self._in = "title"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "title"):
            self._in = None

    def handle_data(self, data: str) -> None:
        if self._in == "ld":
            self.json_ld[-1] += data
        elif self._in == "title":
            self.title += data
        elif self._in is None:
            self.text.append(data)


def _parse(html: bytes) -> _PageParser:
    parser = _PageParser()
    parser.feed(html.decode("utf-8", "replace"))
    return parser


def _job_postings(blocks: list[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                found.append(node)
            walk(node.get("@graph"))

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return found


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return " ".join(unescape(value).split())
    return None


def posting_info(html: bytes) -> PostingInfo:
    """Title, company and location from JSON-LD, falling back to the page title."""
    page = _parse(html)
    for job in _job_postings(page.json_ld):
        org = job.get("hiringOrganization")
        company = _text(org.get("name")) if isinstance(org, dict) else _text(org)
        place = job.get("jobLocation")
        place = place[0] if isinstance(place, list) and place else place
        address = place.get("address") if isinstance(place, dict) else None
        location = None
        if isinstance(address, dict):
            bits = [_text(address.get(k)) for k in ("addressLocality", "addressRegion")]
            location = ", ".join(b for b in bits if b) or None
        return PostingInfo(_text(job.get("title")), company, location)
    return PostingInfo(title=_text(page.title))


def is_readable(html: bytes) -> bool:
    page = _parse(html)
    if _job_postings(page.json_ld):
        return True
    return len(" ".join(" ".join(page.text).split())) >= MIN_READABLE_CHARS


_WORKDAY = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$", re.IGNORECASE)


def ats_json_url(url: str) -> str | None:
    """The public JSON endpoint for a posting on a known ATS, if there is one."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    workday = _WORKDAY.match(host)
    if workday and "job" in segments:
        i = segments.index("job")
        # /<locale?>/<site>/job/<location>/<title_id>: the site is just before "job".
        if i >= 1 and len(segments) > i + 1:
            site, rest = segments[i - 1], "/".join(segments[i + 1 :])
            return f"https://{host}/wday/cxs/{workday['tenant']}/{site}/job/{rest}"
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and "jobs" in segments:
        i = segments.index("jobs")
        if i >= 1 and len(segments) > i + 1 and segments[i + 1].isdigit():
            return (
                f"https://boards-api.greenhouse.io/v1/boards/{segments[0]}/jobs/{segments[i + 1]}"
            )
    if host == "jobs.lever.co" and len(segments) >= 2:
        return f"https://api.lever.co/v0/postings/{segments[0]}/{segments[1]}"
    if host == "jobs.ashbyhq.com" and len(segments) >= 2:
        return f"https://api.ashbyhq.com/posting-api/job-board/{segments[0]}#{segments[1]}"
    return None


def _ats_json(url: str, fetcher: Fetcher) -> bytes | None:
    endpoint = ats_json_url(url)
    if endpoint is None:
        return None
    endpoint, _, ashby_id = endpoint.partition("#")
    try:
        reply = fetcher(endpoint)
        data = json.loads(reply.body)
    except (FetchError, ValueError):
        return None
    if ashby_id:  # the Ashby board lists every job; keep only this one
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        data = next((j for j in jobs if isinstance(j, dict) and j.get("id") == ashby_id), None)
    if not data:
        return None
    pretty = json.dumps(data, indent=2, ensure_ascii=False)
    return (
        '<!doctype html>\n<meta charset="utf-8">\n'
        f"<title>Job ad snapshot (ATS data)</title>\n"
        f"<p>Source: {escape(url)}<br>Data: {escape(endpoint)}</p>\n"
        f"<pre>{escape(pretty)}</pre>\n"
    ).encode()


def capture(url: str, fetcher: Fetcher) -> Capture | None:
    """Fetch a copy of the ad, or None if neither the page nor an ATS API gave one."""
    try:
        page = fetcher(url)
    except FetchError:
        page = None
    if page is not None and page.status == 200 and is_readable(page.body):
        return Capture(page.body, "html", "page")
    data = _ats_json(page.url if page else url, fetcher)
    if data is not None:
        return Capture(data, "html", "ats_json")
    return None


def upload_type(data: bytes) -> str | None:
    """The file extension for an uploaded snapshot, judged by its bytes, or None."""
    for magic, ext in UPLOAD_TYPES.items():
        if data.startswith(magic):
            return ext
    return None


def save(data_dir: Path, data: bytes, ext: str, today: dt.date) -> str:
    """Write a new snapshot owner-only and return its path relative to the data dir."""
    if not re.fullmatch(r"[a-z]{3,4}", ext):
        raise ValueError(f"unexpected snapshot extension {ext!r}")
    folder = data_dir / "snapshots" / f"{today.year:04d}"
    folder.mkdir(mode=0o700, parents=False, exist_ok=True)
    os.chmod(folder, 0o700)
    relative = f"snapshots/{today.year:04d}/{secrets.token_hex(8)}.{ext}"
    fd = os.open(data_dir / relative, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return relative
