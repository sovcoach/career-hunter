"""Match a posting's title to a lane from `search.lanes` (#33).

A lane is a named group of target titles with a priority (1 is best) and an optional
`fallback` flag. A title entry is a string, or a list of variants that count as one
title. A posting's title matches an entry when any variant appears in it as whole
words, ignoring case, by `filters.has_phrase`: the same folding as every other title
match here. When titles in several lanes match, the lane with the best (lowest)
priority wins. Priorities are unique (config validation), so there is never a tie.

Only when no lane matches literally, the normalized rule is tried, by
`titles.loose_match`, and again the best priority wins. A variant that is a core of two
or more words and a role word (specialist, analyst, associate, one built-in group)
matches a posting with that core and any role word of the group, ignoring level words
(junior, senior, I to IV, "level N", ...) and after comma inversion ("Analyst, Client
Experience" is "Client Experience Analyst"). The role word must end the title or come
before a separator (comma, dash, bracket), so "IT Support Associate Manager" is not an
"IT Support Specialist". A one-word core ("Data Analyst") only matches literally.

Lanes only label and order postings. They never skip one and never exempt one from a
hard gate: `hard_gates.never_gated_families` is a separate list, and neither reads the
other. A config without `search.lanes` has no lanes, and lane checking is off.

Alert order (`alert_order`, ADR-015 as amended). With lanes, pending alerts go:
off-lane last; then the best fit first; then non-fallback lanes before fallback ones;
then the best lane priority; then the oldest (`first_seen`, then id). A stored lane
that is no longer in config sorts as a fallback after every configured lane, but before
off-lane. An off-lane posting whose fit is below `search.off_lane_min_fit` is held, and
so is a posting the watcher has not checked against the lanes yet. Without lanes,
alerts go by fit, then oldest, and none is held for its lane. The sort is done here in
Python, so it does not depend on how a SQLite version orders nulls.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from careerhunter.config import off_lane_min_fit
from careerhunter.enums import FIT_LEVELS
from careerhunter.filters import has_phrase
from careerhunter.search_store import PendingAlert
from careerhunter.titles import loose_match


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
    return _best(title, raw, has_phrase) or _best(title, raw, loose_match)


def _best(title: str, raw: list[Any], test: Callable[[str, str], bool]) -> Lane | None:
    """The best-priority lane with a variant that `test` matches, or None."""
    best: Lane | None = None
    for lane in raw:
        if not isinstance(lane, Mapping):
            continue
        if any(test(title, variant) for variant in _variants(lane.get("titles"))):
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


@dataclass(frozen=True)
class Ordered:
    """One alert to send, in order, with its lane line (None when lanes are off)."""

    posting_id: int
    lane_line: str | None


def alert_order(pending: list[PendingAlert], doc: Mapping[str, Any]) -> list[Ordered]:
    """The pending alerts to send this poll, in order. Held ones are left out."""
    lanes = configured(doc)
    if lanes is None:
        ranked = sorted(
            pending,
            key=lambda alert: (-_fit_rank(alert.fit), alert.first_seen, alert.posting_id),
        )
        return [Ordered(alert.posting_id, None) for alert in ranked]
    by_name = {lane.name: lane for lane in lanes}
    lowest = max(lane.priority for lane in lanes) + 1  # a missing lane goes after all
    min_rank = _fit_rank(off_lane_min_fit(doc))
    keyed: list[tuple[tuple[int, int, int, int, str, int], Ordered]] = []
    for alert in pending:
        if not alert.lane_checked:
            continue  # the watcher checks it on its next run
        rank = _fit_rank(alert.fit)
        if alert.lane is None:
            if rank < min_rank:
                continue  # off-lane and below the threshold: held
            key = (1, -rank, 1, lowest, alert.first_seen, alert.posting_id)
            keyed.append((key, Ordered(alert.posting_id, "Off-lane")))
            continue
        lane = by_name.get(alert.lane)
        fallback, priority = (1, lowest) if lane is None else (int(lane.fallback), lane.priority)
        key = (0, -rank, fallback, priority, alert.first_seen, alert.posting_id)
        keyed.append((key, Ordered(alert.posting_id, f"Lane: {alert.lane}")))
    return [ordered for _, ordered in sorted(keyed, key=lambda pair: pair[0])]


def _fit_rank(fit: str) -> int:
    """0 for low, 2 for high. An unknown value ranks below low, so it never jumps ahead."""
    return FIT_LEVELS.index(fit) if fit in FIT_LEVELS else -1
