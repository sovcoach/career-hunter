"""One role posted more than once (Batch R): the same company, title, known location
and near-identical job description, whatever the requisition ids. Alerted once, with
one application and one EI entry naming every requisition. Synthetic only."""

from __future__ import annotations

import copy
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from careerhunter import db, duplicates, export, jobdesc, search_store, snapshots, watch
from careerhunter.filters import PostingText
from careerhunter.score import Score
from careerhunter.storage_guard import DataDir
from tests import test_bot
from tests.conftest import ACTIVE
from tests.test_bot import Env, _insert, _poll, _rows
from tests.test_score import RUBRIC
from tests.test_watch import Scripted, company, fetched, url

env = test_bot.env

DUTIES = (
    "Coordinate synthetic deliveries across the dock. Keep the yard schedule. "
    "Report weekly on synthetic volumes. Requisition 100100."
)


def _company(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO companies (name, source) VALUES ('Example Robotics', 'manual') "
        "ON CONFLICT DO NOTHING"
    )
    return int(conn.execute("SELECT id FROM companies").fetchone()[0])


def _kept(
    conn: sqlite3.Connection,
    root: Path,
    title: str,
    location: str,
    text: str | None,
    req: str,
) -> int:
    company_id = _company(conn)
    cur = conn.execute(
        "INSERT INTO postings (company_id, title, location, requisition_id, first_seen, source) "
        "VALUES (?, ?, ?, ?, '2026-11-02', 'watcher')",
        (company_id, title, location, req),
    )
    posting_id = int(cur.lastrowid or 0)
    if text is not None:
        (root / "snapshots").mkdir(mode=0o700, exist_ok=True)
        path = snapshots.save(root, text.encode(), "txt", dt.date(2026, 11, 2))
        search_store.keep_jd(conn, posting_id, path, jobdesc.normalized_sha256(text))
    return posting_id


@pytest.fixture
def conn(datadir: DataDir) -> sqlite3.Connection:
    c = db.connect(datadir.search_db_path)
    db.migrate(c, "search")
    return c


def _original(conn: sqlite3.Connection, datadir: DataDir, posting_id: int, text: str) -> int | None:
    return duplicates.find_original(conn, datadir.root, posting_id, text)


def test_the_same_role_under_another_requisition_is_a_duplicate(
    conn: sqlite3.Connection, datadir: DataDir
) -> None:
    first = _kept(conn, datadir.root, "Operations Coordinator", "Example City", DUTIES, "R-1")
    again = DUTIES.replace("100100", "200200")  # long numbers are ignored
    second = _kept(
        conn, datadir.root, "Operations Coordinator (R12345)", "example city.", again, "R-2"
    )
    assert _original(conn, datadir, second, again) == first


def test_a_near_identical_text_is_a_duplicate_and_a_different_one_is_not(
    conn: sqlite3.Connection, datadir: DataDir
) -> None:
    first = _kept(conn, datadir.root, "Operations Coordinator", "Example City", DUTIES, "R-1")
    near = DUTIES.replace("weekly", "weekly,")
    second = _kept(conn, datadir.root, "Operations Coordinator", "Example City", near, "R-2")
    assert _original(conn, datadir, second, near) == first
    other = "Plan synthetic routes for the night shift. Drive the yard tractor."
    third = _kept(conn, datadir.root, "Operations Coordinator", "Example City", other, "R-3")
    assert _original(conn, datadir, third, other) is None


@pytest.mark.parametrize(
    ("location", "title"),
    [
        ("Sample Village", "Operations Coordinator"),  # another place
        ("", "Operations Coordinator"),  # one location unknown
        ("Operations Coordinator", "Shift Lead"),  # another title (location text here)
    ],
)
def test_another_place_an_unknown_one_or_another_title_is_not_a_duplicate(
    conn: sqlite3.Connection, datadir: DataDir, location: str, title: str
) -> None:
    _kept(conn, datadir.root, "Operations Coordinator", "Example City", DUTIES, "R-1")
    place = location if title == "Operations Coordinator" else "Example City"
    second = _kept(conn, datadir.root, title, place, DUTIES, "R-2")
    assert _original(conn, datadir, second, DUTIES) is None


def test_two_unknown_locations_are_never_merged(conn: sqlite3.Connection, datadir: DataDir) -> None:
    _kept(conn, datadir.root, "Operations Coordinator", "", DUTIES, "R-1")
    second = _kept(conn, datadir.root, "Operations Coordinator", "", DUTIES, "R-2")
    assert _original(conn, datadir, second, DUTIES) is None


def test_a_posting_with_no_kept_jd_is_never_merged(
    conn: sqlite3.Connection, datadir: DataDir
) -> None:
    _kept(conn, datadir.root, "Operations Coordinator", "Example City", None, "R-1")
    second = _kept(conn, datadir.root, "Operations Coordinator", "Example City", DUTIES, "R-2")
    assert _original(conn, datadir, second, DUTIES) is None


def test_a_group_points_at_its_first_posting(conn: sqlite3.Connection, datadir: DataDir) -> None:
    first = _kept(conn, datadir.root, "Operations Coordinator", "Example City", DUTIES, "R-1")
    ids = [first]
    for req in ("R-2", "R-3"):
        posting = _kept(conn, datadir.root, "Operations Coordinator", "Example City", DUTIES, req)
        original = _original(conn, datadir, posting, DUTIES)
        assert original == first
        duplicates.mark(conn, posting, original)
        ids.append(posting)
    assert duplicates.group(conn, ids[2]) == ids
    assert duplicates.requisitions(conn, ids) == "R-1, R-2, R-3"
    assert duplicates.requisitions(conn, [first]) is None


# --- in the watch run ---------------------------------------------------------------------


def _two_jobs(datadir: DataDir) -> list[str]:
    (datadir.root / "snapshots").mkdir(mode=0o700, exist_ok=True)
    conn = db.connect(datadir.search_db_path)
    db.migrate(conn, "search")

    def scorer(posting: PostingText) -> Score:
        return Score(llm_output="model text", gaps="[]", tier="high")

    jobs = [
        {
            "id": ident,
            "title": "Operations Coordinator",
            "absolute_url": f"https://boards.greenhouse.io/examplerobotics/jobs/{ident}",
            "location": {"name": "Example City"},
            "requisition_id": f"REQ-{ident}",
            "content": DUTIES.replace("100100", str(ident * 100)),
        }
        for ident in (1001, 1002)
    ]
    try:
        return watch.run(
            conn,
            [company("Example Robotics", "greenhouse", "examplerobotics")],
            today="2026-11-02",
            fetcher=Scripted(
                {url("greenhouse", "examplerobotics"): fetched({"jobs": jobs})}, on_board=True
            ),
            config=copy.deepcopy(RUBRIC),
            scorer=scorer,
            data_dir=datadir.root,
        )
    finally:
        conn.close()


def test_the_watcher_marks_the_second_posting_and_alerts_only_the_first(
    datadir: DataDir,
) -> None:
    lines = _two_jobs(datadir)
    assert any("the same role as posting 1; alerted with it" in line for line in lines)
    conn = db.connect(datadir.search_db_path)
    try:
        assert [tuple(r) for r in conn.execute("SELECT id, duplicate_of FROM postings")] == [
            (1, None),
            (2, 1),
        ]
        assert [a.posting_id for a in search_store.pending_alerts(conn)] == [1]
        assert search_store.digest_fields(conn, [1])[1].also == ("REQ-1002",)
    finally:
        conn.close()


# --- the bot ------------------------------------------------------------------------------


def _group(env: Env) -> tuple[int, int]:
    first = _insert(env, "Example Robotics", "Synthetic Analyst", "https://jobs.example.com/d/1")
    second = _insert(env, "Example Robotics", "Synthetic Analyst", "https://jobs.example.com/d/2")
    conn = db.connect(env.datadir.search_db_path)
    try:
        conn.execute(
            "UPDATE postings SET requisition_id = 'R-1', location = 'Example City' WHERE id = ?",
            (first,),
        )
        conn.execute(
            "UPDATE postings SET requisition_id = 'R-2', location = 'Example City', "
            "duplicate_of = ? WHERE id = ?",
            (first, second),
        )
    finally:
        conn.close()
    return first, second


def test_the_group_is_alerted_once_listing_the_other_requisition(env: Env) -> None:
    _group(env)
    assert _poll(env) == 0
    rows = _rows(env)
    assert len(rows) == 1
    assert rows[0][2] == "Also posted as R-2"


def _skips(env: Env) -> dict[int, str]:
    conn = db.connect_read_only(env.datadir.search_db_path)
    try:
        return {int(r[0]): str(r[1]) for r in conn.execute("SELECT posting_id, reason FROM skips")}
    finally:
        conn.close()


def test_applied_records_one_application_naming_every_requisition(env: Env) -> None:
    first, second = _group(env)
    assert _poll(env) == 0
    env.press("Applied 1")
    env.press("Email")
    env.press("Today")
    (app,) = test_bot.ei_apps(env)
    assert app["requisitions"] == "R-1, R-2"
    assert _skips(env) == {}  # covered by the application, never skipped
    conn = db.connect(env.datadir.search_db_path)
    try:
        apps = [int(r[0]) for r in conn.execute("SELECT id FROM applications")]
        assert [int(r[0]) for r in conn.execute("SELECT posting_id FROM applications")] == [first]
        assert duplicates.covering_application(conn, second) == apps[0]
        assert duplicates.covering_application(conn, first) is None  # its own, not covered
        assert search_store.pending_alerts(conn) == []
        # Not re-scored, not offered for rescore, and not swept up as a repost.
        conn.execute("UPDATE postings SET scored_resume_sha256 = NULL")
        assert conn.execute(watch._RESCORE_SQL).fetchall() == []
        assert conn.execute(watch._NEEDS_RESCORE_SQL, (second,)).fetchone() is None
        assert search_store.rescore_candidates(conn, [second]) == []
        assert search_store.unhandled_reposts(conn) == []
    finally:
        conn.close()


def test_a_duplicate_is_covered_only_once_its_group_is_applied_to(
    env: Env,
) -> None:
    _, second = _group(env)
    conn = db.connect(env.datadir.search_db_path)
    try:
        assert duplicates.covering_application(conn, second) is None  # nothing applied yet
        conn.execute("UPDATE postings SET scored_resume_sha256 = NULL")
        rescorable = [int(r[0]) for r in conn.execute(watch._RESCORE_SQL)]
        assert second in rescorable
    finally:
        conn.close()


def test_skipped_records_a_skip_for_every_posting_in_the_group(env: Env) -> None:
    first, second = _group(env)
    assert _poll(env) == 0
    env.press("Skipped 1")
    env.press("Not a fit")
    assert _skips(env) == {first: "not a fit", second: "not a fit"}


def test_a_typed_reason_reaches_every_posting_in_the_group(env: Env) -> None:
    first, second = _group(env)
    assert _poll(env) == 0
    env.press("Skipped 1")
    env.press("Other")
    env.say("Synthetic reason", reply_to=env.prompt_id())
    assert _skips(env) == {first: "Synthetic reason", second: "Synthetic reason"}


def test_a_single_posting_has_no_requisitions_in_its_ei_entry(env: Env) -> None:
    test_bot.apply(env)
    (app,) = test_bot.ei_apps(env)
    assert app["requisitions"] is None


# --- the EI record --------------------------------------------------------------------------


def test_ei_0005_keeps_rows_and_fixes_requisitions_once_written(tmp_path: Path) -> None:
    ei = db.load_migrations("ei_record")
    conn = db.connect(tmp_path / "ei.db")
    try:
        db.migrate(conn, "ei_record", ei[:4])
        conn.execute(
            "INSERT INTO claim (id, label, start_date) VALUES (1, '2026-27', '2026-09-01')"
        )
        insert = (
            "INSERT INTO applications (correlation_id, employer, role, date_applied, "
            "date_precision, method) "
            "VALUES (?, 'Example Robotics', ?, '2026-09-02', 'exact', 'email')"
        )
        conn.execute(insert, (f"{1:032x}", "Analyst"))
        before = [tuple(r) for r in conn.execute("SELECT * FROM applications")]
        assert db.migrate(conn, "ei_record") == [5]
        after = [tuple(r)[:-1] for r in conn.execute("SELECT * FROM applications")]
        assert after == before
        assert conn.execute("SELECT requisitions FROM applications").fetchone()[0] is None
        for bad in ("", "   ", "x" * 501):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO applications (correlation_id, employer, role, date_applied, "
                    "date_precision, method, requisitions) VALUES (?, 'Example Robotics', "
                    "'Clerk', '2026-09-03', 'exact', 'email', ?)",
                    (f"{2:032x}", bad),
                )
        conn.execute(
            "INSERT INTO applications (correlation_id, employer, role, date_applied, "
            "date_precision, method, requisitions) VALUES (?, 'Example Robotics', 'Clerk', "
            "'2026-09-03', 'exact', 'email', 'R-1, R-2')",
            (f"{3:032x}",),
        )
        for change in ("R-9", None):
            with pytest.raises(sqlite3.IntegrityError, match="requisitions can not be changed"):
                conn.execute("UPDATE applications SET requisitions = ? WHERE id = 2", (change,))
        with pytest.raises(sqlite3.IntegrityError, match="requisitions can not be changed"):
            conn.execute("UPDATE applications SET requisitions = 'R-1' WHERE id = 1")
    finally:
        conn.close()


def test_the_export_shows_the_requisitions(env: Env) -> None:
    _group(env)
    assert _poll(env) == 0
    env.press("Applied 1")
    env.press("Email")
    env.press("Today")
    conn = db.connect_read_only(env.datadir.ei_record_path(ACTIVE.label))
    try:
        rows = export.collect_applications(conn, dt.date(2026, 8, 1), dt.date(2027, 7, 31))
    finally:
        conn.close()
    assert [r["requisitions"] for r in rows] == ["R-1, R-2"]
    assert rows[0]["role_shown"] == "Synthetic Analyst (requisitions: R-1, R-2)"
    assert "requisitions" in export.APPLICATION_COLUMNS


def test_the_export_reads_a_file_from_before_requisitions(tmp_path: Path) -> None:
    ei = db.load_migrations("ei_record")
    conn = db.connect(tmp_path / "ei.db")
    try:
        db.migrate(conn, "ei_record", ei[:4])
        conn.execute(
            "INSERT INTO claim (id, label, start_date) VALUES (1, '2026-27', '2026-09-01')"
        )
        conn.execute(
            "INSERT INTO applications (correlation_id, employer, role, date_applied, "
            "date_precision, method) VALUES (?, 'Example Robotics', 'Analyst', '2026-09-02', "
            "'exact', 'email')",
            (f"{1:032x}",),
        )
        rows = export.collect_applications(conn, dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    finally:
        conn.close()
    assert [(r["requisitions"], r["role_shown"]) for r in rows] == [("", "Analyst")]
