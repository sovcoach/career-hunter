"""doctor: one test per check, and doctor itself never migrates or seals."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from careerhunter import claims, cli, db
from careerhunter.claims import Claim
from tests.conftest import ACTIVE, EARLIER, FIXTURES, FakeProbe, set_claims


def doctor(probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    code = cli.main(["doctor"], probe=probe)
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
        "search.db       OK  schema version 1",
        "search.db       OK  0600",
        "claim 2026-27   OK  schema version 1",
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
    assert "search.db       BAD schema version 0, expected 1; run: careerhunter migrate" in out
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
        f"reconcile       BAD {corr}: in the EI record for claim 2026-27 but missing from search.db"
    ) in out


def _datadir(root: Path) -> claims.DataDir:
    return claims.DataDir(root=root, mount_point=root.parent)
