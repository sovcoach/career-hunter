"""`careerhunter status`: one application's status, dated, in both layers, the same as
the bot's /status. Synthetic companies and roles only, against temporary databases."""

from __future__ import annotations

from pathlib import Path

import pytest

from careerhunter import cli, db, ledger
from tests.conftest import ACTIVE, FakeProbe, set_claims
from tests.test_import_cli import entry, run, write

URL = "https://careers.example.com/jobs/77"


@pytest.fixture
def two(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    assert cli.main(["migrate"], probe=probe) == 0
    path = write(
        data_env,
        entry(url=URL),
        entry(role="Data Analyst", date="2026-09-17", url=None, resolve="new"),
    )
    assert run(probe, path) == cli.EXIT_OK
    capsys.readouterr()
    return data_env


def status(probe: FakeProbe, *args: str) -> int:
    return cli.main(["status", *args], probe=probe)


def _histories(ready: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    ei = db.connect_read_only(ready / f"ei_record_{ACTIVE.label}.db")
    search = db.connect_read_only(ready / "search.db")
    try:
        e = [tuple(r) for r in ei.execute("SELECT status, status_date FROM status_history")]
        s = [tuple(r) for r in search.execute("SELECT status, status_date FROM decision_history")]
        assert ledger.reconcile(search, {ACTIVE.label: ei}) == []
        return e, s  # type: ignore[return-value]
    finally:
        ei.close()
        search.close()


def test_a_dry_run_says_what_would_be_written_and_writes_nothing(
    two: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _histories(two)
    assert status(probe, "rejected", "--on", "2026-09-20", "--url", URL, "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.startswith("dry run: nothing is written. Would record app 1 (2026-09-15, ")
    assert out.endswith(f"rejected on 2026-09-20, in search.db and EI claim {ACTIVE.label}.\n")
    assert _histories(two) == before


def test_a_status_is_recorded_on_its_day_in_both_layers(
    two: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--company", "example robotics inc.", "--title", "DATA ANALYST"]
    assert status(probe, "Position Closed", "--on", "2026-09-30", *args) == 0
    assert capsys.readouterr().out.startswith("Recorded app 2 (2026-09-17, ")
    ei, search = _histories(two)
    assert ei[-1] == ("position_closed", "2026-09-30")
    assert search[-1] == ("position_closed", "2026-09-30")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["ghosted", "--id", "1"], "the status must be one of"),
        (["applied", "--id", "1"], "the status must be one of"),
        (["rejected"], "give --id, or --url, or --company and --title"),
        (["rejected", "--id", "1", "--url", URL], "give --id, or --url"),
        (["rejected", "--company", "Example Robotics Inc."], "give --id, or --url"),
        (["rejected", "--id", "9"], "no application matches"),
        (
            ["rejected", "--company", "Example Robotics Inc.", "--title", "Nobody"],
            "no application matches",
        ),
        (["rejected", "--id", "1", "--on", "2026-09-01"], "before the application (2026-09-15)"),
        (["rejected", "--id", "1", "--on", "2099-01-01"], "in the future"),
    ],
)
def test_requests_that_cannot_be_recorded_write_nothing(
    two: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    before = _histories(two)
    assert status(probe, *args) == cli.EXIT_ERROR
    assert message in capsys.readouterr().err
    assert _histories(two) == before


def test_two_matches_need_the_applied_date(
    two: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(two, entry(date="2026-09-22", url=None, resolve="new"))
    assert run(probe, path) == cli.EXIT_OK
    capsys.readouterr()
    names = ["--company", "Example Robotics Inc.", "--title", "Analyst, Client Experience"]
    assert status(probe, "rejected", "--on", "2026-09-25", *names) == cli.EXIT_ERROR
    assert "2 applications match" in capsys.readouterr().err
    assert status(probe, "rejected", "--on", "2026-09-25", *names, "--applied", "2026-09-22") == 0
    assert _histories(two)[0][-1] == ("rejected", "2026-09-25")
