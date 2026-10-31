"""Corrections to the record without deleting anything.

A mistaken application or activity is voided: it stays in the database with the reason
and time, drops out of exports, and is frozen from then on. The audit triggers in the
schema log the change in `edits`.
"""

from __future__ import annotations

import sqlite3

from careerhunter.db import transaction

VOIDABLE = ("applications", "activities")


class RecordError(Exception):
    pass


def void(conn: sqlite3.Connection, table: str, row_id: int, reason: str) -> None:
    if table not in VOIDABLE:
        raise RecordError(f"only {', '.join(VOIDABLE)} can be voided, not {table!r}")
    reason = reason.strip()
    if not reason:
        raise RecordError("a void needs a reason, so the record explains itself")
    with transaction(conn):
        row = conn.execute(
            f"SELECT voided FROM {table} WHERE id = ?",  # noqa: S608 - table is allowlisted
            (row_id,),
        ).fetchone()
        if row is None:
            raise RecordError(f"no {table[:-1]} with id {row_id}")
        if row["voided"]:
            raise RecordError(f"{table[:-1]} {row_id} is already voided")
        conn.execute(
            f"UPDATE {table} SET voided = 1, void_reason = ?, "  # noqa: S608
            "voided_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (reason, row_id),
        )


def void_application(conn: sqlite3.Connection, application_id: int, reason: str) -> None:
    void(conn, "applications", application_id, reason)


def void_activity(conn: sqlite3.Connection, activity_id: int, reason: str) -> None:
    void(conn, "activities", activity_id, reason)
