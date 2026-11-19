"""The operator's scoring criteria: numbered rules, from a file in the data folder.

`search.criteria_file` names a text file relative to the data folder (for example
`criteria.md`). It holds one rule per line, numbered 1, 2, 3 in order, each with its
kind (ADR-028):

    1. veto: No night-shift roles at a warehouse.
    2. cap medium: A contract whose only stated path is a possible extension.
    3. veto [no car]: No role that needs a car for daily site visits.

A rule may carry a short label in square brackets after its kind, as in rule 3: at most
MAX_LABEL characters, with no brackets inside. The label names the rule in the skip
reason and in the bot's withdrawn message; it is not sent to the model.

Blank lines and lines that start with `#` are ignored. The rules go in the scoring
request. A posting the model shows meets a veto rule, with a quote from the posting
that the code checks, is skipped with reason `criteria: N. <the rule's first words>`.
A cap rule (`cap low`, `cap medium` or `cap high`, a fit level) lowers the posting's
fit to at most that level and never skips it. Unset means no criteria are sent.

A line that is not a numbered rule, numbers out of order, a rule with no valid kind or
a bad label (`no-kind`: empty, over MAX_LABEL characters, not closed or holding a
bracket), a rule over MAX_RULE characters or more than MAX_RULES rules make the file
unusable, so a file still written as prose, or as rules without kinds, is refused
rather than guessed.

The file is read only when it is inside the data folder once every symlink is
resolved, is a regular file readable by its owner only, is UTF-8, and is not empty
and at most MAX_CHARS characters long. Anything else is a CriteriaError with a fixed
reason, and the watcher then holds scoring for that run rather than score without the
stated criteria. No path and no file text is ever logged.

A change to the file does not re-score postings already scored.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_CHARS = 4_000
MAX_BYTES = MAX_CHARS * 4  # UTF-8 needs at most 4 bytes a character
MAX_PATH = 200
MAX_RULE = 300
MAX_RULES = 20
MAX_LABEL = 40
CONFIG_KEY = "search.criteria_file"
_RULE = re.compile(r"(\d{1,3})\.\s+(\S.*)")
_KIND = re.compile(r"(veto|cap\s+(low|medium|high))\s*(\[[^\]]*\]?)?\s*:\s*(\S.*)", re.IGNORECASE)
KINDS = ("veto", "cap")

REASONS = {
    "outside": "the path is outside the data folder",
    "missing": "the file does not exist",
    "unreadable": "it is not a readable regular file",
    "loose": "other users can read it (run: chmod 600 on it)",
    "not-utf8": "it is not UTF-8 text",
    "too-long": f"it is over the {MAX_CHARS:,}-character cap",
    "empty": "it is empty",
    "not-numbered": "a line is not a numbered rule (write one rule per line as 1. …)",
    "out-of-order": "the rules are not numbered 1, 2, 3 in order",
    "rule-too-long": f"a rule is over {MAX_RULE} characters",
    "too-many-rules": f"it has more than {MAX_RULES} rules",
    "no-kind": "a rule has no kind or a bad label (start each rule with veto: or "
    "cap low|medium|high:, a label as veto [label]:)",
}


class CriteriaError(Exception):
    """A criteria file that can't be used. `reason` is a key of REASONS; `chars` is the
    file's length in characters when it was read far enough to know it."""

    def __init__(self, reason: str, chars: int | None = None) -> None:
        super().__init__(REASONS[reason])
        self.reason = reason
        self.chars = chars


@dataclass(frozen=True)
class Rule:
    """One numbered rule: its text after the kind, the kind, a cap's fit level, and
    its label when the file gives one."""

    number: int
    text: str
    kind: str = "veto"
    cap: str | None = None  # "low", "medium" or "high" for a cap rule
    label: str | None = None


@dataclass(frozen=True)
class Criteria:
    text: str
    chars: int
    rules: tuple[Rule, ...] = ()

    def rule(self, number: int, kind: str | None = None) -> Rule | None:
        """Rule `number`, or None when the file has no such rule, or (with `kind`) when
        rule `number` is of another kind."""
        if 1 <= number <= len(self.rules):
            found = self.rules[number - 1]
            if kind is None or found.kind == kind:
                return found
        return None

    def count(self, kind: str) -> int:
        return sum(rule.kind == kind for rule in self.rules)


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
    return Criteria(text, len(text), parse_rules(text))


def parse_rules(text: str) -> tuple[Rule, ...]:
    """The numbered rules in `text`. Raises CriteriaError when a line is not one, the
    numbers are not 1, 2, 3 in order, a rule has no valid kind or a bad label, a rule is
    too long, or there are too many."""
    rules: list[Rule] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _RULE.fullmatch(line)
        if match is None:
            raise CriteriaError("not-numbered", chars=len(text))
        if int(match.group(1)) != len(rules) + 1:
            raise CriteriaError("out-of-order", chars=len(text))
        rest = " ".join(match.group(2).split())
        kind = _KIND.fullmatch(rest)
        if kind is None:
            raise CriteriaError("no-kind", chars=len(text))
        label = _label(kind.group(3), len(text))
        body = kind.group(4)
        if len(body) > MAX_RULE:
            raise CriteriaError("rule-too-long", chars=len(text))
        cap = kind.group(2).casefold() if kind.group(2) else None
        rules.append(Rule(len(rules) + 1, body, "cap" if cap else "veto", cap, label))
        if len(rules) > MAX_RULES:
            raise CriteriaError("too-many-rules", chars=len(text))
    if not rules:
        raise CriteriaError("empty", chars=len(text))
    return tuple(rules)


def _label(raw: str | None, chars: int) -> str | None:
    """The label inside `[…]`, or None when the rule has none. Raises CriteriaError when
    it is empty, too long, not closed or holds a bracket."""
    if raw is None:
        return None
    inner = raw[1:-1].strip() if raw.endswith("]") else ""
    if not inner or len(inner) > MAX_LABEL or "[" in inner:
        raise CriteriaError("no-kind", chars=chars)
    return inner


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
