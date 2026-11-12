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

A job description (ADR-024) goes the same way. Its snapshot file is saved before the
call, like the ADR-017 job-ad copy; the EI record gets its `job_descriptions` row, and
search.db the text's hash and the file's path. Every application recorded from now on
has `jd_expected = 1` in both layers, with or without a JD. A JD file also becomes the
job-ad snapshot when there is none yet.

A skip is a search-only decision: it never opens an EI file. A discovery
decision is search-only too: it approves or rejects one queued company and
does not open an EI file.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from careerhunter import discover, ei_store, enums, search_store, urls
from careerhunter.claims import ActiveClaim, ClaimError, Verdict, check_dating, read_file_claim
from careerhunter.db import transaction
from careerhunter.ei_store import EiApplication, EiJobDescription
from careerhunter.jobdesc import SavedJd
from careerhunter.models import Activity, Application, Dating, Posting


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
        jd_expected=1,
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


def ei_job_description(jd: SavedJd) -> EiJobDescription:
    """The EI record's copy of a saved JD. Every field is named on purpose."""
    description = jd.description
    sha = description.text_sha256
    if sha is None:
        raise ValueError("a JD with no text has no job_descriptions row")
    return EiJobDescription(
        text=description.text,
        text_sha256=sha,
        source=description.source,
        file_path=jd.path,
        file_sha256=description.file_sha256,
        file_type=description.file_type,
    )


def record_application(
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    app: Application,
    jd: SavedJd | None = None,
) -> Recorded:
    """Record an application in both layers (search.db only when no claim is active).

    `jd` is a JD whose file is already saved. Its text, when it has any, is recorded in
    both layers; its file becomes the job-ad snapshot when the posting has none.
    """
    warning = ""
    if active is not None:
        verdict, warning = check_dating(active.claim, app.dating)
        if verdict is Verdict.REFUSE:
            raise ClaimError(warning)
    if jd is not None and app.posting.snapshot_path is None:
        app = replace(app, posting=replace(app.posting, snapshot_path=jd.path))
    label = active.claim.label if active else None
    correlation_id = new_correlation_id()
    status_date = app.dating.sort_date
    with ei_first(search, active):
        _, posting_id, _, created = search_store.find_or_create_posting(
            search, app.posting, app.dating
        )
        if jd is not None and not created:
            search_store.fill_posting_snapshot(search, posting_id, jd.path)
        app_id = search_store.insert_application(search, posting_id, app, correlation_id, label)
        search_store.add_decision(search, app_id, "applied", status_date)
        if jd is not None and jd.has_text:
            search_store.set_application_jd(search, app_id, jd.description.text_sha256, jd.path)
        if active is not None:
            ei_id = ei_store.insert_application(active.conn, sanitize(app, correlation_id))
            ei_store.add_status(active.conn, ei_id, "applied", status_date)
            if jd is not None and jd.has_text:
                ei_store.insert_job_description(active.conn, ei_id, ei_job_description(jd))
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


def record_discovery_decision(
    search: sqlite3.Connection, candidate_id: int, decision: str
) -> discover.Queued:
    """Approve or reject one queued company. Opens no EI file."""
    if decision == "approve":
        return discover.approve(search, candidate_id)
    if decision == "reject":
        return discover.reject(search, candidate_id)
    raise ValueError(f"unknown discovery decision {decision!r}")


def record_skip(search: sqlite3.Connection, posting: Posting, date: str, reason: str) -> int:
    """The operator's skip. Search-only: the EI record never hears about it.

    It is marked as the operator's (search migration 0016), so a repost of the same role
    is not alerted again (`already_handled`).
    """
    with transaction(search):
        _, posting_id, _, _ = search_store.find_or_create_posting(
            search, posting, Dating("exact", date)
        )
        return search_store.insert_skip(search, posting_id, date, reason, by_operator=True)


def record_activity(active: ActiveClaim | None, activity: Activity) -> bool:
    """Record a job-search activity in the active claim's EI record (its only home).

    Returns False if the same activity (type, description, date) was already there.
    """
    if active is None:
        raise ClaimError("no EI claim is active, so there is no record to add an activity to")
    verdict, message = check_dating(active.claim, activity.dating)
    if verdict is Verdict.REFUSE:
        raise ClaimError(message)
    with transaction(active.conn):
        return ei_store.insert_activity(active.conn, activity)


def attach_snapshot(
    search: sqlite3.Connection, active: ActiveClaim | None, correlation_id: str, path: str
) -> bool:
    """Record a job-ad snapshot taken after the application. Returns True if the EI
    record got it too.

    Both layers are updated, EI first (ADR-014); in the EI record it is an audited
    update. An application whose claim is sealed gets it in search.db only.
    """
    row = search.execute(
        "SELECT posting_id, ei_claim FROM applications WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no application with correlation id {correlation_id}")
    to_ei = active if active is not None and row["ei_claim"] == active.claim.label else None
    with ei_first(search, to_ei):
        search_store.set_posting_snapshot(search, int(row["posting_id"]), path)
        if to_ei is not None:
            to_ei.conn.execute(
                "UPDATE applications SET snapshot_path = ? WHERE correlation_id = ?",
                (path, correlation_id),
            )
    return to_ei is not None


class AttachRefused(Exception):
    """A later attach was refused, and nothing was written. The message is user-facing."""


@dataclass(frozen=True)
class Attached:
    to_ei: bool  # the EI record got it too
    jd_added: bool  # a new current JD (False when it was the current one already)
    url_set: bool
    snapshot_set: bool


def _changeable(
    search: sqlite3.Connection, active: ActiveClaim | None, correlation_id: str
) -> tuple[search_store.JdReference, ActiveClaim | None, sqlite3.Row | None]:
    """The application's search fields, the claim whose record gets the change (None
    for an application in search.db only), and its EI row. Refused when its record
    can't change: another claim's, a sealed one, or a voided application."""
    ref = search_store.jd_reference(search, correlation_id)
    if ref is None:
        raise ValueError(f"no application with correlation id {correlation_id}")
    to_ei: ActiveClaim | None = None
    if ref.ei_claim is not None:
        if active is None or active.claim.label != ref.ei_claim:
            raise AttachRefused(
                f"That application is in the record for claim {ref.ei_claim}, which is not "
                "the active claim, so its record can't change."
            )
        file_claim = read_file_claim(active.conn)
        if file_claim is not None and file_claim.sealed_at is not None:
            raise AttachRefused(
                f"The record for claim {ref.ei_claim} is sealed, so it can't change."
            )
        to_ei = active
    ei_row = None
    if to_ei is not None:
        ei_row = ei_store.application_fields(to_ei.conn, correlation_id)
        if ei_row is None:
            raise ClaimError(
                f"application {correlation_id} is missing from the EI record for claim "
                f"{to_ei.claim.label}; run `careerhunter doctor`"
            )
        if int(ei_row["voided"]):
            raise AttachRefused("That application was voided, so it can't change.")
    return ref, to_ei, ei_row


def mark_jd_unavailable(
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    correlation_id: str,
    explanation: str,
) -> bool:
    """Mark an application's JD unavailable, with why, EI first (ADR-024, amended).

    `explanation` is already checked (`jobdesc.unavailable_explanation`). The EI record
    gets a new `jd_unavailable` row and search.db the same explanation. Returns False,
    writing nothing, when the current explanation is already this one. Refused, writing
    nothing, when the application has a JD, or when its record can't change. A JD
    attached later is recorded as usual and shown in the marker's place.
    """
    ref, to_ei, ei_row = _changeable(search, active, correlation_id)
    current = ei_store.current_job_description(to_ei.conn, correlation_id) if to_ei else None
    if ref.jd_sha256 is not None or current is not None:
        raise AttachRefused("That application already has a job description.")
    if ref.jd_unavailable == explanation:
        return False
    if to_ei is not None and not ei_store.has_jd_unavailable(to_ei.conn):
        raise ClaimError(
            f"the EI record for claim {to_ei.claim.label} is behind this code; "
            "run: careerhunter migrate"
        )
    with ei_first(search, to_ei):
        search_store.set_jd_unavailable(search, ref.application_id, explanation)
        if to_ei is not None and ei_row is not None:
            ei_store.insert_jd_unavailable(to_ei.conn, int(ei_row["id"]), explanation)
    return True


def _url_owner(search: sqlite3.Connection, url: str, posting_id: int) -> bool:
    """True when another posting already has this URL, compared canonically."""
    return any(
        other != posting_id and urls.canonical(stored) == url
        for other, stored in search_store.posting_urls(search)
    )


def attach_job_description(
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    correlation_id: str,
    jd: SavedJd | None,
    url: str | None = None,
) -> Attached:
    """Add a JD to an application recorded earlier, EI first (ADR-014).

    `jd` is None for a URL-only attach (the import fills an empty link that way).

    With `url`, an empty URL is filled with its canonical form: an audited EI update,
    and `postings.url` when that is empty. A URL that another posting has, or that
    differs from the one recorded, is refused. The JD file becomes the job-ad snapshot
    when the application has none. An application recorded under a claim that is not
    the active one, or whose claim is sealed, is refused: its record can't change.
    """
    ref, to_ei, ei_row = _changeable(search, active, correlation_id)
    new_url = urls.canonical(url) if url else None
    url_set = False
    if new_url is not None:
        recorded = [u for u in (ref.url, ei_row["posting_url"] if ei_row else None) if u]
        if any(urls.canonical(u) != new_url for u in recorded):
            raise AttachRefused("That application already has a different link.")
        if ref.url is None and _url_owner(search, new_url, ref.posting_id):
            raise AttachRefused("That link is already recorded for another posting.")
        url_set = ref.url is None or (ei_row is not None and ei_row["posting_url"] is None)
    sha = jd.description.text_sha256 if jd is not None else None
    current = ei_store.current_job_description(to_ei.conn, correlation_id) if to_ei else None
    current_sha = current.text_sha256 if current else ref.jd_sha256
    jd_added = jd is not None and jd.has_text and sha != current_sha
    snapshot_set = jd is not None and (
        ref.snapshot_path is None or (ei_row is not None and ei_row["snapshot_path"] is None)
    )
    try:
        with ei_first(search, to_ei):
            if new_url is not None and ref.url is None:
                search_store.set_posting_url(search, ref.posting_id, new_url)
            if jd is not None and ref.snapshot_path is None:
                search_store.set_posting_snapshot(search, ref.posting_id, jd.path)
            if jd is not None and jd_added:
                search_store.set_application_jd(search, ref.application_id, sha, jd.path)
            if to_ei is not None and ei_row is not None:
                if new_url is not None and ei_row["posting_url"] is None:
                    ei_store.set_posting_url(to_ei.conn, correlation_id, new_url)
                if jd is not None and ei_row["snapshot_path"] is None:
                    ei_store.set_snapshot_path(to_ei.conn, correlation_id, jd.path)
                if jd is not None and jd_added:
                    ei_store.insert_job_description(
                        to_ei.conn, int(ei_row["id"]), ei_job_description(jd)
                    )
    except sqlite3.IntegrityError:
        raise AttachRefused(
            "That job description is already recorded for this application."
        ) from None
    return Attached(to_ei is not None, jd_added, url_set, snapshot_set)


# --- Reconciliation -------------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    """One way the two layers disagree. `kind` is `orphan` when the application exists
    on one side only (or links to a claim with no file), `mismatch` when both sides have
    it but disagree. `doctor` reports both and fixes neither."""

    correlation_id: str
    problem: str
    kind: str = "mismatch"


def reconcile(search: sqlite3.Connection, records: dict[str, sqlite3.Connection]) -> list[Gap]:
    """Compare search.db with each claim's EI record, both ways. Reads only.

    `records` maps claim labels to connections on their EI files. Each connection is
    queried on its own: nothing is attached to anything.
    """
    gaps: list[Gap] = []
    links = search_store.claim_links(search)
    decisions = search_store.decision_sequences(search)
    references = search_store.jd_references(search)
    for label, conn in sorted(records.items()):
        where = f"the EI record for claim {label}"
        file_claim = read_file_claim(conn)
        sealed = file_claim is not None and file_claim.sealed_at is not None
        statuses = ei_store.status_sequences(conn)
        expected = ei_store.jd_expected(conn)
        current = ei_store.current_job_descriptions(conn)
        unavailable = ei_store.jd_unavailable(conn) if ei_store.has_jd_unavailable(conn) else None
        ei_urls = ei_store.posting_urls(conn)
        for corr in sorted(statuses):
            if corr not in links:
                gaps.append(Gap(corr, f"in {where} but missing from search.db", "orphan"))
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
            gaps.extend(_jd_gaps(corr, label, expected, current, ei_urls, references))
            if unavailable is not None and unavailable.get(corr) != references[corr].jd_unavailable:
                gaps.append(
                    Gap(
                        corr,
                        f"the JD-unavailable marker differs between claim {label} and search.db",
                    )
                )
        for corr, claim_label in sorted(links.items()):
            if claim_label == label and corr not in statuses:
                gaps.append(Gap(corr, f"in search.db but missing from {where}", "orphan"))
    for corr, linked in sorted(links.items()):
        if linked is not None and linked not in records:
            gaps.append(
                Gap(
                    corr,
                    f"search.db links it to claim {linked}, which has no record file",
                    "orphan",
                )
            )
    return gaps


def _jd_gaps(
    corr: str,
    label: str,
    expected: dict[str, int],
    current: dict[str, EiJobDescription],
    ei_urls: dict[str, str | None],
    references: dict[str, search_store.JdReference],
) -> list[Gap]:
    """jd_expected, the current JD's hash and the canonical URL, compared (ADR-024)."""
    ref = references[corr]
    gaps: list[Gap] = []
    if expected.get(corr, 0) != ref.jd_expected:
        gaps.append(Gap(corr, f"jd_expected differs between claim {label} and search.db"))
    ei_sha = current[corr].text_sha256 if corr in current else None
    if ei_sha != ref.jd_sha256:
        gaps.append(Gap(corr, f"the current JD differs between claim {label} and search.db"))
    ei_url, search_url = ei_urls.get(corr), ref.url
    if (urls.canonical(ei_url) if ei_url else None) != (
        urls.canonical(search_url) if search_url else None
    ):
        gaps.append(Gap(corr, f"the posting link differs between claim {label} and search.db"))
    return gaps
