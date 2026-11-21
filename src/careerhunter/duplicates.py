"""One role posted more than once (Batch R, search migration 0036, EI migration 0005).

Two postings are the same role when all four hold:

- the same `companies` row;
- the same `filters.title_key` (the repost match `/log` already uses);
- the same location, normalized (case, spacing and punctuation folded), and both known:
  a posting whose location is unknown (`locations.unknown`) is never merged, so one
  application never covers two places;
- the same or near-identical job description: equal `jd_sha256` of the normalized text
  (`jobdesc.normalized`), or a `difflib` ratio of `MIN_RATIO` or more between the two
  normalized texts. A posting with no kept description is never merged.

Requisition and job ids are ignored. `postings.duplicate_of` points at the group's
first posting (the lowest id), never at another duplicate. A duplicate is never alerted
on its own: the first posting's digest row lists every requisition. Applied records one
application for the group, whose EI entry names every requisition; the other postings
are covered by it through `duplicate_of` (`covering_application`), never skipped, and
never re-scored. Skipped records a skip for every posting in it.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from pathlib import Path

from careerhunter import jobdesc, locations, snapshots
from careerhunter.filters import title_key

MIN_RATIO = 0.97
_PLACE = re.compile(r"[^\w]+")


def normalized_location(text: str) -> str:
    return " ".join(_PLACE.sub(" ", text.casefold()).split())


def _text(data_dir: Path, relative: str) -> str | None:
    data = snapshots.read_kept(data_dir, relative)
    if data is None:
        return None
    try:
        return jobdesc.from_file(data).text
    except jobdesc.JobDescError:
        return None


def find_original(
    conn: sqlite3.Connection, data_dir: Path, posting_id: int, text: str
) -> int | None:
    """The first posting of the group this one belongs to, or None."""
    row = conn.execute(
        "SELECT company_id, title, location, work_model, jd_sha256 FROM postings WHERE id = ?",
        (posting_id,),
    ).fetchone()
    if row is None or row["jd_sha256"] is None:
        return None
    location = str(row["location"] or "")
    if locations.unknown(location, title=str(row["title"]), work_model=row["work_model"]):
        return None
    key, place = title_key(str(row["title"])), normalized_location(location)
    mine = jobdesc.normalized(text)
    for other in conn.execute(
        "SELECT id, title, location, work_model, jd_sha256, snapshot_path FROM postings "
        "WHERE company_id = ? AND id < ? AND duplicate_of IS NULL AND jd_sha256 IS NOT NULL "
        "ORDER BY id",
        (row["company_id"], posting_id),
    ):
        if title_key(str(other["title"])) != key:
            continue
        if normalized_location(str(other["location"] or "")) != place:
            continue
        if other["jd_sha256"] == row["jd_sha256"]:
            return int(other["id"])
        theirs = _text(data_dir, str(other["snapshot_path"]))
        if theirs is None:
            continue
        if difflib.SequenceMatcher(None, mine, jobdesc.normalized(theirs)).ratio() >= MIN_RATIO:
            return int(other["id"])
    return None


def mark(conn: sqlite3.Connection, posting_id: int, original: int) -> None:
    conn.execute(
        "UPDATE postings SET duplicate_of = ? WHERE id = ? AND duplicate_of IS NULL",
        (original, posting_id),
    )


def group(conn: sqlite3.Connection, posting_id: int) -> list[int]:
    """Every posting in this one's group, its first posting first; just itself when it
    is in none."""
    row = conn.execute("SELECT duplicate_of FROM postings WHERE id = ?", (posting_id,)).fetchone()
    root = int(row["duplicate_of"]) if row is not None and row["duplicate_of"] else posting_id
    others = [
        int(r[0])
        for r in conn.execute("SELECT id FROM postings WHERE duplicate_of = ? ORDER BY id", (root,))
    ]
    return [root, *others]


def covering_application(conn: sqlite3.Connection, posting_id: int) -> int | None:
    """The application that covers this posting: the one recorded for its group's first
    posting, when it is a duplicate and that posting was applied to. None otherwise."""
    row = conn.execute(
        "SELECT a.id FROM postings p JOIN applications a ON a.posting_id = p.duplicate_of "
        "WHERE p.id = ? ORDER BY a.id LIMIT 1",
        (posting_id,),
    ).fetchone()
    return None if row is None else int(row[0])


def requisition(conn: sqlite3.Connection, posting_id: int) -> str:
    """How a posting is named in a group: its requisition id, else its board key's id,
    else `posting <id>`."""
    row = conn.execute(
        "SELECT requisition_id, ats_key FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()
    if row is not None and row["requisition_id"]:
        return str(row["requisition_id"])
    if row is not None and row["ats_key"]:
        return str(row["ats_key"]).rsplit(":", 1)[-1]
    return f"posting {posting_id}"


def requisitions(conn: sqlite3.Connection, posting_ids: list[int]) -> str | None:
    """The group's requisitions for its EI entry, such as `R-1, R-2`; None for one."""
    if len(posting_ids) < 2:
        return None
    named = list(dict.fromkeys(requisition(conn, i) for i in posting_ids))
    return ", ".join(named)[:500]
