"""Recording decisions in both layers, and checking that the two layers agree.

An application goes to two files:

- the active claim's EI record gets a sanitized copy (an EiApplication) and its status
  history;
- search.db gets the full decision and its decision history.

The two rows share an opaque correlation id: 128 random bits, unrelated to the record's
content, so neither side can be derived from the other.

SQLite can't commit atomically across two WAL databases, so the order is fixed:

1. search.db's transaction is opened and its rows written, but not committed, so that a
   search-side problem (such as a url already used by another company) stops everything
   before the EI record is touched;
2. the EI record's transaction commits. It is the source of truth;
3. search.db commits.

If step 3 fails, the EI record still stands and PartialWriteError says so; `doctor`
reports the gap. `reconcile()` compares the two layers in both directions.

A skip is a search-only decision: it never opens an EI file.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from careerhunter import ei_store, enums, search_store
from careerhunter.claims import ActiveClaim, ClaimError, Verdict, check_dating, read_file_claim
from careerhunter.db import transaction
from careerhunter.ei_store import EiApplication
from careerhunter.models import Application, Dating, Posting


class PartialWriteError(Exception):
    """The EI record was written but search.db was not. The message is user-facing."""


def new_correlation_id() -> str:
    return secrets.token_hex(16)


def sanitize(app: Application, correlation_id: str) -> EiApplication:
    """The EI record's copy of an application. Every field is named on purpose."""
    return EiApplication(
        correlation_id=correlation_id,
        employer=app.posting.company,
        role=app.posting.title,
        location=app.posting.location,
        work_model=app.posting.work_model,
        posting_url=app.posting.url,
        date_precision=app.dating.precision,
        date_applied=app.dating.day,
        week_of=app.dating.week_of,
        method=app.method,
        snapshot_path=app.posting.snapshot_path,
        resume_file=app.resume_file,
    )


@contextmanager
def ei_first(search: sqlite3.Connection, active: ActiveClaim | None) -> Iterator[None]:
    """Open search.db's transaction around the caller's work; commit EI, then search.

    The caller writes search rows on `search` and EI rows on `active.conn` inside the
    block. The EI rows go in their own transaction, committed before search.db's.
    """
    ei_committed = False
    try:
        with transaction(search):
            if active is None:
                yield
            else:
                with transaction(active.conn):
                    yield
                ei_committed = True
    except BaseException as exc:
        if ei_committed:
            raise PartialWriteError(
                f"the EI record for claim {active.claim.label if active else '?'} was saved, "
                f"but writing search.db failed ({exc}). Run `careerhunter doctor` to see the "
                "gap"
            ) from exc
        raise


@dataclass(frozen=True)
class Recorded:
    correlation_id: str
    ei_claim: str | None
    warning: str = ""


def record_application(
    search: sqlite3.Connection, active: ActiveClaim | None, app: Application
) -> Recorded:
    """Record an application in both layers (search.db only when no claim is active)."""
    warning = ""
    if active is not None:
        verdict, warning = check_dating(active.claim, app.dating)
        if verdict is Verdict.REFUSE:
            raise ClaimError(warning)
    label = active.claim.label if active else None
    correlation_id = new_correlation_id()
    status_date = app.dating.sort_date
    with ei_first(search, active):
        _, posting_id, _, _ = search_store.find_or_create_posting(search, app.posting, app.dating)
        app_id = search_store.insert_application(search, posting_id, app, correlation_id, label)
        search_store.add_decision(search, app_id, "applied", status_date)
        if active is not None:
            ei_id = ei_store.insert_application(active.conn, sanitize(app, correlation_id))
            ei_store.add_status(active.conn, ei_id, "applied", status_date)
    return Recorded(correlation_id, label, warning)


def record_status(
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    correlation_id: str,
    status: str,
    status_date: str | None,
    note: str | None = None,
) -> bool:
    """Append a status to both histories. Returns True if the EI record got it too.

    An application recorded under an earlier claim gets the status in search.db only:
    that claim's record is sealed (write commands refuse to run until it is).
    """
    if status not in enums.STATUSES:
        raise ValueError(f"{status!r} is not one of {list(enums.STATUSES)}")
    row = search_store.application_by_correlation(search, correlation_id)
    if row is None:
        raise ValueError(f"no application with correlation id {correlation_id}")
    to_ei = active if active is not None and row["ei_claim"] == active.claim.label else None
    with ei_first(search, to_ei):
        search_store.add_decision(search, int(row["id"]), status, status_date, note)
        if to_ei is not None:
            ei_id = ei_store.application_id(to_ei.conn, correlation_id)
            if ei_id is None:
                raise ClaimError(
                    f"application {correlation_id} is missing from the EI record for claim "
                    f"{to_ei.claim.label}; run `careerhunter doctor`"
                )
            ei_store.add_status(to_ei.conn, ei_id, status, status_date)
    return to_ei is not None


def record_skip(search: sqlite3.Connection, posting: Posting, date: str, reason: str) -> int:
    """A skip is search-only: the EI record never hears about it."""
    with transaction(search):
        _, posting_id, _, _ = search_store.find_or_create_posting(
            search, posting, Dating("exact", date)
        )
        return search_store.insert_skip(search, posting_id, date, reason)


# --- Reconciliation -------------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    correlation_id: str
    problem: str


def reconcile(search: sqlite3.Connection, records: dict[str, sqlite3.Connection]) -> list[Gap]:
    """Compare search.db with each claim's EI record, both ways. Reads only.

    `records` maps claim labels to connections on their EI files. Each connection is
    queried on its own: nothing is attached to anything.
    """
    gaps: list[Gap] = []
    links = search_store.claim_links(search)
    decisions = search_store.decision_sequences(search)
    for label, conn in sorted(records.items()):
        where = f"the EI record for claim {label}"
        file_claim = read_file_claim(conn)
        sealed = file_claim is not None and file_claim.sealed_at is not None
        statuses = ei_store.status_sequences(conn)
        for corr in sorted(statuses):
            if corr not in links:
                gaps.append(Gap(corr, f"in {where} but missing from search.db"))
                continue
            if links[corr] != label:
                other = links[corr] or "no claim"
                gaps.append(Gap(corr, f"in {where} but search.db links it to {other}"))
                continue
            ei_seq, search_seq = statuses[corr], decisions[corr]
            # After a claim is sealed, later statuses are recorded in search.db only.
            agrees = search_seq[: len(ei_seq)] == ei_seq if sealed else search_seq == ei_seq
            if not agrees:
                gaps.append(
                    Gap(corr, f"status history differs between claim {label} and search.db")
                )
        for corr, claim_label in sorted(links.items()):
            if claim_label == label and corr not in statuses:
                gaps.append(Gap(corr, f"in search.db but missing from {where}"))
    for corr, linked in sorted(links.items()):
        if linked is not None and linked not in records:
            gaps.append(
                Gap(corr, f"search.db links it to claim {linked}, which has no record file")
            )
    return gaps
