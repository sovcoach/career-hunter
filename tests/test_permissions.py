"""Database files are owner-only, and doctor reports permission problems."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from careerhunter import cli, db
from tests.conftest import FIXTURES, FakeProbe


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_new_database_and_wal_files_are_0600(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("INSERT INTO companies (name, source) VALUES ('Acme Widgets', 'manual')")
    for f in db.db_files(path):
        assert f.exists(), f
        assert mode(f) == 0o600, f
    conn.close()


def test_existing_loose_database_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    db.connect(path).close()
    os.chmod(path, 0o644)
    assert db.loose_permissions(path) == ["x.db (0o644)"]
    db.connect(path).close()
    assert db.loose_permissions(path) == []


def test_doctor_reports_and_fixes_db_permissions(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    assert mode(data_env / "careerhunter.db") == 0o600
    os.chmod(data_env / "careerhunter.db", 0o644)
    capsys.readouterr()
    assert cli.main(["doctor"], probe=probe) == 0
    assert "db permissions  FIXED careerhunter.db (0o644) -> 0600" in capsys.readouterr().out
    assert cli.main(["doctor"], probe=probe) == 0
    assert "db permissions  OK  0600" in capsys.readouterr().out


def test_doctor_flags_loose_config(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    shutil.copyfile(FIXTURES / "seed_synthetic.yaml", data_env / "seed.yaml")
    os.chmod(data_env / "config.yaml", 0o644)
    assert cli.main(["doctor"], probe=probe) == cli.EXIT_ERROR
    assert "chmod 600" in capsys.readouterr().out
