"""`careerhunter rescore`: queue chosen scored postings for the next watch run.
Synthetic boards, postings and gaps; "Examplish" is a made-up language. No sockets."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careerhunter import cli, db, search_store
from careerhunter import watch as w
from careerhunter.filters import PostingText
from careerhunter.score import Score
from tests.conftest import FakeProbe
from tests.test_rescore import OLD, board, lever, watched
from tests.test_watch import DAY, OnBoard

PHANTOM = Score(
    llm_output="{}",
    gaps='[{"name":"Examplish language proficiency","kind":"core"}]',
    tier="strong",
    key_requirements=("Examplish language required",),
)
CLEAN = Score(llm_output="{}", gaps="[]", tier="strong")


@pytest.fixture
def scored(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    conn = db.connect(data_env / "search.db")
    db.migrate(conn, "search")
    replies = {"Phantom": PHANTOM, "Clean": CLEAN, "Skipped": PHANTOM}

    def scorer(text: PostingText) -> Score:
        return replies[text.title]

    jobs = lever("examplea", ("a1", "Phantom"), ("a2", "Clean"), ("a3", "Skipped"))
    w.run(
        conn,
        watched("examplea"),
        today=DAY,
        fetcher=OnBoard({board("examplea"): jobs}),
        scorer=scorer,
        resume=OLD,
    )
    (skipped,) = conn.execute("SELECT id FROM postings WHERE title = 'Skipped'").fetchone()
    search_store.insert_skip(conn, int(skipped), DAY, "not a fit", by_operator=True)
    conn.close()
    capsys.readouterr()
    return data_env


def _hashes(path: Path) -> dict[str, str | None]:
    conn = db.connect_read_only(path / "search.db")
    try:
        rows = conn.execute("SELECT title, scored_resume_sha256 FROM postings").fetchall()
        return {str(r[0]): r[1] for r in rows}
    finally:
        conn.close()


def test_a_dry_run_lists_the_unskipped_postings_with_the_gap_and_writes_nothing(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["rescore", "--gap", "examplish", "--dry-run"], probe=probe) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("Examplea — Phantom: fit None, not alerted")
    assert out[-1] == "dry run: nothing is written. 1 postings would be re-scored."
    assert set(_hashes(scored).values()) == {OLD.sha256}


def test_queued_postings_are_re_scored_by_the_next_run_and_only_they(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["rescore", "--gap", "Examplish"], probe=probe) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("1 postings queued")
    assert _hashes(scored)["Phantom"] is None
    calls: list[str] = []

    def scorer(text: PostingText) -> Score:
        calls.append(text.title)
        return CLEAN

    conn = db.connect(scored / "search.db")
    w.run(conn, [], today=DAY, fetcher=OnBoard({}), scorer=scorer, resume=OLD)
    gaps = conn.execute("SELECT gaps FROM postings WHERE title = 'Phantom'").fetchone()[0]
    conn.close()
    assert calls == ["Phantom"]
    assert gaps == "[]"
    assert _hashes(scored)["Phantom"] == OLD.sha256


def test_an_id_that_is_skipped_or_unknown_is_named_and_not_queued(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    conn: sqlite3.Connection = db.connect_read_only(scored / "search.db")
    ids = dict(conn.execute("SELECT title, id FROM postings").fetchall())
    conn.close()
    args = ["rescore", "--posting", str(ids["Clean"]), "--posting", str(ids["Skipped"])]
    assert cli.main([*args, "--posting", "999"], probe=probe) == cli.EXIT_ERROR
    out = capsys.readouterr().out
    assert f"{ids['Skipped']:5} not queued" in out and "  999 not queued" in out
    assert _hashes(scored) == {"Phantom": OLD.sha256, "Clean": None, "Skipped": OLD.sha256}


def test_nothing_to_pick_is_an_error(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["rescore"], probe=probe) == cli.EXIT_ERROR
    assert capsys.readouterr().err == "error: give --posting, or --gap\n"
