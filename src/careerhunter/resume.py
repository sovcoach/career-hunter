"""Read a resume locally: the text of a PDF or DOCX file, with no network.

The format is decided from the bytes, never from the file name:

- PDF: the file starts with `%PDF-`. It is read with pypdf. An encrypted PDF is
  refused before any page is touched, and so is one with more than MAX_PAGES pages.
  Text is taken page by page, and the first page that takes the total over MAX_CHARS
  is refused.
- DOCX: a zip that contains both `[Content_Types].xml` and `word/document.xml`. It is
  read with the standard library only. More than MAX_ZIP_ENTRIES entries is refused.
  `word/document.xml` is read with a bounded read of MAX_DOCUMENT_XML + 1 bytes, so a
  zip header that understates the size cannot get more past. It must be UTF-8, with
  no NUL byte, and must have no DOCTYPE anywhere; the parser reads exactly the bytes
  that were checked. Text is collected from the `w:t`, `w:tab`, `w:br` and
  `w:cr` elements, and the MAX_CHARS cap is checked as it is collected.

Anything else is refused. A file over a cap is refused, never truncated. Every
failure is a ResumeError with one of a fixed set of messages: no file content and no
path. It is raised `from None`, so no library exception travels with it.

`import_text` writes the text to `resume/current.txt` in the data folder (folder 0700,
file 0600) and records its SHA-256 in `search.db`. One import runs at a time, under an
exclusive lock on `resume/.import.lock`. The new file is written to a temporary name
with O_EXCL, renamed into place inside the database transaction, and the file it
replaces is kept as `resume/.previous.txt` until the COMMIT has succeeded. If the
COMMIT fails after the rename, the previous file is put back. If that fails too, or
the process stops in between, the file no longer matches its recorded hash:
`load` refuses it and `doctor` reports it, so it is never used silently.

`load` is the only way the rest of the code reads the resume. It returns nothing when
`search.resume` is unset, and refuses anything but the imported file, unchanged.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import io
import logging
import os
import re
import secrets
import sqlite3
import stat
import struct
import warnings
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdf
import pypdf.errors

from careerhunter import db

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_XML = 5 * 1024 * 1024
MAX_ZIP_ENTRIES = 100
MAX_CHARS = 50_000
MAX_PAGES = 200

# Where an import puts the text, relative to the data folder. search.resume names it.
RESUME_DIR = "resume"
CURRENT = "current.txt"
PREVIOUS = ".previous.txt"  # the file being replaced, kept only during a swap
LOCK = ".import.lock"
TMP_PREFIX = ".current."
TMP_SUFFIX = ".tmp"
CONFIG_VALUE = f"{RESUME_DIR}/{CURRENT}"
# 50,000 characters are at most four bytes each in UTF-8.
MAX_TEXT_BYTES = MAX_CHARS * 4

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_PARTS = ("[Content_Types].xml", "word/document.xml")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCTYPE = re.compile(rb"<!doctype", re.IGNORECASE)
_DECLARED_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*\bencoding\s*=\s*["']([^"']+)["']""")
_UTF8_BOM = b"\xef\xbb\xbf"

_MESSAGES = {
    "open": "the file could not be read",
    "too_large": "the file is larger than 10 MB",
    "unsupported": "the file is not a PDF or DOCX",
    "encrypted": "the PDF is encrypted",
    "pdf_unreadable": "the PDF could not be read",
    "too_many_pages": f"the PDF has more than {MAX_PAGES} pages",
    "docx_zip": "the DOCX is not a readable zip file",
    "docx_compression": "the DOCX uses a compression method that is not supported",
    "docx_entries": f"the DOCX has more than {MAX_ZIP_ENTRIES} parts",
    "docx_xml_large": "the DOCX document text is larger than 5 MB",
    "docx_encoding": "the DOCX document text is not UTF-8",
    "docx_doctype": "the DOCX document text declares a DOCTYPE",
    "docx_xml": "the DOCX document text is not valid XML",
    "too_long": f"the text is longer than {MAX_CHARS:,} characters",
    "empty": "no text was found in the file",
    "folder": "the resume folder in the data folder is not a plain folder",
    "import_running": "another resume import is running; try again when it has finished",
    "write": "the resume could not be written; nothing was imported",
    "incomplete": "the resume import did not complete; run careerhunter resume import again",
    "config": f"search.resume must be {CONFIG_VALUE} (set it after careerhunter resume import)",
    "not_imported": "no resume has been imported; run careerhunter resume import",
    "missing": "the imported resume file is missing; run careerhunter resume import",
    "loose": "the resume file is readable by others; run careerhunter doctor",
    "mismatch": "the resume file does not match its import; run careerhunter resume import",
}

# pypdf parses untrusted bytes and can fail in many ways besides its own errors. Only
# the pypdf calls themselves are wrapped with this, never code of ours.
_PDF_FAILURES = (
    pypdf.errors.PyPdfError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    AttributeError,
    RecursionError,
    ZeroDivisionError,
    OverflowError,
    struct.error,
    zlib.error,
)


class ResumeError(Exception):
    """A resume could not be read. The message is one of a fixed set."""

    def __init__(self, reason: str) -> None:
        if reason not in _MESSAGES:
            raise ValueError(f"unknown resume error reason {reason!r}")
        self.reason = reason
        super().__init__(_MESSAGES[reason])


def read_file(path: Path) -> bytes:
    """The bytes of the file at `path`, refusing anything over MAX_FILE_BYTES."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        raise ResumeError("open") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ResumeError("open")
        if info.st_size > MAX_FILE_BYTES:
            raise ResumeError("too_large")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:  # the file may grow after fstat
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError:
        raise ResumeError("open") from None
    finally:
        os.close(fd)
    if total > MAX_FILE_BYTES:
        raise ResumeError("too_large")
    return b"".join(chunks)


def extract_text(data: bytes) -> str:
    """The text of a PDF or DOCX file, decided from its bytes."""
    if len(data) > MAX_FILE_BYTES:
        raise ResumeError("too_large")
    if data.startswith(_PDF_MAGIC):
        text = _pdf_text(data)
    elif data.startswith(_ZIP_MAGIC):
        text = _docx_text(data)
    else:
        raise ResumeError("unsupported")
    text = text.replace("\x00", "").strip()
    if not text:
        raise ResumeError("empty")
    return text


@contextmanager
def _quiet_pypdf() -> Iterator[None]:
    """pypdf's log records and warnings can quote the file; none of them leave here."""
    # pypdf logs through child loggers (pypdf._reader and others) that take their level
    # from this one, so a level above CRITICAL silences them all, and nothing they log
    # is passed up to the handlers of the CLI or the bot.
    logger = logging.getLogger("pypdf")
    level, propagate = logger.level, logger.propagate
    logger.setLevel(logging.CRITICAL + 1)
    logger.propagate = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)
        logger.propagate = propagate


def _pdf_text(data: bytes) -> str:
    with _quiet_pypdf():
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            encrypted = reader.is_encrypted
        except _PDF_FAILURES:
            raise ResumeError("pdf_unreadable") from None
        if encrypted:
            raise ResumeError("encrypted")
        try:
            pages = reader.pages
            count = len(pages)
        except _PDF_FAILURES:
            raise ResumeError("pdf_unreadable") from None
        if count > MAX_PAGES:
            raise ResumeError("too_many_pages")
        parts: list[str] = []
        total = 0
        for index in range(count):
            try:
                page_text = pages[index].extract_text()
            except _PDF_FAILURES:
                raise ResumeError("pdf_unreadable") from None
            total += len(page_text) + 1  # the newline between pages counts too
            if total > MAX_CHARS:
                raise ResumeError("too_long")
            parts.append(page_text)
    return "\n".join(parts)


def _docx_xml(data: bytes) -> bytes:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError):
        raise ResumeError("docx_zip") from None
    with archive:
        names = archive.namelist()
        if len(names) > MAX_ZIP_ENTRIES:
            raise ResumeError("docx_entries")
        if not all(part in names for part in _DOCX_PARTS):
            raise ResumeError("unsupported")
        try:
            with archive.open(_DOCX_PARTS[1]) as part:
                xml = part.read(MAX_DOCUMENT_XML + 1)
        except NotImplementedError:
            raise ResumeError("docx_compression") from None
        except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, ValueError, OSError):
            # RuntimeError: an encrypted zip entry asks for a password.
            raise ResumeError("docx_zip") from None
    if len(xml) > MAX_DOCUMENT_XML:
        raise ResumeError("docx_xml_large")
    return xml


def _checked_xml(xml: bytes) -> bytes:
    """The same bytes, once they are known to be UTF-8 with no DOCTYPE anywhere.

    The parser is then given exactly these bytes. A NUL byte is refused first: XML
    forbids it, and UTF-16 or UTF-32 without a byte-order mark is otherwise valid
    UTF-8, which the parser would detect and read as UTF-16, hiding a DOCTYPE from a
    byte search. A UTF-16 or UTF-32 byte-order mark is not valid UTF-8. A declared
    encoding has to be UTF-8, so the parser reads the bytes the way they were checked.
    """
    if b"\x00" in xml:
        raise ResumeError("docx_encoding")
    try:
        xml.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ResumeError("docx_encoding") from None
    declared = _DECLARED_ENCODING.match(xml.removeprefix(_UTF8_BOM))
    if declared and declared.group(1).strip().lower() not in (b"utf-8", b"utf8"):
        raise ResumeError("docx_encoding")
    if _DOCTYPE.search(xml):
        raise ResumeError("docx_doctype")
    return xml


def _docx_text(data: bytes) -> str:
    xml = _checked_xml(_docx_xml(data))
    parts: list[str] = []
    total = 0
    try:
        # The bytes were checked above: UTF-8, and no DOCTYPE, so no entity can be
        # declared, and ElementTree never fetches an external one.
        for _event, element in ET.iterparse(io.BytesIO(xml), events=("end",)):  # noqa: S314
            tag = element.tag
            if tag == f"{_W}t":
                piece = element.text or ""
            elif tag == f"{_W}tab":
                piece = "\t"
            elif tag in (f"{_W}br", f"{_W}cr", f"{_W}p"):
                piece = "\n"
            else:
                continue
            total += len(piece)
            if total > MAX_CHARS:
                raise ResumeError("too_long")
            parts.append(piece)
    except ET.ParseError:
        raise ResumeError("docx_xml") from None
    return "".join(parts)


# --- importing and loading -------------------------------------------------------------------


@dataclass(frozen=True)
class Resume:
    """The resume in use. `repr` shows only the start of the hash, never the text."""

    text: str = field(repr=False)
    sha256: str

    def __repr__(self) -> str:
        return f"Resume(sha256={self.sha256[:12]}…, chars={len(self.text)})"


@dataclass(frozen=True)
class Imported:
    sha256: str
    chars: int


def _utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _private_folder(root: Path) -> Path:
    """resume/ in the data folder, created 0700 if missing. A link or a file is refused."""
    folder = root / RESUME_DIR
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise ResumeError("write") from None
    try:
        info = folder.lstat()
    except OSError:
        raise ResumeError("write") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ResumeError("folder")
    if stat.S_IMODE(info.st_mode) != 0o700:
        try:
            os.chmod(folder, 0o700, follow_symlinks=False)
        except OSError:
            raise ResumeError("write") from None
    return folder


@contextmanager
def _import_lock(folder: Path) -> Iterator[None]:
    """One import at a time. Another import holding the lock is refused, not waited for."""
    try:
        fd = os.open(folder / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise ResumeError("write") from None
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResumeError("import_running") from None
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _remove_leftovers(folder: Path) -> None:
    """Under the lock no swap is in progress, so these are left over from a crash."""
    for leftover in [folder / PREVIOUS, *folder.glob(f"{TMP_PREFIX}*{TMP_SUFFIX}")]:
        try:
            leftover.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise ResumeError("write") from None


def _write_temp(folder: Path, data: bytes) -> Path:
    tmp = folder / f"{TMP_PREFIX}{secrets.token_hex(8)}{TMP_SUFFIX}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise ResumeError("write") from None
    try:
        os.fchmod(fd, 0o600)  # the umask could have narrowed it; never widened
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except OSError:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise ResumeError("write") from None
    os.close(fd)
    return tmp


def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def import_text(
    root: Path,
    conn: sqlite3.Connection,
    text: str,
    *,
    now: Callable[[], str] = _utc_stamp,
) -> Imported:
    """Write `text` as resume/current.txt and record its SHA-256 in search.db.

    Raises ResumeError. Before the rename, a failure leaves the previous file and
    database as they were. After it, see the module docstring.
    """
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    folder = _private_folder(root)
    current, previous = folder / CURRENT, folder / PREVIOUS
    with _import_lock(folder):
        _remove_leftovers(folder)
        tmp = _write_temp(folder, data)
        swapped = False
        try:
            if current.is_symlink() or (current.exists() and not current.is_file()):
                raise ResumeError("folder")
            had_previous = current.exists()
            if had_previous:
                os.link(current, previous)
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO resume_import (sha256, imported_at) VALUES (?, ?)",
                    (digest, now()),
                )
                os.replace(tmp, current)
                swapped = True
                _fsync_dir(folder)
        except (sqlite3.Error, OSError):
            if swapped:
                _restore(folder, had_previous)
                raise ResumeError("incomplete") from None
            # Not renamed: current.txt is untouched, so the extra link to it goes.
            with suppress(OSError):
                previous.unlink(missing_ok=True)
            raise ResumeError("write") from None
        finally:
            tmp.unlink(missing_ok=True)
        # Committed. If this unlink fails, doctor reports the leftover and the next
        # import removes it.
        with suppress(OSError):
            previous.unlink(missing_ok=True)
    return Imported(digest, len(text))


def _restore(folder: Path, had_previous: bool) -> None:
    """Put the replaced file back after a failed COMMIT. If this fails, load refuses."""
    current, previous = folder / CURRENT, folder / PREVIOUS
    try:
        if had_previous:
            os.replace(previous, current)
        else:
            current.unlink(missing_ok=True)
        _fsync_dir(folder)
    except OSError:
        pass  # the file and search.db now disagree: load and doctor both say so


def latest_import(conn: sqlite3.Connection) -> str | None:
    """The SHA-256 of the latest import, or None when nothing has been imported."""
    row = conn.execute("SELECT sha256 FROM resume_import ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else str(row[0])


def _configured_value(config: Mapping[str, Any]) -> str | None:
    """search.resume, stripped, or None when it is unset or blank."""
    search = config.get("search")
    value = search.get("resume") if isinstance(search, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def configured(config: Mapping[str, Any]) -> bool:
    """Whether search.resume is set at all."""
    return _configured_value(config) is not None


def load(root: Path, conn: sqlite3.Connection, config: Mapping[str, Any]) -> Resume | None:
    """The imported resume, or None when search.resume is unset.

    Refuses, with a ResumeError, a search.resume other than resume/current.txt, a
    missing import, a file that is missing, a link, readable by others, or changed
    since its import.
    """
    value = _configured_value(config)
    if value is None:
        return None
    if value != CONFIG_VALUE:
        raise ResumeError("config")
    expected = latest_import(conn)
    if expected is None:
        raise ResumeError("not_imported")
    folder = root / RESUME_DIR
    try:
        if not stat.S_ISDIR(folder.lstat().st_mode):
            raise ResumeError("folder")
        fd = os.open(folder / CURRENT, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ResumeError("missing") from None
    except OSError:
        raise ResumeError("mismatch") from None  # a link, or unreadable
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ResumeError("mismatch")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ResumeError("loose")
        data = os.read(fd, MAX_TEXT_BYTES + 1)
    except OSError:
        raise ResumeError("mismatch") from None
    finally:
        os.close(fd)
    if len(data) > MAX_TEXT_BYTES or hashlib.sha256(data).hexdigest() != expected:
        raise ResumeError("mismatch")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ResumeError("mismatch") from None
    return Resume(text, expected)
