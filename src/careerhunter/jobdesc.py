"""Job-description text, from a paste, a file or an allowed fetch (ADR-024).

A `JobDescription` is the text of one job description as it is kept in the record,
with its source:

- **pasted**: kept verbatim. It must be valid UTF-8 with no NUL, and at most
  MAX_CHARS characters.
- **file**: a PDF, HTML or plain-text file, its type judged from its bytes and never
  from a name. A PDF's text comes through `pdftext.py` (an encrypted one is refused),
  HTML's through `htmltext.py`, and plain text must be strict UTF-8. A PDF that opens
  but has no text, such as a scan, is still a JobDescription, with empty text: its file
  is saved as the job-ad snapshot, but it is not a JD (`has_text` is false), so no
  `job_descriptions` row is written for it.
- **fetched**: the page's JSON-LD `JobPosting.description`, or the description in the
  ATS's public JSON (`snapshots.ats_json_url`), through the caller's fetcher. Never for
  a host that is not fetched (`urls.no_fetch`).

`text_sha256` covers the exact text that is stored. `save` writes every JD to a
snapshot file through `snapshots.save` (owner-only: files 0600, folders 0700): a paste
or a fetched text as `.txt`, a file as it came, in its own type.

Every failure is a `JobDescError` with a fixed, user-facing message that quotes no
content and no path.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from careerhunter import ats, htmltext, pdftext, snapshots, urls
from careerhunter.fetch import FetchError
from careerhunter.snapshots import Fetcher

MAX_CHARS = 100_000
MAX_FILE_BYTES = 20 * 1024 * 1024  # the bot's upload cap
MAX_PAGES = 200  # the resume reader's page cap
SOURCES = ("pasted", "file", "fetched")
FILE_TYPES = ("pdf", "html", "txt")

_UTF8_BOM = b"\xef\xbb\xbf"
_HTML_START = re.compile(rb"<!doctype\s+html|<html[\s>]", re.IGNORECASE)
_HTML_SNIFF = 1024

_MESSAGES = {
    "empty": "The job description is empty.",
    "nul": "The job description contains a NUL character.",
    "utf8": "The job description is not valid UTF-8 text.",
    "too_long": f"The job description is longer than {MAX_CHARS:,} characters.",
    "too_large": "The file is larger than 20 MB.",
    "unsupported": "The file is not a PDF, HTML or plain-text file.",
    "encrypted": "The PDF is encrypted, so it can't be read. Nothing was saved.",
    "pdf_unreadable": "The PDF could not be read. Nothing was saved.",
    "too_many_pages": f"The PDF has more than {MAX_PAGES} pages.",
    "no_text": "No text was found in the file.",
}


class JobDescError(Exception):
    """The job description was refused. The message is user-facing and fixed."""

    def __init__(self, reason: str) -> None:
        if reason not in _MESSAGES:
            raise ValueError(f"unknown job description error reason {reason!r}")
        self.reason = reason
        super().__init__(_MESSAGES[reason])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class JobDescription:
    """One job description. `text` is empty only for a PDF with no extractable text."""

    text: str = field(repr=False)
    source: str
    file_type: str  # the snapshot file's type
    file_data: bytes | None = field(default=None, repr=False)  # a file's own bytes

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"unknown job description source {self.source!r}")
        if self.file_type not in FILE_TYPES:
            raise ValueError(f"unknown job description file type {self.file_type!r}")
        if not self.text and not (self.source == "file" and self.file_type == "pdf"):
            raise ValueError("only a PDF file can have no text")

    @property
    def has_text(self) -> bool:
        return bool(self.text)

    @property
    def text_sha256(self) -> str | None:
        return sha256(self.text.encode("utf-8")) if self.text else None

    @property
    def file_bytes(self) -> bytes:
        """The bytes of the snapshot file: the file itself, or the text as UTF-8."""
        return self.file_data if self.file_data is not None else self.text.encode("utf-8")

    @property
    def file_sha256(self) -> str:
        return sha256(self.file_bytes)


@dataclass(frozen=True)
class SavedJd:
    """A job description whose snapshot file is saved, at `path` in the data folder."""

    description: JobDescription
    path: str

    @property
    def has_text(self) -> bool:
        return self.description.has_text


def _checked_text(text: str) -> str:
    if "\x00" in text:
        raise JobDescError("nul")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate
        raise JobDescError("utf8") from None
    if len(text) > MAX_CHARS:
        raise JobDescError("too_long")
    if not text.strip():
        raise JobDescError("empty")
    return text


def _decoded(data: bytes) -> str:
    try:
        return data.removeprefix(_UTF8_BOM).decode("utf-8")
    except UnicodeDecodeError:
        raise JobDescError("utf8") from None


def from_paste(text: str) -> JobDescription:
    """A pasted description, kept verbatim."""
    return JobDescription(_checked_text(text), "pasted", "txt")


def from_paste_bytes(data: bytes) -> JobDescription:
    """A pasted description given as bytes (standard input): strict UTF-8, verbatim."""
    if len(data) > MAX_FILE_BYTES:
        raise JobDescError("too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise JobDescError("utf8") from None
    return from_paste(text)


def file_type(data: bytes) -> str:
    """`pdf`, `html` or `txt`, judged from the bytes. Anything else is refused."""
    if data.startswith(pdftext.MAGIC):
        return "pdf"
    if _HTML_START.search(data[:_HTML_SNIFF].removeprefix(_UTF8_BOM)):
        return "html"
    if b"\x00" in data:
        raise JobDescError("unsupported")
    try:
        data.removeprefix(_UTF8_BOM).decode("utf-8")
    except UnicodeDecodeError:
        raise JobDescError("unsupported") from None
    return "txt"


_PDF_REASONS = {
    "unreadable": "pdf_unreadable",
    "encrypted": "encrypted",
    "too_many_pages": "too_many_pages",
    "too_long": "too_long",
}


def from_file(data: bytes) -> JobDescription:
    """A file's description. A PDF with no text comes back with empty text."""
    if len(data) > MAX_FILE_BYTES:
        raise JobDescError("too_large")
    kind = file_type(data)
    if kind == "pdf":
        try:
            text = pdftext.pdf_text(data, max_chars=MAX_CHARS, max_pages=MAX_PAGES)
        except pdftext.PdfTextError as exc:
            raise JobDescError(_PDF_REASONS[exc.reason]) from None
        text = text.replace("\x00", "")
        if not text.strip():
            return JobDescription("", "file", "pdf", data)
        if len(text) > MAX_CHARS:
            raise JobDescError("too_long")
        return JobDescription(text, "file", "pdf", data)
    text = htmltext.to_text(_decoded(data)) if kind == "html" else _decoded(data)
    if not text.strip():
        raise JobDescError("no_text")
    return JobDescription(_checked_text(text), "file", kind, data)


# --- Fetching -------------------------------------------------------------------------


def _json_ld_description(body: bytes) -> str | None:
    value = snapshots.posting_description(body)
    return htmltext.to_text(value) or None if value else None


def _ats_description(endpoint: str, data: Any) -> str | None:
    """The description in an ATS's public JSON for one posting (see `ats.py`)."""
    if not isinstance(data, dict):
        return None
    if "/wday/cxs/" in endpoint:
        info = data.get("jobPostingInfo")
        html = info.get("jobDescription") if isinstance(info, dict) else None
        if not isinstance(html, str):
            return None
        return htmltext.to_text(html) or None
    if "greenhouse.io" in endpoint:
        return ats._greenhouse_text(data.get("content"))
    if "lever.co" in endpoint:
        return ats._lever_text(data)
    if "ashbyhq.com" in endpoint:
        return ats._ashby_text(data)
    return None


def _from_ats(url: str, fetcher: Fetcher) -> str | None:
    endpoint = snapshots.ats_json_url(url)
    if endpoint is None:
        return None
    endpoint, _, ashby_id = endpoint.partition("#")
    if urls.no_fetch(endpoint):
        return None
    try:
        data = json.loads(fetcher(endpoint).body)
    except (FetchError, ValueError):
        return None
    if ashby_id:  # the Ashby board lists every job; keep only this one
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        data = next((j for j in jobs if isinstance(j, dict) and j.get("id") == ashby_id), None)
    return _ats_description(endpoint, data)


def fetched(url: str, fetcher: Fetcher) -> JobDescription | None:
    """The description fetched from the posting's page or its ATS's JSON, or None.

    Never fetches a host that is not fetched (`urls.no_fetch`). A text that fails the
    paste rules (too long, for one) is not used.
    """
    if not url or urls.no_fetch(url):
        return None
    text: str | None = None
    final = url
    try:
        page = fetcher(url)
    except FetchError:
        page = None
    if page is not None:
        final = page.url
        if page.status == 200:
            text = _json_ld_description(page.body)
    if text is None and not urls.no_fetch(final):
        text = _from_ats(final, fetcher)
    if not text:
        return None
    try:
        return JobDescription(_checked_text(text), "fetched", "txt")
    except JobDescError:
        return None


def save(data_dir: Path, jd: JobDescription, today: dt.date) -> SavedJd:
    """Write the JD's snapshot file (owner-only) and return it with its path."""
    return SavedJd(jd, snapshots.save(data_dir, jd.file_bytes, jd.file_type, today))
