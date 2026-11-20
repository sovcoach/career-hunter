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
    tier="high",
    key_requirements=("Examplish language required",),
)
CLEAN = Score(llm_output="{}", gaps="[]", tier="high")


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
    assert capsys.readouterr().err == "error: give --posting, --gap or --set-aside\n"


# --- set-aside postings (search migration 0034) ---------------------------------------------


def _set_aside(path: Path, title: str) -> int:
    conn = db.connect(path / "search.db")
    try:
        (posting_id,) = conn.execute("SELECT id FROM postings WHERE title = ?", (title,)).fetchone()
        with db.transaction(conn):
            for stamp in ("2026-11-01T10:00:00Z", "2026-11-01T11:00:00Z"):
                search_store.record_timeout(conn, int(posting_id), stamp, set_aside_after=2)
        return int(posting_id)
    finally:
        conn.close()


def _aside(path: Path) -> int:
    conn = db.connect_read_only(path / "search.db")
    try:
        return search_store.set_aside_count(conn)
    finally:
        conn.close()


def test_set_aside_lists_and_a_dry_run_writes_nothing(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    ident = _set_aside(scored, "Clean")
    assert cli.main(["rescore", "--set-aside", "--dry-run"], probe=probe) == cli.EXIT_OK
    assert capsys.readouterr().out == (
        f"{ident:5} Examplea — Clean: set aside after timeouts, fit None, not alerted\n"
        "dry run: nothing is written. 1 set-aside posting would be released.\n"
    )
    assert _aside(scored) == 1


def test_set_aside_releases_them_for_the_next_run(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_aside(scored, "Clean")
    assert cli.main(["rescore", "--set-aside"], probe=probe) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.endswith("1 set-aside posting released; the next watch run sends it again.\n")
    assert "postings queued" not in out
    assert _aside(scored) == 0
    assert _hashes(scored)["Clean"] == OLD.sha256  # releasing does not queue a re-score


def test_set_aside_with_none_says_so(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["rescore", "--set-aside"], probe=probe) == cli.EXIT_OK
    assert capsys.readouterr().out == (
        "0 set-aside postings released; the next watch run sends them again.\n"
    )


def test_a_posting_id_that_is_set_aside_is_released_and_queued(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    ident = _set_aside(scored, "Clean")
    assert cli.main(["rescore", "--posting", str(ident)], probe=probe) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "1 postings queued" in out and "1 set-aside posting released" in out
    assert _aside(scored) == 0
    assert _hashes(scored)["Clean"] is None


def test_an_unscored_posting_set_aside_is_released_by_its_id(
    scored: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = db.connect(scored / "search.db")
    try:
        with db.transaction(conn):
            conn.execute("UPDATE postings SET llm_output = NULL WHERE title = 'Phantom'")
    finally:
        conn.close()
    ident = _set_aside(scored, "Phantom")
    assert cli.main(["rescore", "--posting", str(ident)], probe=probe) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "not queued" not in out
    assert "0 postings queued" in out and "1 set-aside posting released" in out
    assert _aside(scored) == 0
