from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from careerhunter import cli
from tests.conftest import FIXTURES, FakeProbe


def run(argv: list[str], probe: FakeProbe) -> int:
    return cli.main(argv, probe=probe)


def test_refuses_without_env(
    probe: FakeProbe, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CAREERHUNTER_DATA_DIR", raising=False)
    assert run(["doctor"], probe) == cli.EXIT_GUARD
    assert "REFUSING TO START" in capsys.readouterr().err


def test_init_refuses_when_volume_unmounted(fake_volume: Path, data_env: Path) -> None:
    assert run(["init"], FakeProbe(fake_volume, mounted=False)) == cli.EXIT_GUARD
    assert not data_env.exists()


def test_full_flow(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["init"], probe) == 0
    for name in ("config.yaml", "exclusions.yaml", "seed.yaml", "careerhunter.db"):
        assert (data_env / name).is_file()
    assert (data_env / "config.yaml").stat().st_mode & 0o777 == 0o600

    shutil.copyfile(FIXTURES / "seed_synthetic.yaml", data_env / "seed.yaml")
    assert run(["seed"], probe) == 0
    first = capsys.readouterr().out
    assert "applications       4 inserted     0 already present" in first
    assert run(["seed"], probe) == 0
    assert "applications       0 inserted     4 already present" in capsys.readouterr().out

    assert run(["export", "--from", "2026-09-01", "--to", "2026-09-30"], probe) == 0
    assert len(list((data_env / "exports").iterdir())) == 3

    assert run(["migrate"], probe) == 0
    assert run(["doctor"], probe) == 0
    out = capsys.readouterr().out
    assert "config.yaml     OK" in out
    assert "telegram_bot_token: missing" in out

    assert run(["init"], probe) == 0  # re-running init keeps existing files
    assert "kept" in capsys.readouterr().out


def test_doctor_reports_bad_config(data_env: Path, probe: FakeProbe) -> None:
    assert run(["init"], probe) == 0
    (data_env / "config.yaml").write_text("search: nope\n")
    assert run(["doctor"], probe) == cli.EXIT_ERROR


def test_bad_seed_is_a_clean_error(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["init"], probe) == 0
    (data_env / "seed.yaml").write_text("applications: [{title: X}]\n")
    assert run(["seed"], probe) == cli.EXIT_ERROR
    assert "company is required" in capsys.readouterr().err


def test_filevault_warning(
    data_env: Path, fake_volume: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["init"], FakeProbe(fake_volume, filevault=False)) == 0
    assert "FileVault is OFF" in capsys.readouterr().err
    assert run(["doctor"], FakeProbe(fake_volume, filevault=None)) == 0
    assert "FileVault is UNKNOWN" in capsys.readouterr().err
