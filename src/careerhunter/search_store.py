"""Reads and writes on search.db, the search and learning layer."""

from __future__ import annotations

import sqlite3

from careerhunter.models import Application, Dating, Posting


class IdentityConflict(Exception):
    """A new record contradicts one already in search.db. The message is user-facing."""


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    *,
    ats_type: str | None = None,
    ats_slug: str | None = None,
    careers_url: str | None = None,
    watchlist: int = 0,
    source: str = "manual",
) -> tuple[int, bool]:
    """The company's id, inserting it if new. Returns (id, inserted)."""
    cur = conn.execute(
        "INSERT INTO companies (name, ats_type, ats_slug, careers_url, watchlist, source) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (name, ats_type, ats_slug, careers_url, watchlist, source),
    )
    found = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    return int(found["id"]), cur.rowcount == 1


def find_posting(
    conn: sqlite3.Connection, company_id: int, posting: Posting, dating: Dating
) -> int | None:
    """Match an existing posting using the schema's identity rules, strongest first."""
    if posting.url:
        row = conn.execute(
            "SELECT id, company_id FROM postings WHERE url = ?", (posting.url,)
        ).fetchone()
        if row:
            if row["company_id"] != company_id:
                raise IdentityConflict(
                    f"{posting.company} / {posting.title}: url {posting.url!r} is already "
                    "recorded for a posting at a different company"
                )
            return int(row["id"])
    if posting.requisition_id:
        row = conn.execute(
            "SELECT id FROM postings WHERE company_id = ? AND requisition_id = ?",
            (company_id, posting.requisition_id),
        ).fetchone()
        if row:
            return int(row["id"])
    if posting.url or posting.requisition_id:
        return None
    fallback = (
        "SELECT p.id FROM postings p {join} WHERE p.company_id = ? AND p.title = ? "
        "AND p.location = ? AND p.url IS NULL AND p.requisition_id IS NULL {extra} LIMIT 1"
    )
    key = (company_id, posting.title, posting.location)
    row = conn.execute(
        fallback.format(join="", extra="AND substr(p.first_seen, 1, 7) = substr(?, 1, 7)"),
        (*key, posting.first_seen),
    ).fetchone()
    if row is None and dating.precision == "unknown":
        # An undated entry's first_seen is the import date, which moves between runs.
        # Match the earlier import of the same undated application instead.
        row = conn.execute(
            fallback.format(
                join="JOIN applications a ON a.posting_id = p.id",
                extra="AND a.date_precision = 'unknown'",
            ),
            key,
        ).fetchone()
    return int(row["id"]) if row else None


def create_posting(conn: sqlite3.Connection, company_id: int, posting: Posting) -> int:
    cur = conn.execute(
        "INSERT INTO postings (company_id, title, location, work_model, schedule, url, "
        "requisition_id, first_seen, source, fit_rating, gaps, llm_output, scout_metadata, "
        "snapshot_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            company_id,
            posting.title,
            posting.location,
            posting.work_model,
            posting.schedule,
            posting.url,
            posting.requisition_id,
            posting.first_seen,
            posting.source,
            posting.fit_rating,
            posting.gaps,
            posting.llm_output,
            posting.scout_metadata,
            posting.snapshot_path,
        ),
    )
    return int(cur.lastrowid or 0)


def find_or_create_posting(
    conn: sqlite3.Connection, posting: Posting, dating: Dating
) -> tuple[int, int, bool, bool]:
    """Returns (company_id, posting_id, company inserted, posting inserted)."""
    company_id, company_new = upsert_company(conn, posting.company, source=posting.source)
    posting_id = find_posting(conn, company_id, posting, dating)
    if posting_id is not None:
        return company_id, posting_id, company_new, False
    return company_id, create_posting(conn, company_id, posting), company_new, True


def find_application(
    conn: sqlite3.Connection, posting_id: int, dating: Dating
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, correlation_id, ei_claim FROM applications WHERE posting_id = ? "
        "AND date_precision = ? AND coalesce(date_applied, '') = coalesce(?, '') "
        "AND coalesce(week_of, '') = coalesce(?, '')",
        (posting_id, dating.precision, dating.day, dating.week_of),
    ).fetchone()
    return row


def application_by_correlation(conn: sqlite3.Connection, correlation_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, correlation_id, ei_claim FROM applications WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    return row


def insert_application(
    conn: sqlite3.Connection,
    posting_id: int,
    app: Application,
    correlation_id: str,
    ei_claim: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO applications (correlation_id, ei_claim, posting_id, date_applied, "
        "date_precision, week_of, method, resume_file, reasoning, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            correlation_id,
            ei_claim,
            posting_id,
            app.dating.day,
            app.dating.precision,
            app.dating.week_of,
            app.method,
            app.resume_file,
            app.reasoning,
            app.notes,
        ),
    )
    return int(cur.lastrowid or 0)


def link_to_claim(conn: sqlite3.Connection, application_id: int, label: str) -> None:
    conn.execute("UPDATE applications SET ei_claim = ? WHERE id = ?", (label, application_id))


def add_decision(
    conn: sqlite3.Connection,
    application_id: int,
    status: str,
    status_date: str | None,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO decision_history (application_id, status, status_date, note) "
        "VALUES (?, ?, ?, ?)",
        (application_id, status, status_date, note),
    )


def insert_skip(conn: sqlite3.Connection, posting_id: int, date: str, reason: str) -> int:
    cur = conn.execute(
        "INSERT INTO skips (posting_id, date, reason) VALUES (?, ?, ?)",
        (posting_id, date, reason),
    )
    return int(cur.lastrowid or 0)


def claim_links(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Every application's correlation id and the claim it was recorded under."""
    return {
        r["correlation_id"]: r["ei_claim"]
        for r in conn.execute("SELECT correlation_id, ei_claim FROM applications")
    }


def decision_sequences(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str | None]]]:
    sequences: dict[str, list[tuple[str, str | None]]] = {c: [] for c in claim_links(conn)}
    for r in conn.execute(
        "SELECT a.correlation_id, d.status, d.status_date FROM decision_history d "
        "JOIN applications a ON a.id = d.application_id ORDER BY d.id"
    ):
        sequences[r["correlation_id"]].append((r["status"], r["status_date"]))
    return sequences
