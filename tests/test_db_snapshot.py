"""`careerhunter db snapshot`: read-only copies of search.db and the EI records."""

from __future__ import annotations

import datetime as dt
import sqlite3
import stat
from pathlib import Path

import pytest

from careerhunter import claims, cli, db, dbsnapshot
from careerhunter.claims import ClaimSettings
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE, FakeProbe

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.UTC)
FOLDER = "careerhunter-snapshot-20261110T120000Z"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _fill(datadir: DataDir) -> sqlite3.Connection:
    """A migrated search.db with one company, left open in WAL mode with a row the WAL
    still holds, and an EI record for the active claim."""
    conn = db.connect(datadir.search_db_path)
    db.migrate(conn, "search")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("INSERT INTO companies (name, source) VALUES ('Example Robotics', 'manual')")
    with claims.active_claim(datadir, ClaimSettings(ACTIVE, (ACTIVE,))) as active:
        assert active is not None
        active.conn.execute(
            "INSERT INTO activities (date_precision, date, type, description) "
            "VALUES ('exact', '2026-09-15', 'course', 'Synthetic course')"
        )
    return conn


@pytest.fixture
def dest(fake_volume: Path) -> Path:
    out = fake_volume / "Investigations"
    out.mkdir()
    return out


def test_copies_both_databases_read_only_and_checked(
    datadir: DataDir, probe: FakeProbe, dest: Path
) -> None:
    live = _fill(datadir)
    try:
        before = {p.name: p.read_bytes() for p in dbsnapshot.sources(datadir)}
        folder, copied = dbsnapshot.snapshot(dest, datadir, probe, NOW)
        after = {p.name: p.read_bytes() for p in dbsnapshot.sources(datadir)}
    finally:
        live.close()
    assert after == before  # the sources are never changed
    assert folder == dest.resolve() / FOLDER
    assert [c.name for c in copied] == ["search.db", "ei_record_2026-27.db"]
    assert {c.integrity for c in copied} == {"ok"}
    assert _mode(folder) == 0o700
    assert sorted(p.name for p in folder.iterdir()) == sorted(c.name for c in copied)
    for item in copied:
        assert _mode(folder / item.name) == 0o600
    copy = db.connect_read_only(folder / "search.db")
    try:
        # The row still in the source's WAL is in the copy, and the copy is one file.
        assert copy.execute("SELECT name FROM companies").fetchone()[0] == "Example Robotics"
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.schema_version(copy) == db.latest_version("search")
    finally:
        copy.close()
    ei = db.connect_read_only(folder / "ei_record_2026-27.db")
    try:
        assert ei.execute("SELECT description FROM activities").fetchone()[0] == "Synthetic course"
    finally:
        ei.close()


def test_refuses_a_volume_diskutil_does_not_report_as_encrypted(
    datadir: DataDir, fake_volume: Path, dest: Path
) -> None:
    _fill(datadir).close()
    probe = FakeProbe(fake_volume, volume={"Encryption": False})
    with pytest.raises(dbsnapshot.SnapshotError, match=r"not report .* as encrypted"):
        dbsnapshot.snapshot(dest, datadir, probe, NOW)
    assert list(dest.iterdir()) == []


def test_refuses_when_diskutil_cannot_say(datadir: DataDir, fake_volume: Path, dest: Path) -> None:
    _fill(datadir).close()
    probe = FakeProbe(fake_volume, volume={"MountPoint": "/Volumes/Other"})
    with pytest.raises(dbsnapshot.SnapshotError, match="could not report"):
        dbsnapshot.snapshot(dest, datadir, probe, NOW)


def test_refuses_a_locked_volume(datadir: DataDir, fake_volume: Path, dest: Path) -> None:
    _fill(datadir).close()
    probe = FakeProbe(fake_volume, volume={"Locked": True})
    with pytest.raises(dbsnapshot.SnapshotError, match="locked"):
        dbsnapshot.snapshot(dest, datadir, probe, NOW)


def test_refuses_a_folder_inside_the_data_folder(datadir: DataDir, probe: FakeProbe) -> None:
    _fill(datadir).close()
    inside = datadir.root / "exports"
    with pytest.raises(dbsnapshot.SnapshotError, match="inside the data folder"):
        dbsnapshot.snapshot(inside, datadir, probe, NOW)
    assert list(inside.iterdir()) == []


def test_refuses_a_folder_inside_a_git_work_tree(
    datadir: DataDir, fake_volume: Path, dest: Path
) -> None:
    _fill(datadir).close()
    probe = FakeProbe(fake_volume, git_top=fake_volume)
    with pytest.raises(dbsnapshot.SnapshotError, match="git work tree"):
        dbsnapshot.snapshot(dest, datadir, probe, NOW)


def test_refuses_a_missing_folder_and_an_empty_data_folder(
    datadir: DataDir, probe: FakeProbe, dest: Path
) -> None:
    with pytest.raises(dbsnapshot.SnapshotError, match="not an existing folder"):
        dbsnapshot.snapshot(dest / "missing", datadir, probe, NOW)
    with pytest.raises(dbsnapshot.SnapshotError, match="no databases"):
        dbsnapshot.snapshot(dest, datadir, probe, NOW)


def test_never_reuses_a_folder(datadir: DataDir, probe: FakeProbe, dest: Path) -> None:
    _fill(datadir).close()
    dbsnapshot.snapshot(dest, datadir, probe, NOW)
    with pytest.raises(dbsnapshot.SnapshotError, match="already exists"):
        dbsnapshot.snapshot(dest, datadir, probe, NOW)


def test_a_failed_integrity_check_fails_the_run(
    datadir: DataDir, probe: FakeProbe, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill(datadir).close()
    monkeypatch.setattr(dbsnapshot, "_copy", lambda source, target: "page 2: corrupt")
    with pytest.raises(dbsnapshot.SnapshotError, match=r"integrity check failed for search\.db"):
        dbsnapshot.snapshot(dest, datadir, probe, NOW)


def test_cli_prints_the_folder_and_each_check_but_no_source_path(
    data_env: Path, probe: FakeProbe, dest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    assert cli.main(["db", "snapshot", "--to", str(dest)], probe=probe, clock=lambda: NOW) == 0
    out = capsys.readouterr().out
    assert f"snapshot: {dest.resolve() / FOLDER}" in out
    assert "search.db: integrity ok" in out
    assert str(data_env) not in out


def test_cli_reports_a_refusal(
    data_env: Path, fake_volume: Path, dest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = FakeProbe(fake_volume)
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    probe.git_toplevel = lambda path: path if path == dest.resolve() else None  # type: ignore[method-assign]
    assert cli.main(["db", "snapshot", "--to", str(dest)], probe=probe) == cli.EXIT_ERROR
    assert "git work tree" in capsys.readouterr().err
