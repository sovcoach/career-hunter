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
"""

from __future__ import annotations

import io
import logging
import os
import re
import stat
import struct
import warnings
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pypdf
import pypdf.errors

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_XML = 5 * 1024 * 1024
MAX_ZIP_ENTRIES = 100
MAX_CHARS = 50_000
MAX_PAGES = 200

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
