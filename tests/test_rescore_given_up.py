"""`careerhunter rescore --given-up` and `--posting <id>` release a posting given up
after replies that did not parse (Batch R), with `--dry-run`. Synthetic only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careerhunter import cli, db
from careerhunter import watch as w
from tests.conftest import FakeProbe


def _given_up(root: Path, title: str, *, given_up: bool = True) -> int:
    conn = db.connect(root / "search.db")
    try:
        db.migrate(conn, "search")
        conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Robotics', 'manual') "
            "ON CONFLICT DO NOTHING"
        )
        cur = conn.execute(
            "INSERT INTO postings (company_id, title, url, first_seen, source) "
            "VALUES (1, ?, ?, '2026-11-02', 'watcher')",
            (title, f"https://jobs.example.com/{title.replace(' ', '-')}"),
        )
        posting_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO score_parse_failures (posting_id, failures, last_failed_at, given_up_at) "
            "VALUES (?, 3, '2026-11-02T10:00:00Z', ?)",
            (posting_id, "2026-11-02T10:00:00Z" if given_up else None),
        )
        return posting_id
    finally:
        conn.close()


def _failures(root: Path) -> list[int]:
    conn = db.connect_read_only(root / "search.db")
    try:
        return [int(r[0]) for r in conn.execute("SELECT posting_id FROM score_parse_failures")]
    finally:
        conn.close()


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    return data_env


def test_a_dry_run_lists_the_given_up_postings_and_writes_nothing(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _given_up(ready, "Client Clerk")
    _given_up(ready, "Still Counting", given_up=False)
    assert cli.main(["rescore", "--given-up", "--dry-run"], probe=probe) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"{first:5} Example Robotics — Client Clerk: given up after replies that did not "
        "parse, fit None, not alerted",
        "dry run: nothing is written. 1 given-up posting would be released.",
    ]
    assert len(_failures(ready)) == 2


def test_given_up_releases_every_given_up_posting_and_only_those(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _given_up(ready, "Client Clerk")
    _given_up(ready, "Second Clerk")
    counting = _given_up(ready, "Still Counting", given_up=False)
    assert cli.main(["rescore", "--given-up"], probe=probe) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "2 given-up postings released; the next watch run sends them to the model again."
    )
    assert _failures(ready) == [counting]  # a count still under the limit stays


def test_a_given_up_posting_id_is_released_by_posting(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _given_up(ready, "Client Clerk")
    other = _given_up(ready, "Second Clerk")
    assert cli.main(["rescore", "--posting", str(first)], probe=probe) == 0
    out = capsys.readouterr().out
    assert "1 given-up posting released; the next watch run sends it to the model" in out
    assert _failures(ready) == [other]


def test_a_released_posting_is_scored_again(ready: Path, probe: FakeProbe) -> None:
    posting = _given_up(ready, "Client Clerk")
    assert cli.main(["rescore", "--given-up"], probe=probe) == 0
    conn: sqlite3.Connection = db.connect(ready / "search.db")
    try:
        queued = [int(r["id"]) for r in conn.execute(w._NEVER_SCORED_SQL)]
    finally:
        conn.close()
    assert posting in queued
