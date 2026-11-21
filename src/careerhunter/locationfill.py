"""`careerhunter locations fill [--dry-run]`: blank stored locations, from what was kept.

For each posting in search.db whose location is blank, the first location found is
used, from, in order:

1. its job-ad snapshot (`postings.snapshot_path`): the page's JSON-LD
   (`snapshots.posting_info`), then a `Location:` line in its text;
2. the job descriptions kept in the EI records for its applications (the latest row
   for each), a `Location:` line in their text.

Nothing is fetched. A dry run opens every file read-only and writes nothing. Without
`--dry-run` the locations are written to search.db in one transaction, and only where
the location is still blank; the EI records are only read. A snapshot path outside
the data folder's `snapshots/` is never opened.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from careerhunter import db, jobdesc, locations, snapshots
from careerhunter.storage_guard import DataDir


@dataclass(frozen=True)
class Filled:
    posting_id: int
    company: str
    title: str
    location: str
    source: str  # "job ad" or "job description"


@dataclass(frozen=True)
class NotPlace:
    """A posting whose kept text has a `Location:` line that is not a place: left blank."""

    posting_id: int
    company: str
    title: str
    line: str


def _from_ad(data: bytes) -> tuple[str | None, str | None]:
    """The job ad's location (JSON-LD, then a `Location:` line that looks like a place),
    and a `Location:` line that does not, if any."""
    found = snapshots.posting_info(data).location
    if found:
        return found, None
    try:
        text = jobdesc.from_file(data).text
    except jobdesc.JobDescError:
        return None, None
    return locations.read_text(text)


def _ei_texts(datadir: DataDir) -> dict[str, list[str]]:
    """The latest job description text per application, by correlation id, from every
    EI record file, each opened read-only."""
    found: dict[str, list[str]] = {}
    for path in datadir.ei_record_files():
        conn = db.connect_read_only(path)
        try:
            rows = conn.execute(
                "SELECT a.correlation_id, j.text FROM job_descriptions j "
                "JOIN applications a ON a.id = j.application_id "
                "WHERE j.id = (SELECT max(k.id) FROM job_descriptions k "
                "WHERE k.application_id = j.application_id)"
            ).fetchall()
        except sqlite3.OperationalError:  # a record from before job descriptions (0001)
            rows = []
        finally:
            conn.close()
        for row in rows:
            found.setdefault(str(row["correlation_id"]), []).append(str(row["text"]))
    return found


def plan(
    search: sqlite3.Connection, datadir: DataDir, not_place: list[NotPlace] | None = None
) -> list[Filled]:
    """Every blank location a kept file names, without writing anything. A `Location:`
    line that is not a place (`locations.looks_like_place`) fills nothing; with
    `not_place`, each such posting is added to it, so the caller can say so."""
    texts: dict[str, list[str]] | None = None
    filled: list[Filled] = []
    rows = search.execute(
        "SELECT p.id, c.name AS company, p.title, p.snapshot_path FROM postings p "
        "JOIN companies c ON c.id = p.company_id WHERE trim(p.location) = '' ORDER BY p.id"
    ).fetchall()
    for row in rows:
        posting_id = int(row["id"])
        found: str | None = None
        rejected: str | None = None
        source = "job ad"
        if row["snapshot_path"]:
            data = snapshots.read_kept(datadir.root, str(row["snapshot_path"]))
            if data is not None:
                found, rejected = _from_ad(data)
        if not found:
            if texts is None:
                texts = _ei_texts(datadir)
            source = "job description"
            for app in search.execute(
                "SELECT correlation_id FROM applications WHERE posting_id = ? ORDER BY id",
                (posting_id,),
            ):
                for text in texts.get(str(app["correlation_id"]), []):
                    if found is None:
                        found, line = locations.read_text(text)
                        rejected = rejected or line
        if not found and rejected is not None and not_place is not None:
            not_place.append(NotPlace(posting_id, str(row["company"]), str(row["title"]), rejected))
        if found:
            filled.append(Filled(posting_id, str(row["company"]), str(row["title"]), found, source))
    return filled


def apply(search: sqlite3.Connection, filled: list[Filled]) -> int:
    """Write the planned locations where still blank, in one transaction."""
    written = 0
    with db.transaction(search):
        for item in filled:
            cur = search.execute(
                "UPDATE postings SET location = ? WHERE id = ? AND trim(location) = ''",
                (item.location, item.posting_id),
            )
            written += cur.rowcount
    return written
