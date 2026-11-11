"""Job descriptions through the ledger: recorded with the application, attached later,
and reconciled between the layers (ADR-024). Synthetic data only."""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import sqlite3
from typing import Any

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from careerhunter import claims, ei_store, jobdesc, ledger, search_store
from careerhunter.claims import ActiveClaim, ClaimSettings
from careerhunter.ledger import AttachRefused, attach_job_description, reconcile
from careerhunter.ledger import record_application as record
from careerhunter.models import Application, Dating, Posting
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE, EARLIER

TODAY = dt.date(2026, 10, 28)
URL = "https://careers.example.com/jobs/analyst-1"


def application(day: str = "2026-09-15", *, url: str | None = URL, **posting: Any) -> Application:
    return Application(
        posting=Posting(
            company=posting.pop("company", "Example Robotics"),
            title=posting.pop("title", "Client Experience Analyst"),
            first_seen=day,
            url=url,
            source="self_sourced",
            **posting,
        ),
        dating=Dating("exact", day=day),
        method="company_site",
    )


@pytest.fixture
def saved(datadir: DataDir) -> Any:
    (datadir.root / "snapshots").mkdir(mode=0o700, exist_ok=True)

    def save(jd: jobdesc.JobDescription) -> jobdesc.SavedJd:
        return jobdesc.save(datadir.root, jd, TODAY)

    return save


def blank_pdf() -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def search_row(search: sqlite3.Connection, corr: str) -> sqlite3.Row:
    row: sqlite3.Row = search.execute(
        "SELECT a.jd_expected, a.jd_sha256, a.jd_path, p.url, p.snapshot_path FROM applications a "
        "JOIN postings p ON p.id = a.posting_id WHERE a.correlation_id = ?",
        (corr,),
    ).fetchone()
    return row


def ei_row(ei: sqlite3.Connection, corr: str) -> sqlite3.Row:
    row: sqlite3.Row = ei.execute(
        "SELECT id, jd_expected, posting_url, snapshot_path FROM applications "
        "WHERE correlation_id = ?",
        (corr,),
    ).fetchone()
    return row


def jd_rows(ei: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in ei.execute(
            "SELECT text, text_sha256, jd_source, file_path, file_type FROM job_descriptions "
            "ORDER BY id"
        )
    ]


# --- recording -------------------------------------------------------------------------


def test_a_pasted_jd_is_recorded_in_both_layers(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    jd = saved(jobdesc.from_paste("About the role\nSynthetic duties.\n"))
    corr = record(search, active, application(), jd).correlation_id
    sha = jd.description.text_sha256
    s, e = search_row(search, corr), ei_row(active.conn, corr)
    assert (s["jd_expected"], s["jd_sha256"], s["jd_path"]) == (1, sha, jd.path)
    assert s["snapshot_path"] == jd.path == e["snapshot_path"]  # the JD file is the snapshot
    assert e["jd_expected"] == 1
    assert jd_rows(active.conn) == [
        ("About the role\nSynthetic duties.\n", sha, "pasted", jd.path, "txt")
    ]
    file_sha = active.conn.execute("SELECT file_sha256 FROM job_descriptions").fetchone()[0]
    assert file_sha == jd.description.file_sha256
    assert reconcile(search, {ACTIVE.label: active.conn}) == []


def test_no_jd_is_recorded_as_expected(search: sqlite3.Connection, active: ActiveClaim) -> None:
    corr = record(search, active, application()).correlation_id
    assert tuple(search_row(search, corr))[:3] == (1, None, None)
    assert ei_row(active.conn, corr)["jd_expected"] == 1
    assert jd_rows(active.conn) == []
    assert reconcile(search, {ACTIVE.label: active.conn}) == []


def test_a_pdf_with_no_text_is_the_snapshot_but_not_a_jd(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    jd = saved(jobdesc.from_file(blank_pdf()))
    corr = record(search, active, application(), jd).correlation_id
    s = search_row(search, corr)
    assert (s["jd_expected"], s["jd_sha256"], s["jd_path"]) == (1, None, None)
    assert s["snapshot_path"] == jd.path == ei_row(active.conn, corr)["snapshot_path"]
    assert jd_rows(active.conn) == []
    with pytest.raises(ValueError):
        ledger.ei_job_description(jd)


def test_an_existing_snapshot_is_kept(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    jd = saved(jobdesc.from_paste("Synthetic duties."))
    app = application(snapshot_path="snapshots/2026/earlier.html")
    corr = record(search, active, app, jd).correlation_id
    assert search_row(search, corr)["snapshot_path"] == "snapshots/2026/earlier.html"
    assert ei_row(active.conn, corr)["snapshot_path"] == "snapshots/2026/earlier.html"


def test_a_stored_posting_without_a_snapshot_gets_the_jd_file(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    record(search, None, application("2026-09-10"))  # stores the posting, no snapshot
    jd = saved(jobdesc.from_paste("Synthetic duties."))
    corr = record(search, active, application(), jd).correlation_id
    assert search_row(search, corr)["snapshot_path"] == jd.path


def test_without_an_active_claim_search_keeps_the_jd(
    search: sqlite3.Connection, saved: Any
) -> None:
    jd = saved(jobdesc.from_paste("Synthetic duties."))
    corr = record(search, None, application(), jd).correlation_id
    assert search_row(search, corr)["jd_sha256"] == jd.description.text_sha256


# --- attaching later -------------------------------------------------------------------


def test_attach_adds_the_jd_and_fills_an_empty_link_canonically(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    corr = record(search, active, application(url=None)).correlation_id
    jd = saved(jobdesc.from_paste("Synthetic duties."))
    done = attach_job_description(search, active, corr, jd, URL + "?utm_source=x#top")
    assert done == ledger.Attached(to_ei=True, jd_added=True, url_set=True, snapshot_set=True)
    s, e = search_row(search, corr), ei_row(active.conn, corr)
    assert s["url"] == URL == e["posting_url"]
    assert s["jd_sha256"] == jd.description.text_sha256 and s["jd_path"] == jd.path
    assert s["snapshot_path"] == jd.path == e["snapshot_path"]
    edited = {r[0] for r in active.conn.execute("SELECT column_name FROM edits")}
    assert edited == {"posting_url", "snapshot_path"}  # audited updates
    assert len(jd_rows(active.conn)) == 1
    assert reconcile(search, {ACTIVE.label: active.conn}) == []


def test_attaching_the_same_jd_again_changes_nothing_and_a_correction_is_a_new_row(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    first = saved(jobdesc.from_paste("Synthetic duties."))
    corr = record(search, active, application(), first).correlation_id
    again = attach_job_description(
        search, active, corr, saved(jobdesc.from_paste("Synthetic duties."))
    )
    assert again == ledger.Attached(True, False, False, False)
    fixed = saved(jobdesc.from_paste("Synthetic duties, corrected."))
    assert attach_job_description(search, active, corr, fixed, URL).jd_added
    current = ei_store.current_job_description(active.conn, corr)
    assert current is not None and current.text == "Synthetic duties, corrected."
    assert search_row(search, corr)["jd_sha256"] == fixed.description.text_sha256
    assert search_row(search, corr)["snapshot_path"] == first.path  # kept
    assert len(jd_rows(active.conn)) == 2
    with pytest.raises(AttachRefused, match="already recorded"):  # back to an older JD
        attach_job_description(search, active, corr, saved(jobdesc.from_paste("Synthetic duties.")))
    assert reconcile(search, {ACTIVE.label: active.conn}) == []


def test_attach_for_a_search_only_application(search: sqlite3.Connection, saved: Any) -> None:
    corr = record(search, None, application()).correlation_id
    jd = saved(jobdesc.from_paste("Synthetic duties."))
    assert not attach_job_description(search, None, corr, jd).to_ei
    assert search_row(search, corr)["jd_sha256"] == jd.description.text_sha256


def _unchanged(search: sqlite3.Connection, active: ActiveClaim) -> tuple[Any, ...]:
    return (
        [tuple(r) for r in search.execute("SELECT * FROM applications")],
        [tuple(r) for r in search.execute("SELECT url, snapshot_path FROM postings")],
        [tuple(r) for r in active.conn.execute("SELECT * FROM applications")],
        jd_rows(active.conn),
    )


def test_a_different_link_is_refused(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    corr = record(search, active, application()).correlation_id
    before = _unchanged(search, active)
    with pytest.raises(AttachRefused, match="different link"):
        attach_job_description(
            search, active, corr, saved(jobdesc.from_paste("x")), "https://other.example.com/1"
        )
    assert _unchanged(search, active) == before


def test_a_link_another_posting_has_is_refused(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    record(search, active, application("2026-09-10", title="Other Role"))  # owns URL
    corr = record(search, active, application(url=None)).correlation_id
    before = _unchanged(search, active)
    with pytest.raises(AttachRefused, match="another posting"):
        attach_job_description(
            search, active, corr, saved(jobdesc.from_paste("x")), URL + "?trk=abc"
        )
    assert _unchanged(search, active) == before


def test_a_voided_application_is_refused(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    corr = record(search, active, application()).correlation_id
    active.conn.execute(
        "UPDATE applications SET voided = 1, void_reason = 'duplicate', voided_at = 'now'"
    )
    with pytest.raises(AttachRefused, match="voided"):
        attach_job_description(search, active, corr, saved(jobdesc.from_paste("x")))


def test_an_application_of_another_claim_is_refused(
    datadir: DataDir, search: sqlite3.Connection, saved: Any
) -> None:
    with claims.active_claim(datadir, ClaimSettings(EARLIER, (EARLIER,))) as earlier:
        assert earlier is not None
        corr = record(search, earlier, application("2026-01-15")).correlation_id
    both = ClaimSettings(ACTIVE, (EARLIER, ACTIVE))
    claims.seal(claims.plan_seal(datadir, both, "2025-26", dt.date(2026, 10, 31)))
    with (
        claims.active_claim(datadir, both) as active,
        pytest.raises(AttachRefused, match="not the active claim"),
    ):
        attach_job_description(search, active, corr, saved(jobdesc.from_paste("x")))
    with pytest.raises(AttachRefused, match="not the active claim"):
        attach_job_description(search, None, corr, saved(jobdesc.from_paste("x")))
    assert search_row(search, corr)["jd_sha256"] is None


def test_a_sealed_active_record_is_refused(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    corr = record(search, active, application()).correlation_id
    active.conn.execute(
        "UPDATE claim SET end_date = '2027-06-30', sealed_at = '2027-07-01T00:00:00Z'"
    )
    with pytest.raises(AttachRefused, match="sealed"):
        attach_job_description(search, active, corr, saved(jobdesc.from_paste("x")))


def test_attach_needs_a_known_application(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    with pytest.raises(ValueError):
        attach_job_description(search, active, "f" * 32, saved(jobdesc.from_paste("x")))
    corr = record(search, None, application()).correlation_id
    search_store.link_to_claim(
        search, int(search.execute("SELECT max(id) FROM applications").fetchone()[0]), ACTIVE.label
    )
    with pytest.raises(claims.ClaimError, match="missing from the EI record"):
        attach_job_description(search, active, corr, saved(jobdesc.from_paste("x")))


# --- reconciling -----------------------------------------------------------------------


def test_reconcile_compares_the_current_jd(
    search: sqlite3.Connection, active: ActiveClaim, saved: Any
) -> None:
    jd = saved(jobdesc.from_paste("Synthetic duties."))
    corr = record(search, active, application(), jd).correlation_id
    search.execute("UPDATE applications SET jd_sha256 = ?", ("0" * 64,))
    assert reconcile(search, {ACTIVE.label: active.conn}) == [
        ledger.Gap(corr, "the current JD differs between claim 2026-27 and search.db")
    ]


def test_reconcile_compares_jd_expected(search: sqlite3.Connection, active: ActiveClaim) -> None:
    corr = record(search, None, application()).correlation_id
    search_store.link_to_claim(search, 1, ACTIVE.label)
    ei_app = dataclasses.replace(ledger.sanitize(application(), corr), jd_expected=0)
    ei_id = ei_store.insert_application(active.conn, ei_app)
    ei_store.add_status(active.conn, ei_id, "applied", "2026-09-15")
    assert reconcile(search, {ACTIVE.label: active.conn}) == [
        ledger.Gap(corr, "jd_expected differs between claim 2026-27 and search.db")
    ]


def test_reconcile_compares_links_canonically(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = record(search, active, application()).correlation_id
    search.execute("UPDATE postings SET url = ?", (URL + "?utm_source=mail",))
    assert reconcile(search, {ACTIVE.label: active.conn}) == []
    search.execute("UPDATE postings SET url = 'https://other.example.com/2'")
    assert reconcile(search, {ACTIVE.label: active.conn}) == [
        ledger.Gap(corr, "the posting link differs between claim 2026-27 and search.db")
    ]
