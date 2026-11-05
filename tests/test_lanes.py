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
