"""Cutting a short phrase to fit, never in the middle of one (Batch R).

A key requirement is at most a fixed number of characters, both when it is stored
(`score.MAX_REQUIREMENT`) and when an alert shows it. A longer one is cut at its last
clause break that fits (a comma, semicolon, colon, slash, bracket, a spaced dash, or the
words "and", "or", "with", "including"), when that keeps at least `MIN_KEPT` of the
limit; otherwise at its last whole word. A dangling joining word ("and", "of", "such",
...) is dropped from the end, a bracket is never left open, and "…" marks the cut. The
result is never longer than the limit.
"""

from __future__ import annotations

import re

MIN_KEPT = 0.7  # a clause break is used when it keeps at least this share of the limit
ELLIPSIS = "…"
_STRIP = " ,;:/-(\u2013\u2014"
_BREAK = re.compile(
    r"\s*(?:[,;:/(\[]|\s[-\u2013\u2014]\s|\s(?:and|or|with|including|incl\.|e\.g\.)\s)",
    re.IGNORECASE,
)
_DANGLING = frozenset(
    (
        *("a", "an", "and", "as", "at", "by", "for", "from", "in", "including", "of"),
        *("on", "or", "the", "to", "with", "such", "like", "preferably", "ideally"),
        *("especially", "e.g.", "i.e.", "incl.", "&", "across", "into", "over", "under"),
        *("within", "between", "through", "about", "after", "before", "per", "via"),
        *("without", "several", "multiple", "various", "any", "all", "each", "our"),
        *("your", "their", "its", "this", "that", "these", "those", "other", "more"),
    )
)


def _trim(text: str) -> str:
    words = text.rstrip(_STRIP).split(" ")
    while len(words) > 1 and words[-1].casefold() in _DANGLING:
        words.pop()
    return " ".join(words).rstrip(_STRIP)


def cut(text: str, limit: int) -> str:
    """`text` on one line, at most `limit` characters, cut at a clause break or a word."""
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    room = limit - len(ELLIPSIS)
    head = line[: room + 1]  # one more, so a break just at the limit is seen
    breaks = [m.start() for m in _BREAK.finditer(head) if m.start() <= room]
    usable = [b for b in breaks if b >= limit * MIN_KEPT]
    if usable:
        kept = _trim(line[: usable[-1]])
    elif " " in line[:room]:
        kept = _trim(line[: line.rindex(" ", 0, room + 1)])
    else:
        kept = line[:room]
    if kept.count("(") > kept.count(")"):  # never leave a bracket open
        at = kept.rfind("(")
        kept = _trim(kept[:at]) if at >= limit * MIN_KEPT / 2 else kept.replace("(", "")
    kept = kept or line[:room]
    return kept[:room] + ELLIPSIS
