from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import claims, cli
from tests.conftest import ACTIVE, EARLIER, FIXTURES, FakeProbe, set_claims


def run(argv: list[str], probe: FakeProbe) -> int:
    return cli.main(argv, probe=probe)


def sealed_at(path: Path) -> str | None:
    file_claim = claims.read_claim_file(path)
    assert file_claim is not None
    return file_claim.sealed_at


@pytest.fixture
def inited(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert run(["init"], probe) == 0
    capsys.readouterr()
    return data_env


@pytest.fixture
def two_claims(inited: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    """An earlier claim's record exists, and config has moved on to the next claim."""
    set_claims(inited, EARLIER.label, EARLIER)
    assert run(["migrate"], probe) == 0
    set_claims(inited, ACTIVE.label, EARLIER, ACTIVE)
    capsys.readouterr()
    return inited


@pytest.fixture
def no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(prompt: str = "") -> str:
        raise AssertionError("asked for confirmation when it should have refused first")

    monkeypatch.setattr("builtins.input", refuse)


def test_refuses_without_env(
    probe: FakeProbe, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CAREERHUNTER_DATA_DIR", raising=False)
    assert run(["doctor"], probe) == cli.EXIT_GUARD
    assert "REFUSING TO START" in capsys.readouterr().err


def test_init_refuses_when_volume_unmounted(fake_volume: Path, data_env: Path) -> None:
    assert run(["init"], FakeProbe(fake_volume, mounted=False)) == cli.EXIT_GUARD
    assert not data_env.exists()


def test_init_without_a_claim(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["init"], probe) == 0
    out = capsys.readouterr().out
    for name in ("config.yaml", "exclusions.yaml", "seed.yaml", "search.db"):
        assert (data_env / name).is_file()
    assert (data_env / "config.yaml").stat().st_mode & 0o777 == 0o600
    assert (data_env / "search.db").stat().st_mode & 0o777 == 0o600
    assert not (data_env / "careerhunter.db").exists()
    assert list(data_env.glob("ei_record_*")) == []
    assert "no active EI claim" in out


def test_full_flow(inited: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> None:
    set_claims(inited, ACTIVE.label, ACTIVE)
    assert run(["migrate"], probe) == 0
    out = capsys.readouterr().out
    assert "ei_record_2026-27.db: created; schema version 1" in out
    assert (inited / "ei_record_2026-27.db").stat().st_mode & 0o777 == 0o600

    shutil.copyfile(FIXTURES / "seed_synthetic.yaml", inited / "seed.yaml")
    assert run(["seed"], probe) == 0
    first = capsys.readouterr().out
    assert "EI claim 2026-27 (from 2026-08-05)" in first
    assert "search.db\n" in first and "ei_record_2026-27.db\n" in first
    assert "  applications         4 inserted     0 already present" in first
    assert "  activities           3 inserted     0 already present" in first
    assert "warning: applications[2] (Initech / Project Coordinator): date unknown" in first
    assert run(["seed"], probe) == 0
    assert "  applications         0 inserted     4 already present" in capsys.readouterr().out

    assert (
        run(["export", "--claim", "2026-27", "--from", "2026-09-01", "--to", "2026-09-30"], probe)
        == 0
    )
    assert "EI claim 2026-27: 3 applications, 3 activities" in capsys.readouterr().out
    assert len(list((inited / "exports").glob("ei_2026-27_2026-09-01_2026-09-30_*"))) == 3

    assert run(["doctor"], probe) == 0
    out = capsys.readouterr().out
    assert "config.yaml     OK" in out
    assert "claim 2026-27   OK  active, from 2026-08-05" in out
    assert "reconcile       OK  search.db and 1 claim record agree" in out
    assert "telegram_bot_token: missing" in out

    assert run(["init"], probe) == 0  # re-running init keeps existing files
    assert "kept" in capsys.readouterr().out


def test_seed_out_of_claim_is_refused_unless_routed(
    inited: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    set_claims(inited, ACTIVE.label, ACTIVE)
    doc = yaml.safe_load((FIXTURES / "seed_synthetic.yaml").read_text())
    doc["applications"][3]["date_applied"] = "2026-07-01"  # typo, or genuinely earlier
    (inited / "seed.yaml").write_text(yaml.safe_dump(doc))
    assert run(["seed"], probe) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "2026-07-01 is before claim 2026-27 started (2026-08-05)" in err
    assert "--route-out-of-claim-to-search" in err
    assert run(["seed", "--route-out-of-claim-to-search"], probe) == 0
    out = capsys.readouterr().out
    assert "search.db only: applications[3] (Umbrella Analytics / Data Clerk): 2026-07-01" in out
    assert "  applications         3 inserted     0 already present" in out  # the EI file


def test_seed_without_a_claim(
    inited: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = yaml.safe_load((FIXTURES / "seed_synthetic.yaml").read_text())
    del doc["activities"]
    (inited / "seed.yaml").write_text(yaml.safe_dump(doc))
    assert run(["seed"], probe) == 0
    out = capsys.readouterr().out
    assert "no active EI claim" in out and "ei_record" not in out
    assert list(inited.glob("ei_record_*")) == []


def test_export_reads_only_the_claim_file(
    inited: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    set_claims(inited, ACTIVE.label, ACTIVE)
    shutil.copyfile(FIXTURES / "seed_synthetic.yaml", inited / "seed.yaml")
    assert run(["seed"], probe) == 0
    search = inited / "search.db"
    os.chmod(search, 0o000)  # search.db can't even be opened now
    try:
        assert (
            run(
                ["export", "--claim", "2026-27", "--from", "2026-09-01", "--to", "2026-09-30"],
                probe,
            )
            == 0
        )
    finally:
        os.chmod(search, 0o600)
    assert "3 applications" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("label", "message"),
    [("2024-25", "no record for claim 2024-25"), ("../x", "not a claim label")],
)
def test_export_needs_a_valid_existing_claim(
    inited: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str], label: str, message: str
) -> None:
    argv = ["export", "--claim", label, "--from", "2026-09-01", "--to", "2026-09-30"]
    assert run(argv, probe) == cli.EXIT_ERROR
    assert message in capsys.readouterr().err


# --- Sealing an earlier claim ---------------------------------------------------------


@pytest.mark.parametrize("command", [["init"], ["migrate"], ["seed"]])
def test_writes_are_refused_while_an_earlier_claim_is_unsealed(
    two_claims: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    assert run(command, probe) == cli.EXIT_ERROR
    assert "Run: careerhunter claim seal 2025-26" in capsys.readouterr().err
    assert not (two_claims / "ei_record_2026-27.db").exists()


def test_claim_seal_flow(
    two_claims: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = two_claims / "ei_record_2025-26.db"
    monkeypatch.setattr("builtins.input", lambda prompt="": "2025-62")
    assert run(["claim", "seal", "2025-26"], probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert "This will permanently close the record for EI claim 2025-26" in captured.out
    assert "Nothing was sealed" in captured.err
    assert sealed_at(path) is None

    monkeypatch.setattr("builtins.input", lambda prompt="": "2025-26")
    assert run(["claim", "seal", "2025-26"], probe) == 0
    assert "sealed" in capsys.readouterr().out
    assert sealed_at(path)
    assert path.stat().st_mode & 0o777 == 0o400

    assert run(["migrate"], probe) == 0
    assert "ei_record_2026-27.db: created" in capsys.readouterr().out
    assert run(["doctor"], probe) == 0
    out = capsys.readouterr().out
    assert "claim 2025-26   OK  sealed" in out and "keep until 2032-06-02" in out
    argv = ["export", "--claim", "2025-26", "--from", "2025-06-04", "--to", "2026-06-02"]
    assert run(argv, probe) == 0


def test_claim_seal_without_confirmation_input(
    two_claims: Path, probe: FakeProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert run(["claim", "seal", "2025-26"], probe) == cli.EXIT_ERROR
    assert sealed_at(two_claims / "ei_record_2025-26.db") is None


def test_the_active_claim_cannot_be_sealed(
    two_claims: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str], no_prompt: None
) -> None:
    assert run(["claim", "seal", "2026-27"], probe) == cli.EXIT_ERROR
    assert "is the active claim" in capsys.readouterr().err


def _both_files(data: Path, probe: FakeProbe) -> None:
    """Records for both claims, the earlier one still unsealed."""
    set_claims(data, EARLIER.label, EARLIER)
    assert run(["migrate"], probe) == 0
    os.rename(data / "ei_record_2025-26.db", data / "hold.db")
    set_claims(data, ACTIVE.label, ACTIVE)
    assert run(["migrate"], probe) == 0
    os.rename(data / "hold.db", data / "ei_record_2025-26.db")


@pytest.mark.parametrize(
    ("active", "entries", "message"),
    [
        ("2026-72", [EARLIER, ACTIVE], "is not one of the claims listed"),
        ("2027-28", [EARLIER, ACTIVE], "is not one of the claims listed"),
        # The earlier claim's label by mistake: the current claim then lacks an end date.
        ("2025-26", [EARLIER, ACTIVE], "2026-27 is not the active claim, so it needs an end date"),
        (None, [EARLIER, ACTIVE], "2026-27 is not the active claim, so it needs an end date"),
    ],
    ids=["typo", "next year", "earlier label", "null"],
)
def test_a_mistyped_active_claim_cannot_seal_anything(
    inited: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    no_prompt: None,
    active: Any,
    entries: list[Any],
    message: str,
) -> None:
    _both_files(inited, probe)
    set_claims(inited, active, *entries)
    capsys.readouterr()
    for label in ("2025-26", "2026-27"):
        assert run(["claim", "seal", label], probe) == cli.EXIT_ERROR
        assert message in capsys.readouterr().err
    for label in ("2025-26", "2026-27"):
        path = inited / f"ei_record_{label}.db"
        assert sealed_at(path) is None
        assert path.stat().st_mode & 0o777 == 0o600


# --- Errors ---------------------------------------------------------------------------


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
