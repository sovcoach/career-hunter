"""Match a posting's title to a lane from `search.lanes` (#33).

A lane is a named group of target titles with a priority (1 is best) and a group:
`primary` (the default), `career_change` or `stopgap` (`fallback: true` is read as
stopgap). A title entry is a string, or a list of variants that count as one
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
"IT Support Specialist". A one-word core ("Data Analyst") only matches literally. A
variant that is a junior or entry-level word and "Developer" ("Junior Developer")
matches a title with such a word, up to three words, then "Developer" ("Junior Java
Developer", "Entry-Level Full Stack Developer"), by `titles.junior_developer_match`.

A stored lane moves only through `careerhunter lanes reassign` (`plan_reassign`,
`reassign`): it recomputes every checked posting's lane against the current config and
matcher, and search migration 0021 logs each move to `lane_changes`. It also checks
every posting not checked yet, as the watcher's fill-in would; a first check is not a
move and is not logged. The watcher only fills in postings not yet checked.

Lanes only label and order postings. They never skip one and never exempt one from a
hard gate: `hard_gates.never_gated_families` is a separate list, and neither reads the
other. A config without `search.lanes` has no lanes, and lane checking is off.

Alert order (`alert_order`, ADR-015 as amended in Batch Q). With lanes, pending alerts
go by lane group first: every primary posting, then career change, then stopgap, then
off-lane; within a group, the best fit first; then the best lane priority; then the
oldest (`first_seen`, then id). So a High stopgap posting never outranks a primary
one, and when `alerts.max_per_poll` holds alerts back, stopgap postings wait first.
The group decides the order and the label (`Lane: support (stopgap)`), never whether a
posting alerts. A stored lane that is no longer in config sorts as stopgap after every
configured lane. An off-lane posting whose fit is below `search.off_lane_min_fit` is
held, every off-lane posting is with `never`, and so is a posting the watcher has not
checked against the lanes yet. Without lanes,
alerts go by fit, then oldest, and none is held for its lane. The sort is done here in
Python, so it does not depend on how a SQLite version orders nulls.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

from careerhunter import db, search_store
from careerhunter.config import LANE_GROUPS, max_posting_age_days, off_lane_min_fit
from careerhunter.enums import FIT_LEVELS
from careerhunter.filters import has_phrase, too_old
from careerhunter.search_store import PendingAlert
from careerhunter.titles import junior_developer_match, loose_match

GROUPS = LANE_GROUPS
_LABELS = {"primary": "", "career_change": " (career change)", "stopgap": " (stopgap)"}


@dataclass(frozen=True)
class Lane:
    name: str
    priority: int
    group: str = "primary"

    @property
    def fallback(self) -> bool:
        """True for a stopgap lane (the old `fallback: true`)."""
        return self.group == "stopgap"


def _lane(raw: Mapping[str, Any]) -> Lane:
    group = raw.get("group")
    if group not in GROUPS:
        group = "stopgap" if raw.get("fallback") is True else "primary"
    return Lane(raw["name"], raw["priority"], group)


def configured(doc: Mapping[str, Any] | None) -> list[Lane] | None:
    """The lanes in an already validated config, best priority first; None without any."""
    search = doc.get("search") if isinstance(doc, Mapping) else None
    raw = search.get("lanes") if isinstance(search, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return None
    lanes = [_lane(lane) for lane in raw if isinstance(lane, Mapping)]
    return sorted(lanes, key=lambda lane: lane.priority)


def match(title: str, doc: Mapping[str, Any] | None) -> Lane | None:
    """The best-priority lane with a title variant in `title`, or None for off-lane.

    Also None when there are no lanes; callers check `configured` first.
    """
    search = doc.get("search") if isinstance(doc, Mapping) else None
    raw = search.get("lanes") if isinstance(search, Mapping) else None
    if not isinstance(raw, list):
        return None
    return _best(title, raw, has_phrase) or _best(title, raw, _normalized)


def _normalized(title: str, variant: str) -> bool:
    """The rules tried when no lane matches literally: `titles.loose_match`, and the
    "Junior/Entry ... Developer" rule (`titles.junior_developer_match`)."""
    return loose_match(title, variant) or junior_developer_match(title, variant)


def _best(title: str, raw: list[Any], test: Callable[[str, str], bool]) -> Lane | None:
    """The best-priority lane with a variant that `test` matches, or None."""
    best: Lane | None = None
    for lane in raw:
        if not isinstance(lane, Mapping):
            continue
        if any(test(title, variant) for variant in _variants(lane.get("titles"))):
            found = _lane(lane)
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
    """One alert to send, in order, with its lane line (None when lanes are off) and its
    group's rank (0 primary, 1 career change, 2 stopgap, 3 off-lane; 0 without lanes)."""

    posting_id: int
    lane_line: str | None
    group: int = 0


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
    minimum = off_lane_min_fit(doc)
    never = minimum == "never"
    min_rank = len(FIT_LEVELS) if never else _fit_rank(minimum)
    stopgap = GROUPS.index("stopgap")
    keyed: list[tuple[tuple[int, int, int, str, int], Ordered]] = []
    for alert in pending:
        if not alert.lane_checked:
            continue  # the watcher checks it on its next run
        rank = _fit_rank(alert.fit)
        if alert.lane is None:
            if rank < min_rank:
                continue  # off-lane and below the threshold, or never: held
            key = (len(GROUPS), -rank, lowest, alert.first_seen, alert.posting_id)
            keyed.append((key, Ordered(alert.posting_id, "Off-lane", len(GROUPS))))
            continue
        lane = by_name.get(alert.lane)
        if lane is None:
            group, priority, label = stopgap, lowest, _LABELS["stopgap"]
        else:
            group, priority, label = GROUPS.index(lane.group), lane.priority, _LABELS[lane.group]
        key = (group, -rank, priority, alert.first_seen, alert.posting_id)
        keyed.append((key, Ordered(alert.posting_id, f"Lane: {alert.lane}{label}", group)))
    return [ordered for _, ordered in sorted(keyed, key=lambda pair: pair[0])]


@dataclass(frozen=True)
class LaneMove:
    """A checked posting whose lane changes. None is off-lane."""

    posting_id: int
    title: str
    old: str | None
    new: str | None


@dataclass(frozen=True)
class Reassignment:
    """What `lanes reassign` changes, or would change on a dry run."""

    checked: int  # postings already checked against the lanes
    moves: tuple[LaneMove, ...]
    released: tuple[int, ...]  # held, and alertable once moved or checked
    too_old: tuple[int, ...]  # released from the hold but past the posting-age gate
    newly_held: int  # alertable now, held off-lane once moved
    # Postings never checked against the lanes, checked now: `old` is None and means
    # "not checked", not off-lane. The watcher's own first check is not logged to
    # `lane_changes`, and neither is this one.
    first_checks: tuple[LaneMove, ...] = ()


def plan_reassign(conn: sqlite3.Connection, doc: Mapping[str, Any], today: dt.date) -> Reassignment:
    """Every checked posting's lane against the current config and matcher, and every
    posting not checked yet (`first_checks`). Reads only.

    A posting the move releases from the off-lane hold alerts as usual, through the
    bot's `alert_order` and `alerts.max_per_poll`. One past `search.max_posting_age_days`
    (its posted date, or first_seen) is listed in `too_old` instead: the age gate runs
    only on unscored postings, so `reassign` applies it here rather than let it pass.
    """
    rows = search_store.checked_lane_titles(conn)
    moves = []
    for row in rows:
        found = match(row.title, doc)
        new = None if found is None else found.name
        if new != row.lane:
            moves.append(LaneMove(row.posting_id, row.title, row.lane, new))
    unchecked = search_store.unchecked_lanes(conn)
    first = []
    for row in unchecked:
        found = match(row.title, doc)
        first.append(
            LaneMove(row.posting_id, row.title, None, None if found is None else found.name)
        )
    moved = {move.posting_id: move.new for move in moves}
    moved.update({move.posting_id: move.new for move in first})
    pending = search_store.pending_alerts(conn)
    after = [
        replace(alert, lane=moved[alert.posting_id], lane_checked=True)
        if alert.posting_id in moved
        else alert
        for alert in pending
    ]
    before_ids = {item.posting_id for item in alert_order(pending, doc)}
    after_ids = {item.posting_id for item in alert_order(after, doc)}
    posted = {row.posting_id: row.posted for row in (*rows, *unchecked)}
    max_age = max_posting_age_days(doc)
    released: list[int] = []
    aged: list[int] = []
    for posting_id in sorted(after_ids - before_ids):
        day = dt.date.fromisoformat(posted[posting_id][:10])
        (aged if too_old(day, None, today, max_age) else released).append(posting_id)
    return Reassignment(
        checked=len(rows),
        moves=tuple(moves),
        released=tuple(released),
        too_old=tuple(aged),
        newly_held=len(before_ids - after_ids),
        first_checks=tuple(first),
    )


def reassign(conn: sqlite3.Connection, doc: Mapping[str, Any], today: dt.date) -> Reassignment:
    """Apply `plan_reassign` in one transaction. `lane_changes` logs each move, and a
    released posting past the age gate gets a `too_old` skip, as the watcher gives."""
    with db.transaction(conn):
        planned = plan_reassign(conn, doc, today)
        for move in planned.moves:
            search_store.move_posting_lane(conn, move.posting_id, move.old, move.new)
        for move in planned.first_checks:
            search_store.set_posting_lane(conn, move.posting_id, move.new)
        for posting_id in planned.too_old:
            search_store.insert_skip(conn, posting_id, today.isoformat(), "too_old")
    return planned


def _fit_rank(fit: str) -> int:
    """0 for low, 2 for high. An unknown value ranks below low, so it never jumps ahead."""
    return FIT_LEVELS.index(fit) if fit in FIT_LEVELS else -1
