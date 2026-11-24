"""A bare "Remote" is a remote job in an unknown region (Batch R2). Synthetic postings
only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from careerhunter import db, filters, locations, watch
from careerhunter.filters import PostingText
from tests import test_bot
from tests.test_bot import Env, _poll, _rows
from tests.test_bot_digest import _post, _set
from tests.test_watch import Scripted, company, fetched, url

env = test_bot.env  # the bot fixture, shared with test_bot

RULES = {"onsite_hybrid": ["Example City"], "remote": ["Exampleland"]}


def _second_line(env: Env) -> str:
    (row,) = _rows(env)
    return row[1]


@pytest.mark.parametrize(
    "text", ["Remote", "remote", "REMOTE (", "Remote \N{EN DASH}", "  Remote - "]
)
def test_a_bare_remote_is_an_unknown_region(text: str) -> None:
    assert locations.bare_remote(text)
    assert locations.unknown(text)


@pytest.mark.parametrize(
    "text",
    ["Remote, Exampleland", "Remote - US", "Example City or Remote", "Remote (Otherland)", ""],
)
def test_a_remote_that_names_a_place_is_not_bare(text: str) -> None:
    assert not locations.bare_remote(text)


def _decide(location: str, work_model: str | None = None) -> str | None:
    posting = PostingText("Example Co", "Example Analyst", location, work_model, "Duties.")
    verdict = filters.decide(
        posting,
        config={"search": {"location_rules": RULES}},
        exclusions={},
        role_filters=None,
        applied=False,
    )
    return verdict.reason


def test_the_location_rules_keep_a_bare_remote() -> None:
    assert _decide("Remote") is None
    assert _decide("Remote", "remote") is None
    assert _decide("", "remote") is None


def test_a_named_remote_place_still_goes_through_the_rules() -> None:
    assert _decide("Remote, Exampleland") is None
    assert _decide("Remote, Otherland") == "location_rule"


def test_the_row_shows_a_bare_remote_as_region_unknown(env: Env, keep_umask: None) -> None:
    posting = _post(env, "Coordinator")
    _set(env, posting, location="Remote")
    assert _poll(env) == 0
    assert _second_line(env).startswith("Remote (region unknown) |")


def test_a_blank_remote_location_shows_region_unknown_too(env: Env, keep_umask: None) -> None:
    posting = _post(env, "Coordinator")
    _set(env, posting, location="", work_model="remote")
    assert _poll(env) == 0
    assert _second_line(env).startswith("Remote (region unknown) |")


def _board_job(location: str, content: str) -> dict[str, Any]:
    return {
        "id": 7,
        "title": "Analyst",
        "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/7",
        "location": {"name": location},
        "content": content,
    }


def _watch(tmp_path: Path, job: dict[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    board = url("greenhouse", "examplerobotics")
    lines = watch.run(
        conn,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today="2026-11-02",
        fetcher=Scripted({board: fetched({"jobs": [job]})}),
        config={"search": {"location_rules": RULES}},
    )
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT p.location, s.reason FROM postings p LEFT JOIN skips s ON s.posting_id = p.id"
        )
    ]
    conn.close()
    return lines, rows


def test_a_bare_remote_takes_its_region_from_the_description(tmp_path: Path) -> None:
    lines, rows = _watch(tmp_path, _board_job("Remote", "&lt;p&gt;Location: Ohio&lt;/p&gt;"))
    assert any("'Analyst': remote region read from its description" in line for line in lines)
    assert rows == [("Remote, Ohio", "location_rule")]  # then checked by the rules


def test_a_bare_remote_with_no_region_in_the_description_is_kept(tmp_path: Path) -> None:
    _, rows = _watch(tmp_path, _board_job("Remote", "&lt;p&gt;Duties.&lt;/p&gt;"))
    assert rows == [("Remote", None)]
