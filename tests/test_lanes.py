"""Title lanes (#33): every variant matches on word boundaries, the best priority wins.

Lane names and titles here are made up.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import lanes
from careerhunter.config import validate_config
from careerhunter.lanes import Lane
from careerhunter.search_store import PendingAlert

ROOT = Path(__file__).parents[1]
TEMPLATE = yaml.safe_load((ROOT / "config.example.yaml").read_text())


def doc(*lane_list: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(TEMPLATE)
    if lane_list:
        d["search"]["lanes"] = list(lane_list)
    assert validate_config(d) == []
    return d


OPERATIONS = Lane("operations", 1, False)
CUSTOMER = Lane("customer_success", 2, True)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Operations Coordinator", OPERATIONS),
        ("Senior Operations Coordinator (Contract)", OPERATIONS),
        ("operations coordinator", OPERATIONS),
        ("OPERATIONS-COORDINATOR", OPERATIONS),
        ("Project Coordinator", OPERATIONS),  # the first variant of a nested list
        ("Project Assistant, Sample Programme", OPERATIONS),  # the second variant
        ("Customer Success Specialist", CUSTOMER),
        ("Customer Success Specialist II", CUSTOMER),
        ("Operations Coordinators", None),  # not a whole-word match
        ("Coordinator", None),
        ("Warehouse Associate", None),
        ("", None),
    ],
)
def test_every_variant_matches_on_word_boundaries(title: str, expected: Lane | None) -> None:
    assert lanes.match(title, doc()) == expected


def test_the_best_priority_wins_when_titles_in_several_lanes_match() -> None:
    d = doc(
        {"name": "broad", "priority": 3, "titles": ["Coordinator"]},
        {"name": "fallback_ops", "priority": 2, "fallback": True, "titles": ["Ops"]},
        {"name": "narrow", "priority": 1, "titles": [["Field Ops Coordinator", "Site Lead"]]},
    )
    assert lanes.match("Field Ops Coordinator", d) == Lane("narrow", 1, False)
    assert lanes.match("Ops Coordinator", d) == Lane("fallback_ops", 2, True)
    assert lanes.match("Logistics Coordinator", d) == Lane("broad", 3, False)
    assert lanes.match("Site Lead", d) == Lane("narrow", 1, False)


def test_configured_lists_lanes_by_priority_and_none_without_any() -> None:
    d = doc(
        {"name": "second", "priority": 5, "titles": ["B"]},
        {"name": "first", "priority": 2, "fallback": True, "titles": ["A"]},
    )
    assert lanes.configured(d) == [Lane("first", 2, True), Lane("second", 5, False)]
    del d["search"]["lanes"]
    assert lanes.configured(d) is None
    assert lanes.match("A", d) is None
    assert lanes.configured(None) is None
    assert lanes.configured({"search": "x"}) is None
    assert lanes.match("A", None) is None


def test_lanes_and_never_gated_families_do_not_read_each_other() -> None:
    d = doc()
    d["hard_gates"]["never_gated_families"] = ["Warehouse Associate", "Operations Coordinator"]
    assert lanes.match("Warehouse Associate", d) is None  # a never-gated family is no lane
    assert lanes.match("Operations Coordinator", d) == OPERATIONS
    d["hard_gates"]["never_gated_families"] = []
    assert lanes.match("Operations Coordinator", d) == OPERATIONS


def test_odd_entries_in_an_unvalidated_doc_are_ignored() -> None:
    raw = {"search": {"lanes": ["x", {"name": "a", "priority": 1, "titles": "A"}]}}
    assert lanes.match("A", raw) is None
    raw = {"search": {"lanes": [{"name": "a", "priority": 1, "titles": [3, ["A", 4]]}]}}
    assert lanes.match("A", raw) == Lane("a", 1, False)


# --- alert order and holds (D2, D8, off_lane_min_fit) ------------------------------------------


def pending(
    posting_id: int,
    fit: str,
    lane: str | None,
    *,
    checked: bool = True,
    first_seen: str = "2026-11-01",
) -> PendingAlert:
    return PendingAlert(posting_id, fit, lane, checked, first_seen)


ORDER_LANES = (
    {"name": "first", "priority": 1, "titles": ["A"]},
    {"name": "second", "priority": 2, "titles": ["B"]},
    {"name": "backup", "priority": 3, "fallback": True, "titles": ["C"]},
)


def ids(ordered: list[lanes.Ordered]) -> list[int]:
    return [item.posting_id for item in ordered]


def test_off_lane_goes_last_then_fit_then_fallback_then_priority_then_oldest() -> None:
    d = doc(*ORDER_LANES)
    d["search"]["off_lane_min_fit"] = "low"
    alerts = [
        pending(1, "high", None),  # off-lane, best fit: still last
        pending(2, "medium", "first"),
        pending(3, "high", "backup"),  # fallback: after non-fallback of the same fit
        pending(4, "high", "second"),
        pending(5, "high", "first", first_seen="2026-11-02"),
        pending(6, "high", "first", first_seen="2026-11-01"),  # older: first
        pending(7, "low", "first"),
        pending(8, "medium", None),
    ]
    assert ids(lanes.alert_order(alerts, d)) == [6, 5, 4, 3, 2, 7, 1, 8]


def test_the_same_day_keeps_the_oldest_id_first() -> None:
    d = doc(*ORDER_LANES)
    alerts = [pending(9, "high", "first"), pending(3, "high", "first")]
    assert ids(lanes.alert_order(alerts, d)) == [3, 9]


def test_a_stored_lane_missing_from_config_sorts_as_a_fallback_before_off_lane() -> None:
    d = doc(*ORDER_LANES)
    d["search"]["off_lane_min_fit"] = "low"
    alerts = [
        pending(1, "high", None),
        pending(2, "high", "retired"),  # no longer in config
        pending(3, "high", "backup"),
        pending(4, "high", "first"),
    ]
    ordered = lanes.alert_order(alerts, d)
    assert ids(ordered) == [4, 3, 2, 1]
    assert [item.lane_line for item in ordered] == [
        "Lane: first",
        "Lane: backup",
        "Lane: retired",
        "Off-lane",
    ]
    # A better fit still goes first: the missing lane only ranks within its fit tier.
    better = [pending(5, "high", "retired"), pending(6, "medium", "first")]
    assert ids(lanes.alert_order(better, d)) == [5, 6]


@pytest.mark.parametrize(
    ("threshold", "sent"),
    [("high", [3]), ("medium", [2, 3]), ("low", [1, 2, 3])],
)
def test_off_lane_postings_below_the_threshold_are_held(threshold: str, sent: list[int]) -> None:
    d = doc(*ORDER_LANES)
    d["search"]["off_lane_min_fit"] = threshold
    alerts = [pending(1, "low", None), pending(2, "medium", None), pending(3, "high", None)]
    assert sorted(ids(lanes.alert_order(alerts, d))) == sent


def test_the_threshold_defaults_to_high() -> None:
    d = doc(*ORDER_LANES)
    del d["search"]["off_lane_min_fit"]
    alerts = [pending(1, "medium", None), pending(2, "high", None)]
    assert ids(lanes.alert_order(alerts, d)) == [2]


def test_a_posting_not_yet_checked_waits_while_lanes_are_on() -> None:
    d = doc(*ORDER_LANES)
    alerts = [pending(1, "high", None, checked=False), pending(2, "low", "first")]
    assert ids(lanes.alert_order(alerts, d)) == [2]


def test_without_lanes_alerts_go_by_fit_then_oldest_and_none_is_held() -> None:
    d = doc()
    del d["search"]["lanes"]
    alerts = [
        pending(1, "low", None),
        pending(2, "high", None, checked=False),
        pending(3, "medium", "first", first_seen="2026-11-03"),
        pending(4, "medium", None, first_seen="2026-11-02"),
    ]
    ordered = lanes.alert_order(alerts, d)
    assert ids(ordered) == [2, 4, 3, 1]
    assert all(item.lane_line is None for item in ordered)


def test_an_unknown_fit_ranks_below_low() -> None:
    d = doc(*ORDER_LANES)
    alerts = [pending(1, "odd", "first"), pending(2, "low", "first")]
    assert ids(lanes.alert_order(alerts, d)) == [2, 1]
