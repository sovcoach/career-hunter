"""`careerhunter report`: the one place the two layers are joined (#34, ADR-014 amended).

search.db is opened read-only, and the claim's EI record is attached to it read-only
(`mode=ro` on both), so the join can read and never write either file. No other code
path attaches a database: `ei_store` opens nothing, export reads only the claim's file,
and `doctor` queries each file on its own. A test checks that `ATTACH` appears in this
module only.

The two layers are linked only by the random `correlation_id`; there are no foreign
keys across files. The report is a private file, `reports/report-<claim>-<utc>.txt`,
mode 0600, in the data folder, never overwritten; the terminal prints only its path
relative to the data folder. It holds the EI facts of each application (employer, role,
date, method, the latest EI status) beside the search side's lane, fit word and source,
and counts by lane. Orphans on either side are counted, not listed: `doctor` names them.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from careerhunter import db

FOLDER = "reports"


class ReportError(Exception):
    """The report can't be made. The message is user-facing."""


@dataclass(frozen=True)
class Row:
    employer: str
    role: str
    when: str
    method: str
    ei_status: str
    lane: str
    fit: str
    source: str


@dataclass(frozen=True)
class Report:
    claim: str
    rows: tuple[Row, ...]
    voided: int
    only_ei: int
    only_search: int


def connect(search_path: Path, ei_path: Path) -> sqlite3.Connection:
    """search.db read-only, with the claim's file attached read-only as `ei`."""
    if not ei_path.is_file():
        raise ReportError(f"there is no record for that claim ({ei_path.name})")
    conn = db.connect_read_only(search_path)
    try:
        conn.execute("ATTACH DATABASE ? AS ei", (f"{ei_path.as_uri()}?mode=ro",))
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


_ROWS = """
SELECT e.employer, e.role, e.date_precision, e.date_applied, e.week_of, e.method, e.voided,
       (SELECT h.status FROM ei.status_history h WHERE h.application_id = e.id
        ORDER BY h.id DESC LIMIT 1) AS ei_status,
       p.lane, p.lane_checked, p.fit, p.source
FROM ei.applications e
JOIN main.applications a ON a.correlation_id = e.correlation_id
JOIN main.postings p ON p.id = a.posting_id
ORDER BY coalesce(e.date_applied, e.week_of, '9999'), e.id
"""
_ONLY_EI = """
SELECT count(*) FROM ei.applications e
WHERE NOT EXISTS (SELECT 1 FROM main.applications a WHERE a.correlation_id = e.correlation_id)
"""
_ONLY_SEARCH = """
SELECT count(*) FROM main.applications a
WHERE a.ei_claim = ?
  AND NOT EXISTS (SELECT 1 FROM ei.applications e WHERE e.correlation_id = a.correlation_id)
"""


def build(conn: sqlite3.Connection, claim: str) -> Report:
    """Read the joined rows and the orphan counts. Voided EI rows are counted, not shown."""
    rows: list[Row] = []
    voided = 0
    for r in conn.execute(_ROWS):
        if r["voided"]:
            voided += 1
            continue
        rows.append(
            Row(
                employer=str(r["employer"]),
                role=str(r["role"]),
                when=_when(r["date_precision"], r["date_applied"], r["week_of"]),
                method=str(r["method"]),
                ei_status=str(r["ei_status"] or "none"),
                lane=_lane(r["lane"], r["lane_checked"]),
                fit=str(r["fit"]).capitalize() if r["fit"] else "not scored",
                source=str(r["source"]),
            )
        )
    only_ei = int(conn.execute(_ONLY_EI).fetchone()[0])
    only_search = int(conn.execute(_ONLY_SEARCH, (claim,)).fetchone()[0])
    return Report(claim, tuple(rows), voided, only_ei, only_search)


def render(report: Report, now: dt.datetime) -> str:
    stamp = now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"careerhunter report: claim {report.claim}, written {stamp}",
        f"applications in both layers: {len(report.rows)} "
        f"({report.voided} voided in the EI record, left out)",
        f"only in the EI record: {report.only_ei}; only in search.db: {report.only_search}"
        + (
            "; run careerhunter doctor for the details"
            if report.only_ei or report.only_search
            else ""
        ),
        "",
        "by lane",
    ]
    lanes: dict[str, dict[str, int]] = {}
    for row in report.rows:
        statuses = lanes.setdefault(row.lane, {})
        statuses[row.ei_status] = statuses.get(row.ei_status, 0) + 1
    for lane in sorted(lanes):
        statuses = lanes[lane]
        detail = ", ".join(f"{status} {count}" for status, count in sorted(statuses.items()))
        lines.append(f"  {lane}: {sum(statuses.values())} ({detail})")
    if not lanes:
        lines.append("  none")
    lines += ["", "applications"]
    for row in report.rows:
        lines.append(
            f"  {row.when}  {row.employer} — {row.role}  | {row.method} | EI status: "
            f"{row.ei_status} | lane: {row.lane} | fit: {row.fit} | source: {row.source}"
        )
    if not report.rows:
        lines.append("  none")
    return "\n".join(lines) + "\n"


def write(root: Path, claim: str, text: str, now: dt.datetime) -> Path:
    """Write reports/report-<claim>-<utc>.txt mode 0600. Never replaces a file."""
    folder = root / FOLDER
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    stamp = now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"report-{claim}-{stamp}.txt"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path


def _when(precision: object, day: object, week: object) -> str:
    if precision == "exact" and day:
        return str(day)
    if precision == "week" and week:
        return f"week of {week}"
    return "date unknown"


def _lane(lane: object, checked: object) -> str:
    if lane:
        return str(lane)
    return "Off-lane" if checked else "No lane"
