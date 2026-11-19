"""A Workday board's `ats_facets` (search migration 0035): names mapped to ids from the
board's own facets every run, the list read filtered when all are found and unfiltered
when one is not, and the result recorded for doctor.

Synthetic boards, facet names and ids only. Nested facets are only covered here, with
fixtures shaped like a facet group; no live board was read with one applied."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from careerhunter import ats, cli, search_store
from careerhunter import watch as w
from careerhunter.config import validate_config
from careerhunter.fetch import Fetched
from tests.test_config import example
from tests.test_watch import DAY, fetched
from tests.test_watch_ats import (
    WD,
    WD_BASE,
    WD_LIST,
    routes,
    stored,
    wd_info,
    wd_page,
    wd_path,
    wd_row,
)

NORTH = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
SOUTH = "f9e8d7c6b5a4938271605f4e3d2c1b0a"
HARBOUR_A = "aaaa000011112222333344445555aaaa"
HARBOUR_B = "bbbb000011112222333344445555bbbb"
HILLSIDE = "cccc000011112222333344445555cccc"

FACETS: list[dict[str, object]] = [
    {
        "facetParameter": "Work_Region",
        "descriptor": "Work Region",
        "values": [
            {"descriptor": "Northland", "id": NORTH, "count": 3},
            {"descriptor": "Southland", "id": SOUTH, "count": 9},
        ],
    },
    {
        # A facet group: its values are facets, not values.
        "facetParameter": "siteMainGroup",
        "descriptor": "Sites",
        "values": [
            {
                "facetParameter": "sites",
                "descriptor": "Sites",
                "values": [
                    {"descriptor": "Harbour Point", "id": HARBOUR_A, "count": 2},
                    {"descriptor": "Harbour Point", "id": HARBOUR_B, "count": 1},
                    {"descriptor": "Hillside", "id": HILLSIDE, "count": 4},
                ],
            }
        ],
    },
]


def faceted_page(rows: list[dict[str, object]], total: int = 0) -> dict[str, object]:
    return {**wd_page(rows, total), "facets": FACETS}


class Faceted:
    """Scripted GETs, plus POSTs answered by the facets applied and the offset."""

    def __init__(
        self,
        details: dict[str, object],
        pages: dict[tuple[str, int], Fetched | Exception],
    ) -> None:
        self.details = routes(details)
        self.pages = pages
        self.posts: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def __call__(self, address: str, **_: object) -> Fetched:
        self.calls.append(address)
        found = self.details[address]
        if isinstance(found, Exception):
            raise found
        return found

    def post(self, address: str, payload: object) -> Fetched:
        assert address == WD_LIST
        assert isinstance(payload, dict)
        self.posts.append(payload)
        key = (json.dumps(payload["appliedFacets"], sort_keys=True), payload["offset"])
        found = self.pages[key]
        if isinstance(found, Exception):
            raise found
        return found


NONE = json.dumps({})


def board(facets: dict[str, object], **extra: object) -> dict[str, object]:
    return {**WD, "ats_facets": facets, **extra}


def run(conn: sqlite3.Connection, fetcher: Faceted, entry: dict[str, object]) -> list[str]:
    return w.run(conn, [entry], today=DAY, fetcher=fetcher, poster=fetcher.post)


def status(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute("SELECT company_id, facet_status, unmatched FROM board_status")
    ]


# --- mapping names to ids ------------------------------------------------------------------


def test_names_map_to_request_keys_and_ids_ignoring_case_and_spaces() -> None:
    applied, unmatched = ats.facet_ids(
        {"facets": FACETS}, (("work  region", ("NORTHLAND", "Southland")),)
    )
    assert applied == {"Work_Region": [NORTH, SOUTH]}
    assert unmatched == []


def test_a_nested_facet_is_found_and_a_shared_value_name_means_every_id() -> None:
    applied, unmatched = ats.facet_ids({"facets": FACETS}, (("Sites", ("Harbour Point",)),))
    assert applied == {"sites": [HARBOUR_A, HARBOUR_B]}
    assert unmatched == []


def test_names_not_found_are_listed_once_per_value() -> None:
    applied, unmatched = ats.facet_ids(
        {"facets": FACETS},
        (("Work Region", ("Northland", "Eastland")), ("Office Type", ("Remote",))),
    )
    assert applied == {"Work_Region": [NORTH]}
    assert unmatched == ["Work Region: Eastland", "Office Type: Remote"]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"facets": "nope"},
        {"facets": [{"facetParameter": "bad key!", "descriptor": "Work Region", "values": []}]},
        {
            "facets": [
                {
                    "facetParameter": "Work_Region",
                    "descriptor": "Work Region",
                    "values": [{"descriptor": "Northland", "id": "not/an/id"}],
                }
            ]
        },
    ],
)
def test_facets_of_an_unexpected_shape_match_nothing(data: object) -> None:
    applied, unmatched = ats.facet_ids(data, (("Work Region", ("Northland",)),))
    assert applied == {}
    assert unmatched == ["Work Region: Northland"]


# --- the watcher ---------------------------------------------------------------------------


def test_every_name_found_reads_the_board_filtered(
    search: sqlite3.Connection, monkeypatch: Any
) -> None:
    monkeypatch.setattr(ats, "WORKDAY_PAGE", 1)
    applied = json.dumps({"Work_Region": [NORTH]})
    fetcher = Faceted(
        {f"{WD_BASE}{wd_path(n)}": wd_info("A friendly team.") for n in (7, 8)},
        {
            (NONE, 0): fetched(faceted_page([wd_row(1)], total=12)),
            (applied, 0): fetched(faceted_page([wd_row(7)], total=2)),
            (applied, 1): fetched(wd_page([wd_row(8)])),
            (applied, 2): fetched(wd_page([])),
        },
    )
    lines = run(search, fetcher, board({"Work Region": "Northland"}))
    assert [(p["appliedFacets"], p["offset"]) for p in fetcher.posts] == [
        ({}, 0),
        ({"Work_Region": [NORTH]}, 0),
        ({"Work_Region": [NORTH]}, 1),
        ({"Work_Region": [NORTH]}, 2),
    ]
    assert [row[3] for row in stored(search)] == [
        f"https://examplerobotics.wd5.myworkdayjobs.com/External{wd_path(n)}" for n in (7, 8)
    ]
    assert not any("ats_facets" in line for line in lines)
    assert lines[-1] == "watch: 1 boards fetched, 0 failed, 0 left"
    assert status(search) == [(1, "matched", "[]")]


def test_a_nested_facet_and_a_country_facet_are_applied_together(
    search: sqlite3.Connection,
) -> None:
    applied = json.dumps({"Work_Region": [SOUTH], "sites": [HARBOUR_A, HARBOUR_B]})
    fetcher = Faceted(
        {f"{WD_BASE}{wd_path(3)}": wd_info("A friendly team.")},
        {
            (NONE, 0): fetched(faceted_page([wd_row(1)], total=12)),
            (applied, 0): fetched(faceted_page([wd_row(3)], total=1)),
        },
    )
    run(search, fetcher, board({"Work Region": ["Southland"], "Sites": ["Harbour Point"]}))
    assert fetcher.posts[1]["appliedFacets"] == {
        "Work_Region": [SOUTH],
        "sites": [HARBOUR_A, HARBOUR_B],
    }
    assert len(stored(search)) == 1
    assert status(search) == [(1, "matched", "[]")]


def test_a_name_not_found_is_logged_and_recorded_and_the_board_read_unfiltered(
    search: sqlite3.Connection,
) -> None:
    fetcher = Faceted(
        {f"{WD_BASE}{wd_path(n)}": wd_info("A friendly team.") for n in (1, 2)},
        {(NONE, 0): fetched(faceted_page([wd_row(1), wd_row(2)], total=2))},
    )
    lines = run(search, fetcher, board({"Work Region": ["Northland", "Eastland"]}))
    # The first page is read once, and it is the unfiltered list's first page.
    assert [(p["appliedFacets"], p["offset"]) for p in fetcher.posts] == [({}, 0)]
    assert len(stored(search)) == 2
    assert (
        'watch: Example Robotics (workday): ats_facets "Work Region: Eastland" not found; '
        "read unfiltered"
    ) in lines
    assert status(search) == [(1, "unmatched", '["Work Region: Eastland"]')]


def test_the_status_follows_each_run_and_goes_when_ats_facets_is_removed(
    search: sqlite3.Connection,
) -> None:
    unfiltered = {(NONE, 0): fetched(faceted_page([wd_row(1)], total=1))}
    details = {f"{WD_BASE}{wd_path(1)}": wd_info("A friendly team.")}
    run(search, Faceted(details, unfiltered), board({"Work Region": "Eastland"}))
    assert status(search)[0][1] == "unmatched"
    applied = json.dumps({"Work_Region": [NORTH]})
    pages = {**unfiltered, (applied, 0): fetched(faceted_page([wd_row(1)], total=1))}
    run(search, Faceted(details, pages), board({"Work Region": "Northland"}))
    assert status(search) == [(1, "matched", "[]")]
    run(search, Faceted(details, unfiltered), WD)
    assert status(search) == []


def test_a_failed_first_page_records_nothing_and_keeps_the_last_status(
    search: sqlite3.Connection,
) -> None:
    details = {f"{WD_BASE}{wd_path(1)}": wd_info("A friendly team.")}
    run(
        search,
        Faceted(details, {(NONE, 0): fetched(faceted_page([wd_row(1)], total=1))}),
        board({"Work Region": "Eastland"}),
    )
    before = status(search)
    lines = run(
        search,
        Faceted({}, {(NONE, 0): fetched(status=503, body=b"")}),
        board({"Work Region": "Northland"}),
    )
    assert "watch: Example Robotics (workday): HTTP 503; nothing recorded" in lines
    assert status(search) == before


def test_ats_facets_on_another_board_is_ignored_by_the_watcher() -> None:
    entry = w._entry({"name": "X", "ats_type": "lever", "ats_slug": "x", "ats_facets": {"A": "B"}})
    assert entry is not None and entry.facets == ()


# --- config --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facets", "message"),
    [
        ([], "must map a facet name"),
        ({}, "must map a facet name"),
        ({"Work Region": []}, "Work Region must be a value name or a list"),
        ({"Work Region": [""]}, "Work Region must be a value name or a list"),
        ({"Work Region": 5}, "Work Region must be a value name or a list"),
        ({"Work Region": ["North\nland"]}, "Work Region must be a value name or a list"),
        ({"": "Northland"}, "has a facet name that is not text"),
        ({f"F{n}": "x" for n in range(11)}, "has more than 10 facets"),
    ],
)
def test_bad_ats_facets_are_reported(facets: object, message: str) -> None:
    doc = example()
    doc["watchlist"] = [{**WD, "ats_facets": facets}]
    assert any(message in problem for problem in validate_config(doc))


def test_ats_facets_is_for_workday_only() -> None:
    doc = example()
    doc["watchlist"] = [
        {"name": "X", "ats_type": "lever", "ats_slug": "x", "ats_facets": {"A": "B"}}
    ]
    assert any("ats_facets is for ats_type workday only" in p for p in validate_config(doc))


def test_good_ats_facets_are_accepted() -> None:
    doc = example()
    doc["watchlist"] = [
        {**WD, "ats_facets": {"Work Region": "Northland", "Sites": ["Harbour Point", "Hillside"]}}
    ]
    assert validate_config(doc) == []


# --- doctor --------------------------------------------------------------------------------


def _doctor(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str], watchlist: list[object]
) -> str:
    capsys.readouterr()
    cli._doctor_facets(cli._Report(), search, {"watchlist": watchlist})
    return capsys.readouterr().out


def test_doctor_warns_for_each_board_with_a_name_not_found(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    faceted = board({"Work Region": ["Northland", "Eastland"], "Office Type": "Remote"})
    assert _doctor(search, capsys, [faceted]) == (
        "workday facets  OK  1 board with ats_facets; every name found last run\n"
    )
    search.execute("INSERT INTO companies (name, source) VALUES ('Example Robotics', 'manual')")
    search_store.set_facet_status(
        search,
        "Example Robotics",
        ["Work Region: Eastland", "Office Type: Remote"],
        "2026-10-31T10:00:00Z",
    )
    assert _doctor(search, capsys, [faceted]) == (
        'workday facets  WARN Example Robotics: "Work Region: Eastland", "Office Type: Remote" '
        "not found last run (renamed, or no open postings); read unfiltered\n"
    )
    # A board whose entry no longer has ats_facets is not warned about.
    assert _doctor(search, capsys, [WD]) == (
        "workday facets  --  none set (ats_facets in a watchlist entry)\n"
    )


def test_the_watcher_leaves_out_ats_facets_of_the_wrong_shape() -> None:
    assert w._facets("Northland") == ()
    assert w._facets(
        {"Work Region": ["Northland", 5, " "], "": "x", "Sites": 7, "Office": ["", None]}
    ) == (("Work Region", ("Northland",)),)


def test_facets_with_odd_nodes_are_skipped() -> None:
    data = {
        "facets": [
            "not a facet",
            {"facetParameter": "Work_Region", "descriptor": "Work Region", "values": "x"},
            {
                "facetParameter": "Work_Region",
                "descriptor": "Work Region",
                "values": ["not a value", {"descriptor": "Northland", "id": NORTH}],
            },
        ]
    }
    assert ats.facet_ids(data, (("Work Region", ("Northland",)),)) == (
        {"Work_Region": [NORTH]},
        [],
    )


def test_doctor_says_nothing_without_config_and_skips_other_boards_rows(
    search: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    cli._doctor_facets(cli._Report(), search, None)
    assert capsys.readouterr().out == ""
    search.execute("INSERT INTO companies (name, source) VALUES ('Example Robotics', 'manual')")
    search.execute("INSERT INTO companies (name, source) VALUES ('Northwind Analytics', 'manual')")
    search_store.set_facet_status(search, "Example Robotics", [], "2026-10-31T10:00:00Z")
    search_store.set_facet_status(
        search, "Northwind Analytics", ["Work Region: Eastland"], "2026-10-31T10:00:00Z"
    )
    assert _doctor(search, capsys, [board({"Work Region": "Northland"})]) == (
        "workday facets  OK  1 board with ats_facets; every name found last run\n"
    )
