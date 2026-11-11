"""The text of a PDF, read locally with pypdf, under caps the caller sets.

Shared by `resume.py` and `jobdesc.py`. An encrypted PDF is refused before any page is
touched, and so is one with more than `max_pages` pages. Text is taken page by page,
and the first page that takes the total over `max_chars` is refused. pypdf parses
untrusted bytes and can fail in many ways besides its own errors: every such failure
is a `PdfTextError` with a fixed reason, raised `from None`, and nothing pypdf logs or
warns leaves this module, since it can quote the file.
"""

from __future__ import annotations

import io
import logging
import struct
import warnings
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

import pypdf
import pypdf.errors

MAGIC = b"%PDF-"

# Only the pypdf calls themselves are wrapped with this, never code of ours.
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

REASONS = frozenset({"unreadable", "encrypted", "too_many_pages", "too_long"})


class PdfTextError(Exception):
    """The PDF's text could not be taken. `reason` is one of REASONS."""

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown PDF error reason {reason!r}")
        self.reason = reason
        super().__init__(reason)


@contextmanager
def quiet_pypdf() -> Iterator[None]:
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


def pdf_text(data: bytes, *, max_chars: int, max_pages: int) -> str:
    """The text of every page, joined with newlines. Raises PdfTextError."""
    with quiet_pypdf():
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            encrypted = reader.is_encrypted
        except _PDF_FAILURES:
            raise PdfTextError("unreadable") from None
        if encrypted:
            raise PdfTextError("encrypted")
        try:
            pages = reader.pages
            count = len(pages)
        except _PDF_FAILURES:
            raise PdfTextError("unreadable") from None
        if count > max_pages:
            raise PdfTextError("too_many_pages")
        parts: list[str] = []
        total = 0
        for index in range(count):
            try:
                page_text = pages[index].extract_text()
            except _PDF_FAILURES:
                raise PdfTextError("unreadable") from None
            total += len(page_text) + 1  # the newline between pages counts too
            if total > max_chars:
                raise PdfTextError("too_long")
            parts.append(page_text)
    return "\n".join(parts)
