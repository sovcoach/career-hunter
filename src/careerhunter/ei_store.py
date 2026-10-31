"""Reads and writes on one EI record file, ei_record_<label>.db.

This module knows only the EI record. It never imports search-layer code, and never
opens or attaches another database: everything it writes arrives as an EiApplication or
an Activity built by the caller, carrying only the fields the EI record holds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass, fields

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
