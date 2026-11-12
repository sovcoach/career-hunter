"""`careerhunter jd unavailable`: pick one application, mark its JD unavailable, with why.
Synthetic companies and roles only, against temporary databases."""

from __future__ import annotations

from pathlib import Path

import pytest

from careerhunter import cli, db, ledger
from tests.conftest import ACTIVE, FakeProbe, set_claims
from tests.test_import_cli import entry, run, search_rows, write
from tests.test_ledger_jd import application

GONE = "posting removed before capture"
URL = "https://careers.example.com/jobs/77?utm_source=feed"


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    assert cli.main(["migrate"], probe=probe) == 0
    capsys.readouterr()
    return data_env


@pytest.fixture
def two(ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    path = write(
        ready,
        entry(url="https://careers.example.com/jobs/77"),
        entry(role="Data Analyst", date="2026-09-17", url=None, resolve="new"),
    )
    assert run(probe, path) == cli.EXIT_OK
    capsys.readouterr()
    return ready


def _ei_markers(ready: Path) -> list[str]:
    conn = db.connect_read_only(ready / f"ei_record_{ACTIVE.label}.db")
    try:
        return [str(r[0]) for r in conn.execute("SELECT explanation FROM jd_unavailable")]
    finally:
        conn.close()


def jd(probe: FakeProbe, *args: str) -> int:
    return cli.main(["jd", "unavailable", *args], probe=probe)


def test_mark_by_link(two: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> None:
    assert jd(probe, "--url", URL, "--reason", f"  {GONE} ") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.endswith(
        f"Marked: Example Robotics Inc. — Analyst, Client Experience (EI claim {ACTIVE.label}): "
        f"job description unavailable ({GONE}).\n"
    )
    assert _ei_markers(two) == [GONE]
    assert search_rows(
        two, "SELECT count(*) FROM applications WHERE jd_unavailable_reason IS NOT NULL"
    ) == [(1,)]
    assert jd(probe, "--url", URL, "--reason", GONE) == cli.EXIT_OK
    assert "Already marked" in capsys.readouterr().out
    assert _ei_markers(two) == [GONE]


def test_mark_by_company_title_and_date(
    two: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--company", "example robotics inc.", "--title", "DATA ANALYST", "--date", "2026-09-17"]
    assert jd(probe, *args, "--reason", GONE) == cli.EXIT_OK
    assert "Data Analyst" in capsys.readouterr().out
    assert _ei_markers(two) == [GONE]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--company", "Example Robotics Inc.", "--reason", GONE], "give --url, or all of"),
        (["--url", URL, "--title", "x", "--reason", GONE], "not both"),
        (
            ["--url", "https://careers.example.com/other", "--reason", GONE],
            "no application matches",
        ),
        (["--url", URL, "--reason", "   "], "1 to 200 characters"),
    ],
)
def test_refusals_write_nothing(
    two: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    assert jd(probe, *args) == cli.EXIT_ERROR
    assert message in capsys.readouterr().err
    assert _ei_markers(two) == []


def test_more_than_one_match_is_refused(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    search = db.connect(ready / "search.db")
    try:
        for number in (1, 2):  # the same role twice, search.db only, under two links
            ledger.record_application(
                search, None, application(url=f"https://careers.example.com/jobs/{number}")
            )
    finally:
        search.close()
    args = ["--company", "Example Robotics", "--title", "Client Experience Analyst"]
    assert jd(probe, *args, "--date", "2026-09-15", "--reason", GONE) == cli.EXIT_ERROR
    assert "2 applications match" in capsys.readouterr().err
    assert search_rows(
        ready, "SELECT count(*) FROM applications WHERE jd_unavailable_reason IS NOT NULL"
    ) == [(0,)]


def test_an_application_with_a_jd_is_refused(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = write(
        ready, entry(url="https://careers.example.com/jobs/9", jd_text="Synthetic duties.")
    )
    assert run(probe, path) == cli.EXIT_OK
    capsys.readouterr()
    assert jd(probe, "--url", "https://careers.example.com/jobs/9", "--reason", GONE) == 1
    assert "already has a job description" in capsys.readouterr().err
