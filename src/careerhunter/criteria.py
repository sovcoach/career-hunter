"""The operator's scoring criteria in plain language, from a file in the data folder.

`search.criteria_file` names a text file relative to the data folder (for example
`criteria.md`). Its text goes in the scoring request as the candidate's own stated
criteria, which the model is told to weigh when it marks a gap minor or core. Unset
means no criteria are sent.

The file is read only when it is inside the data folder once every symlink is
resolved, is a regular file readable by its owner only, is UTF-8, and is not empty
and at most MAX_CHARS characters long. Anything else is a CriteriaError with a fixed
reason, and the watcher then holds scoring for that run rather than score without the
stated criteria. No path and no file text is ever logged.

A change to the file does not re-score postings already scored.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_CHARS = 4_000
MAX_BYTES = MAX_CHARS * 4  # UTF-8 needs at most 4 bytes a character
MAX_PATH = 200
CONFIG_KEY = "search.criteria_file"

REASONS = {
    "outside": "the path is outside the data folder",
    "missing": "the file does not exist",
    "unreadable": "it is not a readable regular file",
    "loose": "other users can read it (run: chmod 600 on it)",
    "not-utf8": "it is not UTF-8 text",
    "too-long": f"it is over the {MAX_CHARS:,}-character cap",
    "empty": "it is empty",
}


class CriteriaError(Exception):
    """A criteria file that can't be used. `reason` is a key of REASONS; `chars` is the
    file's length in characters when it was read far enough to know it."""

    def __init__(self, reason: str, chars: int | None = None) -> None:
        super().__init__(REASONS[reason])
        self.reason = reason
        self.chars = chars


@dataclass(frozen=True)
class Criteria:
    text: str
    chars: int


def configured_value(config: Mapping[str, Any] | None) -> str | None:
    """search.criteria_file, or None when it is unset, null or blank."""
    search = config.get("search") if isinstance(config, Mapping) else None
    value = search.get("criteria_file") if isinstance(search, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value
    return None


def path_problem(value: object) -> str | None:
    """Why a search.criteria_file value is refused by config validation, or None."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_PATH:
        return f"must be a path of 1 to {MAX_PATH} characters, relative to the data folder"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or value != value.strip():
        return "must have no control characters and no spaces at either end"
    if value.startswith("~") or PurePosixPath(value).is_absolute():
        return "must be relative to the data folder, not an absolute path"
    return None


def load(root: Path, config: Mapping[str, Any] | None) -> Criteria | None:
    """The criteria text, or None when search.criteria_file is unset. Raises CriteriaError."""
    value = configured_value(config)
    if value is None:
        return None
    if path_problem(value) is not None:
        raise CriteriaError("outside")
    try:
        base = root.resolve(strict=True)
        target = (base / value).resolve(strict=True)
    except FileNotFoundError:
        raise CriteriaError("missing") from None
    except (OSError, RuntimeError):  # a symlink loop, or unreadable on the way
        raise CriteriaError("unreadable") from None
    if not target.is_relative_to(base) or target == base:
        raise CriteriaError("outside")
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise CriteriaError("missing") from None
    except OSError:
        raise CriteriaError("unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CriteriaError("unreadable")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise CriteriaError("loose")
        data = _read(fd)
    except OSError:
        raise CriteriaError("unreadable") from None
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise CriteriaError("too-long")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CriteriaError("not-utf8") from None
    text = text.strip()
    if not text:
        raise CriteriaError("empty", chars=0)
    if len(text) > MAX_CHARS:
        raise CriteriaError("too-long", chars=len(text))
    return Criteria(text, len(text))


def _read(fd: int) -> bytes:
    """At most MAX_BYTES + 1 bytes, so an oversized file is known without reading it all."""
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_BYTES:
        chunk = os.read(fd, MAX_BYTES + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)
