"""`careerhunter import [--dry-run]`: lines, exit codes and the dry run's promise.

Synthetic companies, roles and job descriptions only, against temporary databases.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, db
from careerhunter.jobdesc import sha256
from tests.conftest import ACTIVE, EARLIER, FakeProbe, set_claims
from tests.test_jobdesc import make_pdf

JD = "About the role\nSYNTHETIC-JD-TEXT Example Robotics is looking for an analyst."
LINKEDIN = "https://www.linkedin.com/jobs/view/4000000001/?refId=abc123&trk=public_jobs_topcard"
LINKEDIN_CANONICAL = "https://www.linkedin.com/jobs/view/4000000001/"


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    assert cli.main(["migrate"], probe=probe) == 0
    capsys.readouterr()
    return data_env


def entry(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "employer": "Example Robotics Inc.",
        "role": "Analyst, Client Experience",
        "date": "2026-09-15",
        "date_precision": "day",
        "method": "linkedin_easy_apply",
    }
    base.update(fields)
    return base


def write(ready: Path, *entries: dict[str, Any]) -> Path:
    path = ready / "imports" / "backfill.json"
    path.write_text(json.dumps({"version": 1, "applications": list(entries)}))
    return path


def run(probe: FakeProbe, path: Path, *extra: str) -> int:
    return cli.main(["import", str(path), *extra], probe=probe)


def state(ready: Path) -> tuple[Any, ...]:
    """Every row in both layers, and every file in the data folder but the databases'."""
    out: list[Any] = []
    for name in ("search.db", f"ei_record_{ACTIVE.label}.db"):
        conn = db.connect_read_only(ready / name)
        try:
            tables = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            out.extend(sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}")) for t in tables)  # noqa: S608
        finally:
            conn.close()
    files = sorted(str(p.relative_to(ready)) for p in ready.rglob("*") if ".db" not in p.name)
    return (*out, files)


def search_rows(ready: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = db.connect_read_only(ready / "search.db")
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def test_the_dry_run_writes_nothing_and_the_run_applies_its_lines(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(
        ready,
        entry(url=LINKEDIN, jd_text=JD, notes="Synthetic note."),
        entry(
            employer="Synthetic Widgets",
            role="Data Analyst",
            date="2026-09-17",
            date_precision="week",
            method="company_site",
        ),
    )
    before = state(ready)
    assert run(probe, path, "--dry-run") == cli.EXIT_OK
    dry = capsys.readouterr().out
    assert state(ready) == before
    assert dry.startswith("dry run: nothing is written. EI claim 2026-27")
    lines = dry.splitlines()[1:]
    assert lines[0] == (
        "  1 CREATE    search.db: Example Robotics Inc. / Analyst, Client Experience, "
        f"2026-09-15, linkedin_easy_apply, link {LINKEDIN_CANONICAL}, notes (15 characters), "
        f"JD sha256 {sha256(JD.encode())} ({len(JD)} characters); EI: the same without notes, "
        f"JD sha256 {sha256(JD.encode())} ({len(JD)} characters)"
    )
    assert "Synthetic Widgets / Data Analyst, the week of 2026-09-14" in lines[1]
    assert "no JD (flagged as missing)" in lines[1]
    assert lines[2] == "2 create"

    assert run(probe, path) == cli.EXIT_OK
    applied = capsys.readouterr().out
    assert applied.splitlines()[1:] == lines  # exactly the lines the dry run printed
    assert search_rows(ready, "SELECT url FROM postings ORDER BY id") == [
        (LINKEDIN_CANONICAL,),
        (None,),
    ]


def test_a_second_run_is_all_unchanged(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(ready, entry(jd_text=JD), entry(employer="Synthetic Widgets", resolve="new"))
    assert run(probe, path) == cli.EXIT_OK
    capsys.readouterr()
    assert run(probe, path) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == "2 unchanged"
    assert (
        "  2 UNCHANGED Synthetic Widgets / Analyst, Client Experience, 2026-09-15 -> app 2" in out
    )
    assert "already recorded" in out


def test_the_jd_text_is_never_printed(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(ready, entry(jd_text=JD))
    for extra in (["--dry-run"], []):
        run(probe, path, *extra)
        captured = capsys.readouterr()
        assert "SYNTHETIC-JD-TEXT" not in captured.out + captured.err


def test_anything_left_to_resolve_exits_1_after_the_rest_are_applied(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(
        ready,
        entry(jd_text=JD),
        entry(employer="Synthetic Widgets", date="2099-01-01"),
        entry(employer="Synthetic Gears", date="2026-07-01"),  # before the claim
    )
    assert run(probe, path) == cli.EXIT_ERROR
    out = capsys.readouterr().out
    assert (
        "  2 REFUSED   Synthetic Widgets / Analyst, Client Experience: "
        "date 2099-01-01 is in the future"
    ) in out
    assert "  3 REFUSED   Synthetic Gears / Analyst, Client Experience, 2026-07-01: " in out
    assert out.splitlines()[-1] == "1 create, 2 refused"
    assert search_rows(ready, "SELECT count(*) FROM applications") == [(1,)]
    assert run(probe, path, "--route-out-of-claim-to-search") == cli.EXIT_ERROR  # 2 still
    out = capsys.readouterr().out
    assert "  3 CREATE    search.db: Synthetic Gears" in out
    assert "EI: nothing (search.db only)" in out
    assert "note: 2026-07-01 is before claim 2026-27 started" in out


def test_ambiguous_entries_list_their_candidates(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    first = write(
        ready,
        entry(role="Client Experience Analyst", date="2026-09-01", resolve="new"),
        entry(role="Client Experience Analyst", date="2026-09-02", resolve="new"),
    )
    assert run(probe, first) == cli.EXIT_OK
    capsys.readouterr()
    second = write(ready, entry(date="2026-09-03", jd_text=JD))
    assert run(probe, second, "--dry-run") == cli.EXIT_ERROR
    out = capsys.readouterr().out.splitlines()
    assert out[1].startswith("  1 AMBIGUOUS Example Robotics Inc. / Analyst, Client Experience")
    assert out[2] == (
        "              candidate app 1 (2026-09-01, Example Robotics Inc. / "
        "Client Experience Analyst)"
    )
    assert out[3].startswith("              candidate app 2 (2026-09-02")


def test_attach_names_the_application_and_the_layers(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(probe, write(ready, entry())) == cli.EXIT_OK
    capsys.readouterr()
    assert run(probe, write(ready, entry(jd_text=JD))) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert (
        "  1 ATTACH    app 1 (2026-09-15, Example Robotics Inc. / Analyst, Client Experience) "
        f"gets, in search.db and EI claim 2026-27: JD sha256 {sha256(JD.encode())}"
    ) in out


def test_a_file_outside_imports_is_a_guard_refusal(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ready / "backfill.json"
    path.write_text(json.dumps({"version": 1, "applications": [entry()]}))
    assert run(probe, path) == cli.EXIT_GUARD
    assert "REFUSING TO IMPORT: the import file must be under imports/" in capsys.readouterr().err


def test_an_unsealed_earlier_claim_is_a_guard_refusal(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, EARLIER.label, EARLIER)
    assert cli.main(["migrate"], probe=probe) == 0  # the earlier claim's record
    set_claims(data_env, ACTIVE.label, EARLIER, ACTIVE)
    capsys.readouterr()
    ready = data_env
    path = write(ready, entry())
    for extra in (["--dry-run"], []):
        assert run(probe, path, *extra) == cli.EXIT_GUARD
        err = capsys.readouterr().err
        assert "REFUSING TO IMPORT until these are fixed:" in err
        assert f"careerhunter claim seal {EARLIER.label}" in err


def test_a_bad_file_exits_1_with_every_problem(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ready / "imports" / "bad.json"
    path.write_text('{"version": 2, "applications": [], "extra": 1}')
    assert run(probe, path) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "import file has 3 problem(s)" in err


def test_a_dry_run_needs_migrated_databases(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write(ready, entry())
    real = db.latest_version
    monkeypatch.setattr(db, "latest_version", lambda track: real(track) + 1)
    assert run(probe, path, "--dry-run") == cli.EXIT_ERROR
    assert "search.db needs a migration first" in capsys.readouterr().err


def test_a_dry_run_before_init_refuses(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    (data_env / "search.db").unlink()
    for leftover in data_env.glob("search.db-*"):
        leftover.unlink()
    capsys.readouterr()
    path = data_env / "imports" / "backfill.json"
    path.write_text(json.dumps({"version": 1, "applications": [entry()]}))
    assert run(probe, path, "--dry-run") == cli.EXIT_ERROR
    assert "search.db doesn't exist yet" in capsys.readouterr().err


def test_a_dry_run_with_no_claim_file_yet_plans_for_search_and_ei(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0  # no claim yet, so no record file
    set_claims(data_env, ACTIVE.label, ACTIVE)
    capsys.readouterr()
    path = data_env / "imports" / "backfill.json"
    path.write_text(json.dumps({"version": 1, "applications": [entry()]}))
    assert run(probe, path, "--dry-run") == cli.EXIT_OK
    assert "EI: the same without notes" in capsys.readouterr().out
    assert not (data_env / f"ei_record_{ACTIVE.label}.db").exists()


def test_without_a_claim_the_run_says_search_only(
    data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    path = data_env / "imports" / "backfill.json"
    path.write_text(json.dumps({"version": 1, "applications": [entry()]}))
    assert run(probe, path) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("No active EI claim: applications go to search.db only.")


def test_a_refusal_while_applying_is_printed(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from careerhunter import backfill, selfsourced

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise selfsourced.LogError("Synthetic refusal.")

    monkeypatch.setattr(backfill.selfsourced, "record", refused)
    assert run(probe, write(ready, entry())) == cli.EXIT_ERROR
    assert "  1 REFUSED   not written: Synthetic refusal." in capsys.readouterr().out


def test_a_search_only_attach_says_so(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(ready, entry(date="2026-07-01"))
    assert run(probe, path, "--route-out-of-claim-to-search") == cli.EXIT_OK
    capsys.readouterr()
    (ready / "imports" / "scan.txt").write_text(JD)
    second = write(
        ready, entry(date="2026-07-01", jd_file="scan.txt", url="https://jobs.example.com/3")
    )
    assert run(probe, second) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "gets, in search.db: link https://jobs.example.com/3, JD sha256" in out
    conn = sqlite3.connect(ready / "search.db")
    try:
        assert conn.execute("SELECT ei_claim FROM applications").fetchall() == [(None,)]
    finally:
        conn.close()


def test_the_example_file_is_a_valid_import(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    example = Path(__file__).resolve().parents[1] / "import.example.json"
    (ready / "imports" / "backfill.json").write_text(example.read_text(encoding="utf-8"))
    folder = ready / "imports" / "job-descriptions"
    folder.mkdir()
    (folder / "synthetic-widgets-data-analyst.pdf").write_bytes(
        make_pdf(["Synthetic duties for a data analyst."])
    )
    assert run(probe, ready / "imports" / "backfill.json", "--dry-run") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"link {LINKEDIN_CANONICAL}" in out
    assert "Synthetic Widgets / Data Analyst, the week of 2026-09-14" in out
    assert "JD unavailable (posting removed before capture)" in out
    assert out.splitlines()[-1] == "4 create"
