"""`careerhunter lanes reassign [--dry-run]`: recompute checked lanes, logged.

Companies, titles and lanes here are made up, against temporary databases.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import cli, db, lanes, search_store
from careerhunter.lanes import LaneMove
from tests.conftest import FakeProbe
from tests.test_lanes import doc

TODAY = dt.date(2026, 11, 2)
RECENT = "2026-11-01"
OLD = "2026-09-01"  # past the default posting-age gate

LANES = (
    {"name": "it_support", "priority": 1, "titles": ["IT Support Specialist"]},
    {"name": "client_experience", "priority": 2, "titles": ["Client Experience Analyst"]},
    {"name": "operations", "priority": 3, "titles": ["Operations Coordinator"]},
)


def config() -> dict[str, Any]:
    return doc(*LANES)


def posting(
    conn: sqlite3.Connection,
    title: str,
    lane: str | None,
    *,
    fit: str = "medium",
    seen: str = RECENT,
    checked: bool = True,
) -> int:
    company = conn.execute("SELECT id FROM companies LIMIT 1").fetchone()
    company_id = (
        company[0]
        if company
        else conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Widgets', 'manual')"
        ).lastrowid
    )
    cur = conn.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, llm_output, fit) "
        "VALUES (?, ?, ?, 'watcher', '{}', ?)",
        (company_id, title, seen, fit),
    )
    posting_id = int(cur.lastrowid or 0)
    if checked:
        search_store.set_posting_lane(conn, posting_id, lane)
    return posting_id


def lanes_of(conn: sqlite3.Connection) -> dict[int, tuple[object, object]]:
    rows = conn.execute("SELECT id, lane, lane_checked FROM postings ORDER BY id")
    return {row["id"]: (row["lane"], row["lane_checked"]) for row in rows}


def changes(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = conn.execute("SELECT posting_id, old_lane, new_lane FROM lane_changes ORDER BY id")
    return [tuple(row) for row in rows]


def test_the_plan_lists_every_checked_posting_that_would_move(
    search: sqlite3.Connection,
) -> None:
    # Stored under the old, literal-only matcher, or an older lane list.
    loose = posting(search, "IT Support Associate", None)
    renamed = posting(search, "Operations Coordinator", "ops_old")
    dropped = posting(search, "Warehouse Associate", "operations")
    same = posting(search, "Client Experience Analyst", "client_experience")
    unchecked = posting(search, "IT Support Specialist", None, checked=False)
    before = lanes_of(search)
    planned = lanes.plan_reassign(search, config(), TODAY)
    assert planned.checked == 4
    assert planned.moves == (
        LaneMove(loose, "IT Support Associate", None, "it_support"),
        LaneMove(renamed, "Operations Coordinator", "ops_old", "operations"),
        LaneMove(dropped, "Warehouse Associate", "operations", None),
    )
    assert same not in {move.posting_id for move in planned.moves}
    assert unchecked not in {move.posting_id for move in planned.moves}
    assert lanes_of(search) == before  # a plan writes nothing
    assert changes(search) == []


def test_reassign_applies_the_plan_and_logs_each_move(search: sqlite3.Connection) -> None:
    loose = posting(search, "Analyst, Client Experience", None)
    dropped = posting(search, "Warehouse Associate", "operations")
    unchecked = posting(search, "IT Support Associate", None, checked=False)
    planned = lanes.reassign(search, config(), TODAY)
    assert len(planned.moves) == 2
    assert lanes_of(search) == {
        loose: ("client_experience", 1),
        dropped: (None, 1),
        unchecked: (None, 0),  # the watcher fills in unchecked rows, not reassign
    }
    assert changes(search) == [
        (loose, None, "client_experience"),
        (dropped, "operations", None),
    ]
    again = lanes.reassign(search, config(), TODAY)
    assert again.moves == ()
    assert len(changes(search)) == 2


def test_held_off_lane_postings_released_by_a_move_are_counted(
    search: sqlite3.Connection,
) -> None:
    # The default off_lane_min_fit is high, so these off-lane postings are held.
    released = posting(search, "IT Support Associate", None, fit="medium")
    stays_held = posting(search, "Warehouse Associate", None, fit="medium")
    already = posting(search, "IT Support Associate II", None, fit="high")  # not held
    planned = lanes.plan_reassign(search, config(), TODAY)
    assert planned.released == (released,)
    assert planned.too_old == ()
    assert planned.newly_held == 0
    before = {
        item.posting_id for item in lanes.alert_order(search_store.pending_alerts(search), config())
    }
    assert before == {already}
    lanes.reassign(search, config(), TODAY)
    after = [
        item.posting_id for item in lanes.alert_order(search_store.pending_alerts(search), config())
    ]
    assert set(after) == {released, already}
    assert stays_held not in after


def test_a_released_posting_past_the_age_gate_is_skipped_not_alerted(
    search: sqlite3.Connection,
) -> None:
    aged = posting(search, "IT Support Associate", None, fit="medium", seen=OLD)
    planned = lanes.plan_reassign(search, config(), TODAY)
    assert planned.released == ()
    assert planned.too_old == (aged,)
    assert not search_store.posting_has_skip(search, aged)  # the plan writes nothing
    lanes.reassign(search, config(), TODAY)
    reason = search.execute("SELECT reason FROM skips WHERE posting_id = ?", (aged,))
    assert reason.fetchone()[0] == "too_old"
    assert lanes_of(search)[aged] == ("it_support", 1)
    assert aged not in {alert.posting_id for alert in search_store.pending_alerts(search)}


def test_an_alertable_posting_moved_off_lane_below_the_threshold_is_held(
    search: sqlite3.Connection,
) -> None:
    posting(search, "Warehouse Associate", "operations", fit="medium")
    planned = lanes.plan_reassign(search, config(), TODAY)
    assert planned.newly_held == 1
    assert planned.released == ()


def test_a_posting_that_moved_since_the_plan_is_left_alone(search: sqlite3.Connection) -> None:
    p = posting(search, "Warehouse Associate", "operations")
    assert not search_store.move_posting_lane(search, p, "support", None)
    assert search_store.move_posting_lane(search, p, "operations", None)
    assert changes(search) == [(p, "operations", None)]


# --- the command ------------------------------------------------------------------------------


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    assert cli.main(["migrate"], probe=probe) == 0
    capsys.readouterr()
    return data_env


def set_lanes(ready: Path, lane_list: tuple[dict[str, Any], ...] | None) -> None:
    path = ready / "config.yaml"
    loaded = yaml.safe_load(path.read_text())
    if lane_list is None:
        loaded["search"].pop("lanes", None)
    else:
        loaded["search"]["lanes"] = list(lane_list)
    path.write_text(yaml.safe_dump(loaded, sort_keys=False))


def seed(ready: Path) -> int:
    conn = db.connect(ready / "search.db")
    try:
        return posting(conn, "IT Support Associate", None, seen=dt.date.today().isoformat())
    finally:
        conn.close()


def test_the_dry_run_lists_the_moves_and_writes_nothing(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    set_lanes(ready, LANES)
    p = seed(ready)
    before = (ready / "search.db").read_bytes()
    assert cli.main(["lanes", "reassign", "--dry-run"], probe=probe) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "dry run: nothing is written."
    assert f"{p:6} IT Support Associate: off-lane -> it_support" in out
    assert "1 of 1 checked postings would change lane." in out
    assert "from off-lane to a lane: 1; to off-lane: 0; between lanes: 0" in out
    assert "1 held off-lane postings would become alertable" in out
    assert (ready / "search.db").read_bytes() == before


def test_the_run_applies_the_same_moves(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    set_lanes(ready, LANES)
    p = seed(ready)
    assert cli.main(["lanes", "reassign"], probe=probe) == 0
    out = capsys.readouterr().out
    assert f"{p:6} IT Support Associate: off-lane -> it_support" in out
    assert "1 of 1 checked postings changed lane." in out
    assert "1 held off-lane postings became alertable" in out
    conn = db.connect_read_only(ready / "search.db")
    try:
        assert changes(conn) == [(p, None, "it_support")]
    finally:
        conn.close()
    assert cli.main(["lanes", "reassign"], probe=probe) == 0
    assert "0 of 1 checked postings changed lane." in capsys.readouterr().out


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_it_refuses_without_search_lanes(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    set_lanes(ready, None)
    assert cli.main(["lanes", "reassign", *extra], probe=probe) == 1
    assert "no search.lanes" in capsys.readouterr().err


def test_a_dry_run_needs_search_db_migrated(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_lanes(ready, LANES)
    real = db.latest_version
    monkeypatch.setattr(db, "latest_version", lambda track: real(track) + 1)
    assert cli.main(["lanes", "reassign", "--dry-run"], probe=probe) == 1
    assert "search.db needs a migration first" in capsys.readouterr().err


def test_it_needs_a_config_and_a_search_db(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    set_lanes(ready, LANES)
    (ready / "search.db").rename(ready / "search.db.moved")
    assert cli.main(["lanes", "reassign"], probe=probe) == 1
    assert "search.db is missing" in capsys.readouterr().err
    (ready / "config.yaml").unlink()
    assert cli.main(["lanes", "reassign"], probe=probe) == 1
    assert "config.yaml is missing" in capsys.readouterr().err
