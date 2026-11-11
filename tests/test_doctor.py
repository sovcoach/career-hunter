"""doctor: one test per check, and doctor itself never migrates or seals."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from careerhunter import claims, cli, db, resume
from careerhunter.claims import Claim
from tests.conftest import ACTIVE, DOCTOR_NOW, EARLIER, FIXTURES, FakeProbe, set_claims


def doctor(probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    code = cli.main(["doctor"], probe=probe, clock=lambda: DOCTOR_NOW)
    return code, capsys.readouterr().out


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe) -> Path:
    """Initialised, with an active claim and the synthetic seed imported."""
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    shutil.copyfile(FIXTURES / "seed_synthetic.yaml", data_env / "seed.yaml")
    assert cli.main(["seed"], probe=probe) == 0
    return data_env


def test_a_clean_setup(ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = doctor(probe, capsys)
    assert code == 0
    for line in (
        "config.yaml     OK",
        "exclusions.yaml OK",
        "ei claim        OK  2026-27 active (from 2026-08-05)",
        f"search.db       OK  schema version {db.latest_version('search')}",
        "search.db       OK  0600",
        "claim 2026-27   OK  schema version 2",
        "claim 2026-27   OK  active, from 2026-08-05",
        "claim 2026-27   OK  0600",
        "reconcile       OK  search.db and 1 claim record agree",
    ):
        assert line in out
    assert "BAD" not in out


def test_no_claim_and_nothing_created(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    for f in db.db_files(data_env / "search.db"):
        if f.exists():
            f.unlink()
    code, out = doctor(probe, capsys)
    assert "search.db       --  not created yet" in out
    assert cli.main(["init"], probe=probe) == 0
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "ei claim        --  none active" in out
    assert "reconcile       OK  search.db and 0 claim records agree" in out


def test_invalid_claim_config(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    set_claims(ready, "2026-27", {"label": "2026-28", "start": "2026-08-05"})
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "consecutive years" in out
    assert "ei claim        --  unknown until config.yaml is valid" in out


def test_ended_active_claim_warns(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    ended = Claim(ACTIVE.label, ACTIVE.start, dt.date(2026, 10, 1))
    set_claims(ready, ACTIVE.label, ended)
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "ei claim        WARN active claim 2026-27 ended on 2026-10-01" in out


def test_active_claim_without_a_file(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert (
        "claim 2026-27   BAD the active claim has no record file; run: careerhunter migrate" in out
    )
    assert not (data_env / "ei_record_2026-27.db").exists()  # doctor didn't create it


def test_a_claim_file_that_does_not_match_config(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    set_claims(ready, ACTIVE.label, Claim(ACTIVE.label, dt.date(2026, 8, 1)))
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "A claim's start date can't change" in out


def test_schema_behind_is_reported_not_migrated(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    path = data_env / "search.db"
    for f in db.db_files(path):
        if f.exists():
            f.unlink()
    conn = db.connect(path)
    db.migrate(conn, "search", [])  # identity only: schema version 0
    conn.close()
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    expected = db.latest_version("search")
    assert (
        f"search.db       BAD schema version 0, expected {expected}; run: careerhunter migrate"
        in out
    )
    assert "reconcile       --  skipped until the problems above are fixed" in out
    conn = db.connect_read_only(path)
    assert db.schema_version(conn) == 0
    conn.close()


def test_a_file_of_the_wrong_kind(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    path = data_env / "search.db"
    for f in db.db_files(path):
        if f.exists():
            f.unlink()
    conn = db.connect(path)
    db.migrate(conn, "ei_record")
    conn.close()
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "search.db       BAD not a search database (identity: ei_record)" in out


def test_the_v010_database(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = ready / "careerhunter.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "careerhunter.db WARN empty v0.1.0 database, no longer used" in out
    conn = sqlite3.connect(legacy)
    conn.execute("INSERT INTO companies (name) VALUES ('Synthetic Co')")
    conn.commit()
    conn.close()
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "careerhunter.db BAD v0.1.0 database holding 1 rows" in out


def test_an_unreadable_v010_database(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (ready / "careerhunter.db").write_bytes(b"not a database at all, just synthetic bytes")
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "careerhunter.db BAD v0.1.0 database that can't be read" in out


def _earlier_file(data: Path, probe: FakeProbe) -> Path:
    os.rename(data / "ei_record_2026-27.db", data / "hold.db")
    set_claims(data, EARLIER.label, EARLIER)
    assert cli.main(["migrate"], probe=probe) == 0
    os.rename(data / "hold.db", data / "ei_record_2026-27.db")
    set_claims(data, ACTIVE.label, EARLIER, ACTIVE)
    return data / "ei_record_2025-26.db"


def test_an_unsealed_earlier_claim(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _earlier_file(ready, probe)
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert (
        "claim 2025-26   BAD not the active claim, but not sealed. "
        "Run: careerhunter claim seal 2025-26"
    ) in out
    file_claim = claims.read_claim_file(path)
    assert file_claim is not None and file_claim.sealed_at is None  # doctor didn't seal it


def test_a_sealed_claim_must_stay_read_only(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _earlier_file(ready, probe)
    settings = claims.ClaimSettings(ACTIVE, (EARLIER, ACTIVE))
    claims.seal(claims.plan_seal(_datadir(ready), settings, "2025-26", dt.date(2026, 10, 31)))
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "claim 2025-26   OK  sealed" in out

    os.chmod(path, 0o600)
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "sealed, but ei_record_2025-26.db is not read-only (0o600)" in out

    os.chmod(path, 0o400)
    (ready / "ei_record_2025-26.db-wal").write_bytes(b"")
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "sealed, but ei_record_2025-26.db-wal still exist" in out


def test_claim_files_config_does_not_know(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copyfile(ready / "ei_record_2026-27.db", ready / "ei_record_2027-28.db")
    (ready / "ei_record_notes.db").write_bytes(b"")
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "claim 2027-28   BAD ei_record_2027-28.db is not listed under ei.claims" in out
    assert "ei_record_notes.db BAD not a valid claim file name" in out


def test_reconciliation_gaps_are_reported(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = db.connect(ready / "search.db")
    corr = conn.execute("SELECT correlation_id FROM applications LIMIT 1").fetchone()[0]
    conn.execute(
        "DELETE FROM decision_history WHERE application_id = "
        "(SELECT id FROM applications WHERE correlation_id = ?)",
        (corr,),
    )
    conn.execute("DELETE FROM applications WHERE correlation_id = ?", (corr,))
    conn.close()
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert (
        f"orphan          BAD {corr}: in the EI record for claim 2026-27 but missing from search.db"
    ) in out


def _bytes(ready: Path) -> dict[str, str]:
    """The content of every database and non-empty WAL in the data folder."""
    found = {}
    for path in sorted(ready.glob("*.db*")):
        if path.name.endswith("-shm") or (path.name.endswith("-wal") and not path.stat().st_size):
            continue
        found[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def test_an_orphan_on_either_side_is_reported_and_left_unchanged(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    """#34: doctor names orphans in both directions and never fixes them."""
    conn = db.connect(ready / "search.db")
    first, second = (
        r[0] for r in conn.execute("SELECT correlation_id FROM applications ORDER BY id LIMIT 2")
    )
    # EI side only: the search row is gone.
    conn.execute(
        "DELETE FROM decision_history WHERE application_id = "
        "(SELECT id FROM applications WHERE correlation_id = ?)",
        (first,),
    )
    conn.execute("DELETE FROM applications WHERE correlation_id = ?", (first,))
    # search.db only: a row linked to the claim that its file never got.
    posting = conn.execute("SELECT id FROM postings LIMIT 1").fetchone()[0]
    orphan = "ab" * 16
    conn.execute(
        "INSERT INTO applications (correlation_id, ei_claim, posting_id, date_applied, "
        "date_precision, method) VALUES (?, '2026-27', ?, '2026-10-21', 'exact', 'email')",
        (orphan, posting),
    )
    conn.close()
    before = _bytes(ready)
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert (
        f"orphan          BAD {first}: in the EI record for claim 2026-27 "
        "but missing from search.db"
    ) in out
    assert (
        f"orphan          BAD {orphan}: in search.db but missing from the EI record for claim "
        "2026-27"
    ) in out
    assert "reconcile       --  left as they are; doctor never fixes either file" in out
    assert second not in out
    assert _bytes(ready) == before
    code, again = doctor(probe, capsys)  # still there: nothing was repaired
    assert code == cli.EXIT_ERROR and f"orphan          BAD {orphan}" in again
    assert _bytes(ready) == before


def test_loose_exports_and_snapshots_are_fixed(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["export", "--claim", "2026-27", "--from", "2026-09-01", "--to", "2026-09-30"]
    assert cli.main(argv, probe=probe) == 0
    code, out = doctor(probe, capsys)
    assert "exports/        OK  3 files, all owner-only" in out
    assert "snapshots/      OK  0 files, all owner-only" in out

    loose = next((ready / "exports").glob("*.pdf"))
    os.chmod(loose, 0o644)
    year = ready / "snapshots" / "2026"
    year.mkdir(mode=0o755)
    (year / "synthetic-ad.html").write_text("<p>synthetic</p>")
    os.chmod(year / "synthetic-ad.html", 0o644)
    elsewhere = ready / "elsewhere.txt"
    elsewhere.write_text("synthetic")
    os.chmod(elsewhere, 0o644)
    (ready / "snapshots" / "link").symlink_to(elsewhere)

    code, out = doctor(probe, capsys)
    assert code == 0
    assert f"exports/        FIXED {loose.name} (0o644) -> owner-only" in out
    assert "snapshots/      FIXED 2026 (0o755), 2026/synthetic-ad.html (0o644)" in out
    assert loose.stat().st_mode & 0o777 == 0o600
    assert year.stat().st_mode & 0o777 == 0o700
    assert elsewhere.stat().st_mode & 0o777 == 0o644  # a symlink is never followed

    code, out = doctor(probe, capsys)
    assert "exports/        OK  3 files, all owner-only" in out
    assert "snapshots/      OK  1 file, all owner-only" in out


def _datadir(root: Path) -> claims.DataDir:
    return claims.DataDir(root=root, mount_point=root.parent)


def test_a_missing_timezone_is_a_warning_not_a_failure(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    import yaml

    code, out = doctor(probe, capsys)
    assert "timezone        OK  UTC" in out
    path = ready / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    del doc["schedule"]["timezone"]
    path.write_text(yaml.safe_dump(doc))
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "timezone        WARN schedule.timezone isn't set, so dates use UTC" in out


# --- resume ---------------------------------------------------------------------------------


def _import_resume(data: Path, text: str = "Avery Example, coordinator") -> None:
    conn = db.connect(data / "search.db")
    try:
        resume.import_text(data, conn, text, now=lambda: "2026-11-04T12:00:00Z")
    finally:
        conn.close()


def _set_resume(data: Path, value: str | None) -> None:
    path = data / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["search"]["resume"] = value
    path.write_text(yaml.safe_dump(doc))


def test_no_resume_is_optional(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "resume          --  not imported (optional: careerhunter resume import)" in out


def test_an_imported_resume_in_use_is_ok(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _import_resume(ready)
    _set_resume(ready, resume.CONFIG_VALUE)
    code, out = doctor(probe, capsys)
    assert code == 0, out
    assert "resume          OK  matches its import" in out
    assert "Avery" not in out


def test_an_imported_resume_not_in_config_is_a_warning(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _import_resume(ready)
    code, out = doctor(probe, capsys)
    assert code == 0
    assert (
        f"resume          WARN imported but not used: set search.resume: {resume.CONFIG_VALUE}"
        in out
    )


def test_search_resume_set_without_an_import_is_bad(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_resume(ready, resume.CONFIG_VALUE)
    code, out = doctor(probe, capsys)
    assert code == 1
    assert "resume          BAD no resume has been imported" in out


def test_search_resume_naming_another_file_is_bad(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _import_resume(ready)
    _set_resume(ready, "resume.txt")
    code, out = doctor(probe, capsys)
    assert code == 1
    assert f"resume          BAD search.resume must be {resume.CONFIG_VALUE}" in out


def test_a_file_that_does_not_match_its_import_is_bad(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    """For example a COMMIT that failed after the rename, with the restore failing too."""
    _import_resume(ready)
    _set_resume(ready, resume.CONFIG_VALUE)
    current = ready / resume.RESUME_DIR / resume.CURRENT
    current.write_text("Avery Example, a different text")
    code, out = doctor(probe, capsys)
    assert code == 1
    assert "resume          BAD the resume file does not match its import" in out
    assert "different text" not in out


def test_loose_resume_modes_are_fixed_then_checked(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _import_resume(ready)
    _set_resume(ready, resume.CONFIG_VALUE)
    folder = ready / resume.RESUME_DIR
    os.chmod(folder / resume.CURRENT, 0o644)
    os.chmod(folder, 0o755)  # noqa: S103 - deliberately too loose
    code, out = doctor(probe, capsys)
    assert code == 0, out
    assert "resume/         FIXED folder -> 0700" in out
    assert "resume/         FIXED current.txt (0o644) -> owner-only" in out
    assert "resume          OK  matches its import" in out
    assert (folder / resume.CURRENT).stat().st_mode & 0o777 == 0o600
    assert folder.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name", [resume.PREVIOUS, f"{resume.TMP_PREFIX}abc{resume.TMP_SUFFIX}"])
def test_import_leftovers_are_a_warning(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    _import_resume(ready)
    _set_resume(ready, resume.CONFIG_VALUE)
    leftover = ready / resume.RESUME_DIR / name
    leftover.write_text("left over")
    os.chmod(leftover, 0o600)
    code, out = doctor(probe, capsys)
    assert code == 0, out
    assert f"resume          WARN left by an interrupted or running import: {name}" in out
    assert "resume          OK  matches its import" in out


# --- lanes, watch runs and the bot's alert hold (#38, #33) ------------------------------------


def _write(path: Path, sql: str, *params: object) -> None:
    conn = db.connect(path)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


def _stamp(delta: dt.timedelta) -> str:
    return (DOCTOR_NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_lanes_and_watch_runs_on_a_fresh_setup(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _code, out = doctor(probe, capsys)
    assert "lanes           OK  2 lanes; off-lane postings alert at high fit or better" in out
    assert "watch run       --  none recorded yet" in out
    assert "bot alerts      OK  not held" in out


def test_lanes_off_is_reported_not_silent(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ready / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    del doc["search"]["lanes"]
    doc["search"]["target_titles"] = ["Example Role"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    code, out = doctor(probe, capsys)
    assert code == 0
    assert (
        "lanes           --  not configured: lane checking is off, alerts go by fit then oldest"
        in out
    )


def test_a_finished_run_reports_what_is_held(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        ready / "search.db",
        "INSERT INTO watch_runs (started_at, finished_at, budget_reached, boards_fetched, "
        "boards_failed, boards_left, held_unscored, held_no_fit, held_off_lane) "
        "VALUES (?, ?, 1, 3, 0, 1, 4, 0, 2)",
        _stamp(dt.timedelta(minutes=20)),
        _stamp(dt.timedelta(minutes=12)),
    )
    code, out = doctor(probe, capsys)
    assert code == 0
    assert (
        "watch held      OK  4 unscored, 0 with no fit, 2 off-lane below the threshold; "
        "that run's budget left work for the next one"
    ) in out


def test_postings_with_no_fit_are_a_warning(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        ready / "search.db",
        "INSERT INTO watch_runs (started_at, finished_at, boards_fetched, boards_failed, "
        "boards_left, held_unscored, held_no_fit, held_off_lane) VALUES (?, ?, 1, 0, 0, 0, 3, 0)",
        _stamp(dt.timedelta(minutes=20)),
        _stamp(dt.timedelta(minutes=12)),
    )
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "watch held      WARN 0 unscored, 3 with no fit" in out
    assert "A tier name isn't in fit_rubric.gap_tiers" in out


def test_a_run_that_never_finished_is_bad_and_its_counts_are_not_used(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        ready / "search.db",
        "INSERT INTO watch_runs (started_at) VALUES (?)",
        _stamp(dt.timedelta(minutes=45)),
    )
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "watch run       BAD the last run started 45 min ago and did not finish" in out
    assert "watch held      --  no finished run yet, so nothing is counted" in out


def test_a_run_in_progress_is_ok(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        ready / "search.db",
        "INSERT INTO watch_runs (started_at) VALUES (?)",
        _stamp(dt.timedelta(minutes=2)),
    )
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "watch run       OK  a run started 2 min ago is in progress" in out


def test_alerts_held_by_the_bot_are_bad(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        ready / "search.db",
        "INSERT INTO bot_state (key, value) VALUES ('alerts_held_since', '2026-10-28T16:00:00Z')",
    )
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert (
        "bot alerts      BAD held since 2026-10-28T16:00:00Z: the bot couldn't read config.yaml "
        "at its last poll"
    ) in out


# --- health: useful work and failing components (#42) -------------------------------------


def _finished(ready: Path, minutes_ago: int, useful: int | None) -> None:
    _write(
        ready / "search.db",
        "INSERT INTO watch_runs (started_at, finished_at, boards_fetched, boards_failed, "
        "boards_left, held_unscored, held_no_fit, held_off_lane, useful) "
        "VALUES (?, ?, 1, 0, 0, 0, 0, 0, ?)",
        _stamp(dt.timedelta(minutes=minutes_ago + 5)),
        _stamp(dt.timedelta(minutes=minutes_ago)),
        useful,
    )


def _set_config(ready: Path, **sections: object) -> None:
    path = ready / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.update(sections)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_three_unproductive_runs_in_a_row_are_a_warning(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _finished(ready, 300, 1)
    for minutes in (200, 100, 10):
        _finished(ready, minutes, 0)
    code, out = doctor(probe, capsys)
    assert code == 0  # a warning, not a failure
    assert (
        "watch useful    WARN the last 3 watch runs did no useful work; last useful run "
        "300 min ago. See logs/watch.log"
    ) in out


def test_fewer_than_the_threshold_is_ok_and_the_threshold_is_configurable(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _finished(ready, 300, 1)
    _finished(ready, 100, 0)
    _finished(ready, 10, 0)
    _code, out = doctor(probe, capsys)
    assert "watch useful    OK  last useful run 300 min ago" in out
    _set_config(ready, health={"unproductive_runs": 2})
    _code, out = doctor(probe, capsys)
    assert "watch useful    WARN the last 2 watch runs did no useful work" in out


def test_a_crashed_run_counts_and_a_run_before_0018_stops_the_count(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _finished(ready, 400, None)  # finished before the migration: unknown
    _finished(ready, 300, 0)
    _finished(ready, 200, 0)
    _write(
        ready / "search.db",
        "INSERT INTO watch_runs (started_at) VALUES (?)",
        _stamp(dt.timedelta(minutes=100)),
    )  # never finished: it did nothing useful
    _write(
        ready / "search.db",
        "INSERT INTO watch_runs (started_at) VALUES (?)",
        _stamp(dt.timedelta(minutes=1)),
    )  # still running: not counted either way
    _code, out = doctor(probe, capsys)
    assert (
        "watch useful    WARN the last 3 watch runs did no useful work; no useful run recorded yet"
    ) in out


def test_no_run_yet_is_neither_ok_nor_a_warning(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _code, out = doctor(probe, capsys)
    assert "watch useful    --  no useful run recorded yet" in out
    assert "discovery useful" not in out  # discovery is off
    assert "health " not in out


def test_unproductive_discovery_passes_are_a_warning(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = yaml.safe_load((ready / "config.yaml").read_text())
    doc["discovery"]["searxng_url"] = "http://127.0.0.1:8888/search"
    (ready / "config.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    _code, out = doctor(probe, capsys)
    assert "discovery useful --  no useful pass yet" in out
    _write(
        ready / "search.db",
        "UPDATE discovery_state SET useful_at = ?, unproductive = 3",
        _stamp(dt.timedelta(minutes=600)),
    )
    _code, out = doctor(probe, capsys)
    assert (
        "discovery useful WARN the last 3 discovery passes got no usable reply; "
        "last useful pass 600 min ago"
    ) in out
    _write(ready / "search.db", "UPDATE discovery_state SET unproductive = 0")
    _code, out = doctor(probe, capsys)
    assert "discovery useful OK  last useful pass 600 min ago" in out


def test_a_failing_component_is_named_with_its_count_and_held_postings(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        ready / "search.db",
        "UPDATE component_health SET failures = 3, last_failure = 'unreachable', held = 4 "
        "WHERE component = 'lm_studio'",
    )
    _write(
        ready / "search.db",
        "UPDATE component_health SET failures = 1, last_failure = 'failed', held = 0 "
        "WHERE component = 'boards'",
    )
    code, out = doctor(probe, capsys)
    assert code == 0
    assert (
        "health          WARN LM Studio failed the last 3 runs (unreachable); "
        "4 postings held for scoring"
    ) in out
    assert (
        "health          OK  every board failed the last 1 run (failed); "
        "0 postings held for scoring"
    ) in out
