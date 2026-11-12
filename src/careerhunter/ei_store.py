"""Reads and writes on one EI record file, ei_record_<label>.db.

This module knows only the EI record. It never imports search-layer code, and never
opens or attaches another database: everything it writes arrives as an EiApplication or
an Activity built by the caller, carrying only the fields the EI record holds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass, field, fields

from careerhunter.models import Activity, Dating


@dataclass(frozen=True)
class EiApplication:
    """A sanitized application: exactly the columns the EI record stores, nothing else."""

    correlation_id: str
    employer: str
    role: str
    location: str
    work_model: str | None
    posting_url: str | None
    date_precision: str
    date_applied: str | None
    week_of: str | None
    method: str
    snapshot_path: str | None
    resume_file: str | None
    jd_expected: int  # 1 for every application recorded since EI migration 0002


@dataclass(frozen=True)
class EiJobDescription:
    """One job description as the EI record stores it: the text verbatim, its hash,
    its source, and its snapshot file (relative to the data folder)."""

    text: str = field(repr=False)
    text_sha256: str
    source: str
    file_path: str | None
    file_sha256: str | None
    file_type: str | None


EI_APPLICATION_FIELDS = tuple(f.name for f in fields(EiApplication))
# Built from the dataclass, never from input.
_COLUMNS = ", ".join(EI_APPLICATION_FIELDS)
_PLACEHOLDERS = ", ".join("?" for _ in EI_APPLICATION_FIELDS)
_INSERT_APPLICATION = f"INSERT INTO applications ({_COLUMNS}) VALUES ({_PLACEHOLDERS})"  # noqa: S608


def find_application(
    conn: sqlite3.Connection,
    employer: str,
    role: str,
    location: str,
    posting_url: str | None,
    dating: Dating,
) -> str | None:
    """The correlation id of the application with this natural key, if it is recorded."""
    row = conn.execute(
        "SELECT correlation_id FROM applications WHERE employer = ? AND role = ? "
        "AND location = ? AND coalesce(posting_url, '') = coalesce(?, '') "
        "AND date_precision = ? AND coalesce(date_applied, '') = coalesce(?, '') "
        "AND coalesce(week_of, '') = coalesce(?, '')",
        (employer, role, location, posting_url, dating.precision, dating.day, dating.week_of),
    ).fetchone()
    return str(row["correlation_id"]) if row else None


def insert_application(conn: sqlite3.Connection, app: EiApplication) -> int:
    cur = conn.execute(_INSERT_APPLICATION, astuple(app))
    return int(cur.lastrowid or 0)


def application_id(conn: sqlite3.Connection, correlation_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM applications WHERE correlation_id = ?", (correlation_id,)
    ).fetchone()
    return int(row["id"]) if row else None


def application_fields(conn: sqlite3.Connection, correlation_id: str) -> sqlite3.Row | None:
    """id, posting_url, snapshot_path and voided of one application, or None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, posting_url, snapshot_path, voided FROM applications WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    return row


def set_posting_url(conn: sqlite3.Connection, correlation_id: str, url: str) -> None:
    """Fill an empty posting URL. An audited update (the edits trigger records it)."""
    conn.execute(
        "UPDATE applications SET posting_url = ? WHERE correlation_id = ? AND posting_url IS NULL",
        (url, correlation_id),
    )


def set_snapshot_path(conn: sqlite3.Connection, correlation_id: str, path: str) -> None:
    """Fill an empty snapshot path. An audited update."""
    conn.execute(
        "UPDATE applications SET snapshot_path = ? WHERE correlation_id = ? "
        "AND snapshot_path IS NULL",
        (path, correlation_id),
    )


def add_status(
    conn: sqlite3.Connection, application_id: int, status: str, status_date: str | None
) -> None:
    conn.execute(
        "INSERT INTO status_history (application_id, status, status_date) VALUES (?, ?, ?)",
        (application_id, status, status_date),
    )


def insert_activity(conn: sqlite3.Connection, act: Activity) -> bool:
    """Record an activity unless it is already there. True if it was new."""
    cur = conn.execute(
        "INSERT INTO activities (date, date_precision, week_of, type, description, "
        "related_company) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            act.dating.day,
            act.dating.precision,
            act.dating.week_of,
            act.type,
            act.description,
            act.related_company,
        ),
    )
    return cur.rowcount == 1


def status_sequences(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str | None]]]:
    """Every application's status history in order, keyed by correlation id."""
    sequences: dict[str, list[tuple[str, str | None]]] = {
        r["correlation_id"]: [] for r in conn.execute("SELECT correlation_id FROM applications")
    }
    for r in conn.execute(
        "SELECT a.correlation_id, s.status, s.status_date FROM status_history s "
        "JOIN applications a ON a.id = s.application_id ORDER BY s.id"
    ):
        sequences[r["correlation_id"]].append((r["status"], r["status_date"]))
    return sequences


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def has_job_descriptions(conn: sqlite3.Connection) -> bool:
    """False for a file still at EI migration 0001 (a sealed claim's, say)."""
    return _has_table(conn, "job_descriptions")


def insert_job_description(
    conn: sqlite3.Connection, application_id: int, jd: EiJobDescription
) -> int:
    cur = conn.execute(
        "INSERT INTO job_descriptions (application_id, text, text_sha256, jd_source, file_path, "
        "file_sha256, file_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            application_id,
            jd.text,
            jd.text_sha256,
            jd.source,
            jd.file_path,
            jd.file_sha256,
            jd.file_type,
        ),
    )
    return int(cur.lastrowid or 0)


def _jd(row: sqlite3.Row) -> EiJobDescription:
    return EiJobDescription(
        text=str(row["text"]),
        text_sha256=str(row["text_sha256"]),
        source=str(row["jd_source"]),
        file_path=row["file_path"],
        file_sha256=row["file_sha256"],
        file_type=row["file_type"],
    )


_CURRENT_JDS = (
    "SELECT a.correlation_id, j.text, j.text_sha256, j.jd_source, j.file_path, j.file_sha256, "
    "j.file_type FROM job_descriptions j JOIN applications a ON a.id = j.application_id "
    "WHERE j.id = (SELECT max(id) FROM job_descriptions WHERE application_id = j.application_id)"
)


def has_jd_unavailable(conn: sqlite3.Connection) -> bool:
    """False for a file still at EI migration 0001 or 0002."""
    return _has_table(conn, "jd_unavailable")


def insert_jd_unavailable(conn: sqlite3.Connection, application_id: int, explanation: str) -> int:
    """Mark the application's JD unavailable, with why (EI migration 0003)."""
    cur = conn.execute(
        "INSERT INTO jd_unavailable (application_id, explanation) VALUES (?, ?)",
        (application_id, explanation),
    )
    return int(cur.lastrowid or 0)


_CURRENT_UNAVAILABLE = (
    "SELECT a.correlation_id, u.explanation FROM jd_unavailable u "
    "JOIN applications a ON a.id = u.application_id "
    "WHERE u.id = (SELECT max(id) FROM jd_unavailable WHERE application_id = u.application_id)"
)


def jd_unavailable(conn: sqlite3.Connection) -> dict[str, str]:
    """Each marked application's latest explanation, keyed by correlation id. A file
    still at 0001 or 0002 has none."""
    if not has_jd_unavailable(conn):
        return {}
    return {
        str(r["correlation_id"]): str(r["explanation"]) for r in conn.execute(_CURRENT_UNAVAILABLE)
    }


def current_job_description(
    conn: sqlite3.Connection, correlation_id: str
) -> EiJobDescription | None:
    """The application's current JD (its latest row), or None. A 0001 file has none."""
    if not has_job_descriptions(conn):
        return None
    row = conn.execute(_CURRENT_JDS + " AND a.correlation_id = ?", (correlation_id,)).fetchone()
    return _jd(row) if row else None


def current_job_descriptions(conn: sqlite3.Connection) -> dict[str, EiJobDescription]:
    """Every application's current JD, keyed by correlation id. A 0001 file has none."""
    if not has_job_descriptions(conn):
        return {}
    return {str(r["correlation_id"]): _jd(r) for r in conn.execute(_CURRENT_JDS)}


def jd_expected(conn: sqlite3.Connection) -> dict[str, int]:
    """Every application's jd_expected, keyed by correlation id; 0 on a 0001 file."""
    has_column = any(
        r["name"] == "jd_expected" for r in conn.execute("PRAGMA table_info(applications)")
    )
    if not has_column:
        return {
            str(r["correlation_id"]): 0
            for r in conn.execute("SELECT correlation_id FROM applications")
        }
    return {
        str(r["correlation_id"]): int(r["jd_expected"])
        for r in conn.execute("SELECT correlation_id, jd_expected FROM applications")
    }


def posting_urls(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Every application's posting URL, as stored, keyed by correlation id."""
    return {
        str(r["correlation_id"]): r["posting_url"]
        for r in conn.execute("SELECT correlation_id, posting_url FROM applications")
    }


def jd_reference(
    text_sha256: str | None,
    source: str | None,
    jd_expected: int,
    unavailable: str | None = None,
) -> str:
    """How a report or an export names an application's JD (ADR-024).

    Any JD shows its reference, `JD <first 8 of sha256> (<source>)`, even before the
    feature, and even when the JD was marked unavailable before it arrived. With none,
    `JD -- (<explanation>)` when it was marked unavailable (EI migration 0003), else
    `JD missing`, or `JD missing (pre-feature)` for an application recorded before a JD
    was expected.
    """
    if text_sha256:
        return f"JD {text_sha256[:8]} ({source})"
    if unavailable:
        return f"JD -- ({unavailable})"
    return "JD missing" if jd_expected else "JD missing (pre-feature)"
