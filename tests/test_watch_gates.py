"""Gates on every board the watcher fetches (#32). Synthetic boards only; no sockets."""

from __future__ import annotations

import sqlite3

from careerhunter import watch as w
from careerhunter.fetch import FetchTooLarge
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

GREENHOUSE = company("Example Robotics", "greenhouse", "examplerobotics")


def _skip_reasons(conn: sqlite3.Connection) -> list[str]:
    return [str(row["reason"]) for row in conn.execute("SELECT reason FROM skips ORDER BY id")]


def test_greenhouse_is_fetched_with_its_descriptions(search: sqlite3.Connection) -> None:
    board = url("greenhouse", "examplerobotics")
    assert board.endswith("/jobs?content=true")
    payload = one_greenhouse("examplerobotics")
    payload["jobs"][0]["content"] = (  # type: ignore[index]
        "&lt;p&gt;Fluency in &lt;b&gt;French&lt;/b&gt; is required.&lt;/p&gt;"
    )
    lines = w.run(
        search,
        [GREENHOUSE],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config={"hard_gates": {"required_languages": ["French"]}},
    )
    assert any("skipped (required_language)" in line for line in lines)
    assert _skip_reasons(search) == ["required_language"]
    # The description is used in memory only; no column holds it.
    dump = "\n".join(search.iterdump())
    assert "Fluency in" not in dump


def test_an_oversized_board_logs_stores_nothing_and_counts_as_failed(
    search: sqlite3.Connection,
) -> None:
    board = url("greenhouse", "examplerobotics")
    lines = w.run(
        search,
        [GREENHOUSE],
        today=DAY,
        fetcher=Scripted({board: FetchTooLarge("the page is larger than 20 MB")}),
    )
    assert (
        "watch: Example Robotics (greenhouse): board over the size cap "
        "(the page is larger than 20 MB); nothing recorded"
    ) in lines
    assert lines[-1] == "watch: 0 boards fetched, 1 failed, 0 left"
    for table in ("companies", "postings", "skips"):
        assert search.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0  # noqa: S608
    # The attempt is kept, so the board goes behind the others next run.
    stamps = list(search.execute("SELECT board_key FROM watch_board_attempts"))
    assert [row["board_key"] for row in stamps] == ["greenhouse:examplerobotics"]
    run = search.execute("SELECT boards_fetched, boards_failed FROM watch_runs").fetchone()
    assert (run["boards_fetched"], run["boards_failed"]) == (0, 1)
