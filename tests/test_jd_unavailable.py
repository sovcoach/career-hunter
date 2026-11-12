"""A JD marked unavailable: append-only in the EI record, mirrored in search.db, and
replaced on display by a real JD attached later (ADR-024, amended). Synthetic data only."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import db, ei_store, jobdesc, ledger, search_store
from careerhunter.claims import ActiveClaim
from careerhunter.ledger import AttachRefused, attach_job_description, mark_jd_unavailable
from careerhunter.ledger import record_application as record
from tests.conftest import ACTIVE
from tests.test_ledger_jd import application, saved  # noqa: F401 (fixture)

GONE = "posting removed before capture"


def _reason(search: sqlite3.Connection, corr: str) -> str | None:
    row = search.execute(
        "SELECT jd_unavailable_reason FROM applications WHERE correlation_id = ?", (corr,)
    ).fetchone()
    return row[0]


def _markers(ei: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows = ei.execute("SELECT application_id, explanation FROM jd_unavailable ORDER BY id")
    return [tuple(r) for r in rows]


@pytest.mark.parametrize(
    ("given", "kept"),
    [(GONE, GONE), ("  posting   removed\nbefore capture ", GONE), ("x" * 200, "x" * 200)],
)
def test_an_explanation_is_trimmed_text(given: str, kept: str) -> None:
    assert jobdesc.unavailable_explanation(given) == kept


@pytest.mark.parametrize("given", ["", "   ", "x" * 201, "bad\x07bell", None, 7])
def test_another_explanation_is_refused(given: object) -> None:
    with pytest.raises(jobdesc.JobDescError, match="1 to 200 characters"):
        jobdesc.unavailable_explanation(given)


def test_marking_writes_both_layers_and_reconciles(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = record(search, active, application()).correlation_id
    assert mark_jd_unavailable(search, active, corr, GONE) is True
    assert _reason(search, corr) == GONE
    assert _markers(active.conn) == [(1, GONE)]
    assert ei_store.jd_unavailable(active.conn) == {corr: GONE}
    assert ledger.reconcile(search, {ACTIVE.label: active.conn}) == []


def test_the_same_explanation_again_writes_nothing_and_a_new_one_is_a_new_row(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = record(search, active, application()).correlation_id
    mark_jd_unavailable(search, active, corr, GONE)
    assert mark_jd_unavailable(search, active, corr, GONE) is False
    assert mark_jd_unavailable(search, active, corr, "employer closed the role") is True
    assert _markers(active.conn) == [(1, GONE), (1, "employer closed the role")]
    assert ei_store.jd_unavailable(active.conn) == {corr: "employer closed the role"}
    assert _reason(search, corr) == "employer closed the role"


def test_the_marker_rows_are_append_only(search: sqlite3.Connection, active: ActiveClaim) -> None:
    corr = record(search, active, application()).correlation_id
    mark_jd_unavailable(search, active, corr, GONE)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        active.conn.execute("UPDATE jd_unavailable SET explanation = 'changed'")
    with pytest.raises(sqlite3.IntegrityError, match="deletes are disabled"):
        active.conn.execute("DELETE FROM jd_unavailable")
    with pytest.raises(sqlite3.IntegrityError):
        active.conn.execute(
            "INSERT INTO jd_unavailable (application_id, explanation) VALUES (1, ' padded ')"
        )


def test_a_later_jd_attaches_normally_and_takes_the_markers_place(
    search: sqlite3.Connection,
    active: ActiveClaim,
    saved: Any,  # noqa: F811
) -> None:
    corr = record(search, active, application()).correlation_id
    mark_jd_unavailable(search, active, corr, GONE)
    assert search_store.jd_counts(search) == (0, 0)
    assert search_store.jd_unavailable_counts(search) == [(GONE, 1)]
    jd = saved(jobdesc.from_paste("Synthetic duties, found in an archive."))
    assert attach_job_description(search, active, corr, jd).jd_added
    assert search_store.jd_unavailable_counts(search) == []
    assert _markers(active.conn) == [(1, GONE)]  # kept in the record
    ref = search_store.jd_reference(search, corr)
    assert ref is not None
    shown = ei_store.jd_reference(ref.jd_sha256, "pasted", 1, ref.jd_unavailable)
    assert shown == f"JD {jd.description.text_sha256[:8]} (pasted)"
    assert ledger.reconcile(search, {ACTIVE.label: active.conn}) == []
    with pytest.raises(AttachRefused, match="already has a job description"):
        mark_jd_unavailable(search, active, corr, GONE)


def test_the_reference_shows_the_explanation_when_there_is_no_jd() -> None:
    assert ei_store.jd_reference(None, None, 1, GONE) == f"JD -- ({GONE})"
    assert ei_store.jd_reference(None, None, 0, GONE) == f"JD -- ({GONE})"
    assert ei_store.jd_reference(None, None, 1) == "JD missing"


def test_a_search_only_application_keeps_its_marker_in_search(
    search: sqlite3.Connection,
) -> None:
    corr = record(search, None, application()).correlation_id
    assert mark_jd_unavailable(search, None, corr, GONE) is True
    assert _reason(search, corr) == GONE


def test_a_voided_or_sealed_record_is_refused(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = record(search, active, application()).correlation_id
    active.conn.execute(
        "UPDATE applications SET voided = 1, void_reason = 'duplicate', voided_at = 'now'"
    )
    with pytest.raises(AttachRefused, match="voided"):
        mark_jd_unavailable(search, active, corr, GONE)
    assert _reason(search, corr) is None


def test_reconcile_compares_the_marker(search: sqlite3.Connection, active: ActiveClaim) -> None:
    corr = record(search, active, application()).correlation_id
    mark_jd_unavailable(search, active, corr, GONE)
    search.execute("UPDATE applications SET jd_unavailable_reason = 'something else'")
    assert ledger.reconcile(search, {ACTIVE.label: active.conn}) == [
        ledger.Gap(corr, "the JD-unavailable marker differs between claim 2026-27 and search.db")
    ]


def test_an_ei_file_at_0002_has_no_markers(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "ei.db")
    try:
        db.migrate(
            conn, "ei_record", [m for m in db.load_migrations("ei_record") if m.version <= 2]
        )
        assert not ei_store.has_jd_unavailable(conn)
        assert ei_store.jd_unavailable(conn) == {}
        db.migrate(conn, "ei_record")
        assert ei_store.has_jd_unavailable(conn)
    finally:
        conn.close()
