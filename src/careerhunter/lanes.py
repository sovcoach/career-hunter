"""Match a posting's title to a lane from `search.lanes` (#33).

A lane is a named group of target titles with a priority (1 is best) and an optional
`fallback` flag. A title entry is a string, or a list of variants that count as one
title. A posting's title matches an entry when any variant appears in it as whole
words, ignoring case, by `filters.has_phrase`: the same folding as every other title
match here. When titles in several lanes match, the lane with the best (lowest)
priority wins. Priorities are unique (config validation), so there is never a tie.

Lanes only label and order postings. They never skip one and never exempt one from a
hard gate: `hard_gates.never_gated_families` is a separate list, and neither reads the
other. A config without `search.lanes` has no lanes, and lane checking is off.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from careerhunter.filters import has_phrase


@dataclass(frozen=True)
class Lane:
    name: str
    priority: int
    fallback: bool


def configured(doc: Mapping[str, Any] | None) -> list[Lane] | None:
    """The lanes in an already validated config, best priority first; None without any."""
    search = doc.get("search") if isinstance(doc, Mapping) else None
    raw = search.get("lanes") if isinstance(search, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return None
    lanes = [
        Lane(lane["name"], lane["priority"], bool(lane.get("fallback", False)))
        for lane in raw
        if isinstance(lane, Mapping)
    ]
    return sorted(lanes, key=lambda lane: lane.priority)


def match(title: str, doc: Mapping[str, Any] | None) -> Lane | None:
    """The best-priority lane with a title variant in `title`, or None for off-lane.

    Also None when there are no lanes; callers check `configured` first.
    """
    search = doc.get("search") if isinstance(doc, Mapping) else None
    raw = search.get("lanes") if isinstance(search, Mapping) else None
    if not isinstance(raw, list):
        return None
    best: Lane | None = None
    for lane in raw:
        if not isinstance(lane, Mapping):
            continue
        if any(has_phrase(title, variant) for variant in _variants(lane.get("titles"))):
            found = Lane(lane["name"], lane["priority"], bool(lane.get("fallback", False)))
            if best is None or found.priority < best.priority:
                best = found
    return best


def _variants(titles: object) -> Iterator[str]:
    if not isinstance(titles, list):
        return
    for entry in titles:
        if isinstance(entry, str):
            yield entry
        elif isinstance(entry, list):
            yield from (variant for variant in entry if isinstance(variant, str))
