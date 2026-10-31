"""The two-layer write path: EI first, sanitized, isolated, and reconciled both ways."""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import claims, db, ei_store, enums, ledger
from careerhunter.claims import ActiveClaim, ClaimError, ClaimSettings
from careerhunter.ledger import PartialWriteError, reconcile, record_application, record_status
from careerhunter.models import Application, Dating, Posting
from careerhunter.search_store import IdentityConflict
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE, EARLIER

# A sentinel for each learning-layer field. None of these may ever reach an EI file.
SENTINELS = {
    "gaps": "SENTINEL-gaps-7f3a",
    "llm_output": "SENTINEL-llm-output-7f3a",
    "scout_metadata": '{"SENTINEL-scout-metadata-7f3a": true}',
    "reasoning": "SENTINEL-reasoning-7f3a",
    "notes": "SENTINEL-notes-7f3a",
    "skip_reason": "SENTINEL-skip-reason-7f3a",
}
FIT_SENTINEL = 9  # fit_rating is a number, so it is checked cell by cell


def application(
    day: str = "2026-09-15", *, title: str = "Operations Coordinator", **posting: Any
) -> Application:
    return Application(
        posting=Posting(
            company=posting.pop("company", "Acme Widgets"),
            title=title,
            first_seen=day,
            location="Springfield, ON",
            work_model="hybrid",
            url=posting.pop("url", f"https://acme.example.com/jobs/{title.replace(' ', '-')}"),
            snapshot_path="snapshots/acme-1001.html",
            **posting,
        ),
        dating=Dating("exact", day=day),
        method="company_site",
        resume_file="resume-ops.pdf",
    )


def learning_application() -> Application:
    app = application(
        fit_rating=FIT_SENTINEL,
        gaps=SENTINELS["gaps"],
        llm_output=SENTINELS["llm_output"],
        scout_metadata=SENTINELS["scout_metadata"],
        source="watcher",
    )
    return dataclasses.replace(app, reasoning=SENTINELS["reasoning"], notes=SENTINELS["notes"])


def file_bytes(conn: sqlite3.Connection) -> bytes:
    """Everything on disk for this connection's database, WAL included."""
    path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    files = db.db_files(path)[:2]  # the -shm index changes on every read, so leave it out
    return b"".join(f.read_bytes() for f in files if f.exists())


def all_cells(conn: sqlite3.Connection) -> list[tuple[str, str, Any]]:
    cells = []
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608
            cells.extend((table, key, row[key]) for key in row.keys())  # noqa: SIM118
    return cells


def snapshot(conn: sqlite3.Connection) -> tuple[Any, ...]:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return (tuple(all_cells(conn)), file_bytes(conn))


class FailingCommit(sqlite3.Connection):
    """A connection whose COMMIT fails on demand, like a disk error at the worst moment."""

    fail = False

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # type: ignore[override]
        if self.fail and sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        return super().execute(sql, *args)


@pytest.fixture
def flaky_search(datadir: DataDir) -> Any:
    c = sqlite3.connect(datadir.search_db_path, factory=FailingCommit, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    db.migrate(c, "search")
    yield c
    c.close()


def ei_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT correlation_id FROM applications")}


def search_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT correlation_id FROM applications")}


# --- The happy path -----------------------------------------------------------------------


def test_application_lands_in_both_layers_under_one_id(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    recorded = record_application(search, active, application())
    assert re.fullmatch(r"[0-9a-f]{32}", recorded.correlation_id)
    assert recorded.ei_claim == "2026-27"

    ei_row = active.conn.execute("SELECT * FROM applications").fetchone()
    assert ei_row["correlation_id"] == recorded.correlation_id
    assert (ei_row["employer"], ei_row["role"], ei_row["date_applied"]) == (
        "Acme Widgets",
        "Operations Coordinator",
        "2026-09-15",
    )
    assert ei_row["snapshot_path"] == "snapshots/acme-1001.html"
    assert ei_row["resume_file"] == "resume-ops.pdf"
    s_row = search.execute("SELECT correlation_id, ei_claim FROM applications").fetchone()
    assert tuple(s_row) == (recorded.correlation_id, "2026-27")
    assert reconcile(search, {"2026-27": active.conn}) == []


def test_correlation_ids_carry_no_meaning(search: sqlite3.Connection, active: ActiveClaim) -> None:
    ids = {
        record_application(search, active, application(f"2026-09-{d:02d}")).correlation_id
        for d in range(10, 20)
    }
    assert len(ids) == 10
    assert ledger.new_correlation_id() != ledger.new_correlation_id()
    source = inspect.getsource(ledger.new_correlation_id)
    assert "secrets.token_hex(16)" in source  # random bits, never content, time or MAC


def test_the_full_status_pipeline_lands_in_both_layers(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = record_application(search, active, application()).correlation_id
    later = [s for s in enums.STATUSES if s != "applied"]
    for i, status in enumerate(later):
        assert record_status(search, active, corr, status, f"2026-10-{i + 1:02d}", note="x")
    expected = [("applied", "2026-09-15")] + [
        (s, f"2026-10-{i + 1:02d}") for i, s in enumerate(later)
    ]
    assert ei_store.status_sequences(active.conn)[corr] == expected
    history = search.execute("SELECT status, status_date FROM decision_history ORDER BY id")
    assert [tuple(r) for r in history] == expected
    assert reconcile(search, {"2026-27": active.conn}) == []


def test_status_input_is_checked(search: sqlite3.Connection, active: ActiveClaim) -> None:
    corr = record_application(search, active, application()).correlation_id
    with pytest.raises(ValueError, match="ghosted"):
        record_status(search, active, corr, "ghosted", None)
    with pytest.raises(ValueError, match="no application"):
        record_status(search, active, "0" * 32, "offer", None)


# --- No learning field can reach the EI file -----------------------------------------------


@pytest.mark.parametrize("field", [*SENTINELS, "fit_rating"])
def test_no_learning_field_reaches_the_ei_file(
    search: sqlite3.Connection, active: ActiveClaim, field: str
) -> None:
    app = learning_application()
    corr = record_application(search, active, app).correlation_id
    record_status(search, active, corr, "screening", "2026-09-20", note=SENTINELS["notes"])
    ledger.record_skip(search, app.posting, "2026-09-16", SENTINELS["skip_reason"])
    active.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    if field == "fit_rating":
        id_columns = {"id", "application_id", "row_id", "version"}
        assert [
            c
            for c in all_cells(active.conn)
            if c[2] in (FIT_SENTINEL, str(FIT_SENTINEL)) and c[1] not in id_columns
        ] == []
        assert search.execute("SELECT fit_rating FROM postings").fetchone()[0] == FIT_SENTINEL
        return
    sentinel = SENTINELS[field]
    assert not any(sentinel in str(cell) for _, _, cell in all_cells(active.conn))
    assert sentinel.encode() not in file_bytes(active.conn)
    # The sentinel did go somewhere, so this test can't pass by accident.
    assert any(sentinel in str(cell) for _, _, cell in all_cells(search))


def test_sanitize_names_only_ei_fields() -> None:
    ei_app = ledger.sanitize(learning_application(), "a" * 32)
    assert set(dataclasses.asdict(ei_app)) == set(ei_store.EI_APPLICATION_FIELDS)
    assert not any(str(v) in SENTINELS.values() for v in dataclasses.asdict(ei_app).values())


# --- The EI record never opens or attaches search.db --------------------------------------


def test_ei_connection_never_attaches_anything(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    statements: list[str] = []
    active.conn.set_trace_callback(statements.append)
    corr = record_application(search, active, application()).correlation_id
    record_status(search, active, corr, "offer", "2026-10-01")
    reconcile(search, {"2026-27": active.conn})
    active.conn.set_trace_callback(None)
    assert statements
    assert not [s for s in statements if "ATTACH" in s.upper() or "search.db" in s]
    assert [r["name"] for r in active.conn.execute("PRAGMA database_list")] == ["main"]


def test_ei_store_cannot_reach_the_search_layer() -> None:
    tree = ast.parse(inspect.getsource(ei_store))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert not [m for m in imported if m and ("search" in m or "ledger" in m)]
    docstring = ast.get_docstring(tree, clean=False)
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != docstring
    ]
    assert not [s for s in strings if "ATTACH" in s.upper() or "search" in s.lower()]
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "connect" not in calls  # it only ever uses connections it is handed


# --- Skips and the no-claim case ----------------------------------------------------------


def test_a_skip_writes_nothing_to_the_ei_file(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    record_application(search, active, application())
    before = snapshot(active.conn)
    changes = active.conn.total_changes
    ledger.record_skip(
        search, application("2026-09-16", title="Data Clerk").posting, "2026-09-16", "too far"
    )
    assert snapshot(active.conn) == before
    assert active.conn.total_changes == changes
    assert search.execute("SELECT reason FROM skips").fetchone()[0] == "too far"


def test_without_an_active_claim_only_search_is_written(
    datadir: DataDir, search: sqlite3.Connection
) -> None:
    recorded = record_application(search, None, application())
    assert recorded.ei_claim is None
    assert search.execute("SELECT ei_claim FROM applications").fetchone()[0] is None
    assert datadir.ei_record_files() == []


def test_an_application_outside_the_claim_is_refused(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    with pytest.raises(ClaimError, match="before claim 2026-27 started"):
        record_application(search, active, application("2026-07-01"))
    assert ei_ids(active.conn) == search_ids(search) == set()


# --- Failures between and before the two writes -------------------------------------------


def test_failure_after_the_ei_commit_leaves_the_ei_record_and_is_reported(
    flaky_search: Any, active: ActiveClaim
) -> None:
    flaky_search.fail = True
    with pytest.raises(PartialWriteError, match="EI record for claim 2026-27 was saved"):
        record_application(flaky_search, active, application())
    flaky_search.fail = False
    (corr,) = ei_ids(active.conn)
    assert search_ids(flaky_search) == set()
    assert reconcile(flaky_search, {"2026-27": active.conn}) == [
        ledger.Gap(corr, "in the EI record for claim 2026-27 but missing from search.db")
    ]


def test_failure_on_a_status_is_reported_as_a_differing_history(
    flaky_search: Any, active: ActiveClaim
) -> None:
    corr = record_application(flaky_search, active, application()).correlation_id
    flaky_search.fail = True
    with pytest.raises(PartialWriteError):
        record_status(flaky_search, active, corr, "interview_scheduled", "2026-10-02")
    flaky_search.fail = False
    assert reconcile(flaky_search, {"2026-27": active.conn}) == [
        ledger.Gap(corr, "status history differs between claim 2026-27 and search.db")
    ]


def test_a_failing_ei_write_leaves_search_untouched(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    record_application(search, active, application())
    before = search_ids(search)
    # Same posting, same day: the EI record's natural key refuses it.
    with pytest.raises(sqlite3.IntegrityError):
        record_application(search, active, application())
    assert search_ids(search) == before
    assert len(ei_ids(active.conn)) == 1


def test_a_search_side_problem_stops_before_the_ei_record(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    record_application(search, active, application(url="https://shared.example.com/1"))
    before = snapshot(active.conn)
    with pytest.raises(IdentityConflict, match="different company"):
        record_application(
            search,
            active,
            application("2026-09-16", company="Globex", url="https://shared.example.com/1"),
        )
    assert snapshot(active.conn) == before


def test_the_reverse_gap_is_reported(
    search: sqlite3.Connection, active: ActiveClaim, tmp_path: Path
) -> None:
    """search.db has an application the EI record lacks, e.g. after restoring an old backup."""
    backup = sqlite3.connect(tmp_path / "backup.db")
    try:
        active.conn.backup(backup)
        corr = record_application(search, active, application()).correlation_id
        backup.backup(active.conn)
    finally:
        backup.close()
    assert ei_ids(active.conn) == set()
    assert reconcile(search, {"2026-27": active.conn}) == [
        ledger.Gap(corr, "in search.db but missing from the EI record for claim 2026-27")
    ]


def test_links_to_a_missing_claim_or_the_wrong_claim_are_reported(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    corr = record_application(search, active, application()).correlation_id
    assert reconcile(search, {}) == [
        ledger.Gap(corr, "search.db links it to claim 2026-27, which has no record file")
    ]
    other = record_application(search, None, application("2026-09-20")).correlation_id
    ei_store.insert_application(active.conn, ledger.sanitize(application("2026-09-20"), other))
    assert ledger.Gap(
        other, "in the EI record for claim 2026-27 but search.db links it to no claim"
    ) in reconcile(search, {"2026-27": active.conn})


# --- Statuses after a claim is sealed -------------------------------------------------------


def test_a_status_for_a_sealed_claim_goes_to_search_only(
    datadir: DataDir, search: sqlite3.Connection, opener: Any
) -> None:
    with claims.active_claim(datadir, ClaimSettings(EARLIER, (EARLIER,))) as earlier:
        assert earlier is not None
        corr = record_application(search, earlier, application("2026-01-15")).correlation_id
    both = ClaimSettings(ACTIVE, (EARLIER, ACTIVE))
    claims.seal(claims.plan_seal(datadir, both, "2025-26", dt.date(2026, 10, 31)))
    with claims.active_claim(datadir, both) as active:
        assert active is not None
        assert record_status(search, active, corr, "rejected", "2026-10-30") is False
        sealed = opener(datadir.ei_record_path("2025-26"), read_only=True)
        assert ei_store.status_sequences(sealed)[corr] == [("applied", "2026-01-15")]
        assert reconcile(search, {"2025-26": sealed, "2026-27": active.conn}) == []
