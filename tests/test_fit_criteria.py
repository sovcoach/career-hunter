"""The structured fit-criteria schema (#9): validation only, generic values only."""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter.config import (
    telegram_chat_id,
    timezone,
    today,
    validate_config,
    validate_exclusions,
)

ROOT = Path(__file__).parents[1]
TEMPLATE = yaml.safe_load((ROOT / "config.example.yaml").read_text())
EXCLUSIONS = yaml.safe_load((ROOT / "exclusions.example.yaml").read_text())


def doc() -> dict[str, Any]:
    return copy.deepcopy(TEMPLATE)


def test_the_template_uses_every_new_field_and_is_valid() -> None:
    d = doc()
    assert validate_config(d) == []
    assert {"lanes", "location_rules"} <= set(d["search"])
    assert "gap_tiers" in d["fit_rubric"]
    assert {"seniority_words", "title_exceptions", "shift_patterns"} <= set(d["hard_gates"])
    assert "schedule_limits" in d
    assert any("role_filters" in w for w in d["watchlist"])
    assert any(w.get("agency") for w in d["watchlist"])
    assert validate_exclusions(EXCLUSIONS) == []


def test_lanes_make_target_titles_optional() -> None:
    d = doc()
    del d["search"]["target_titles"]
    assert validate_config(d) == []
    del d["search"]["lanes"]
    assert any("target_titles" in p for p in validate_config(d))


def test_gap_tiers_make_weights_and_thresholds_optional() -> None:
    d = doc()
    del d["fit_rubric"]["weights"], d["fit_rubric"]["thresholds"]
    assert validate_config(d) == []
    del d["fit_rubric"]["gap_tiers"]
    problems = validate_config(d)
    assert any("weights" in p for p in problems)
    assert any("thresholds" in p for p in problems)


def test_quiet_hours_are_not_in_the_example() -> None:
    assert "quiet_hours" not in doc()["schedule"]
    text = (ROOT / "config.example.yaml").read_text()
    assert "Quiet hours are not implemented" in text


def test_watcher_interval_is_optional() -> None:
    d = doc()
    del d["schedule"]["watcher_interval_minutes"]
    assert validate_config(d) == []


def _set(path: str, value: Any) -> Any:
    def mutate(d: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        node: Any = d
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node[key]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value

    return mutate


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_set("search.lanes", []), "non-empty list of lanes"),
        (_set("search.lanes.0.name", " "), "name must be a non-empty string"),
        (_set("search.lanes.0.priority", 0), "priority must be an integer >= 1"),
        (_set("search.lanes.1.priority", 1), "priorities must be unique"),
        (_set("search.lanes.1.name", "operations"), "names must be unique"),
        (_set("search.lanes.0.fallback", "yes"), "fallback must be true or false"),
        (_set("search.lanes.0.titles", []), "titles must be a non-empty list"),
        (_set("search.lanes.0.titles", [["A", 3]]), "a title may be a list of variants"),
        (_set("search.lanes.0.colour", "red"), "unknown keys"),
        (_set("search.location_rules.remote", "Canada"), "location_rules.remote"),
        (_set("search.location_rules.allow_travel_outside", "no"), "allow_travel_outside"),
        (_set("search.location_rules.radius", 5), "unknown keys"),
        (_set("fit_rubric.gap_tiers.tiers", []), "tiers must be a non-empty list"),
        (
            _set(
                "fit_rubric.gap_tiers.tiers",
                [{"name": "a", "max_gaps": 3}, {"name": "b", "max_gaps": 1}],
            ),
            "ordered by increasing max_gaps",
        ),
        (_set("fit_rubric.gap_tiers.tiers.0.max_gaps", -1), "max_gaps must be an integer >= 0"),
        (_set("fit_rubric.gap_tiers.exclude_from_gaps", 2), "above every max_gaps"),
        (_set("fit_rubric.gap_tiers.boundary.all_minor", "best"), "must name one of the tiers"),
        (_set("fit_rubric.gap_tiers.boundary.at_gaps", "one"), "at_gaps must be an integer"),
        (_set("fit_rubric.gap_tiers.exempt_requirements", "degree"), "exempt_requirements"),
        (_set("hard_gates.shift_patterns", "overnight"), "hard_gates.shift_patterns"),
        (_set("hard_gates.seniority_words", []), "title_exceptions needs"),
        (_set("schedule_limits.weekend_days_per_month", -1), "weekend_days_per_month"),
        (_set("schedule_limits.over_limit", "ignore"), "over_limit must be one of"),
        (_set("watchlist.1.agency", "yes"), "agency must be true or false"),
        (_set("watchlist.1.role_filters.work_models", ["office"]), "role_filters.work_models"),
        (_set("watchlist.1.role_filters.seniority", ["junior"]), "unknown keys"),
        (_set("telegram.chat_id", "me"), "telegram.chat_id"),
        (_set("telegram.chat_id", -100123), "positive numeric id"),
        (_set("telegram.token", "x"), "unknown keys"),
        (_set("telegram", "chat"), "telegram must be a mapping"),
    ],
)
def test_problems_are_reported(mutate: Any, message: str) -> None:
    d = doc()
    if "title_exceptions needs" in message:
        d["hard_gates"]["title_exceptions"] = ["Example Manager"]
    mutate(d)
    problems = validate_config(d)
    assert any(message in p for p in problems), problems


def test_every_problem_is_reported_in_one_pass() -> None:
    d = doc()
    d["search"]["lanes"][0]["priority"] = 0
    d["fit_rubric"]["gap_tiers"]["exclude_from_gaps"] = 0
    d["schedule_limits"]["over_limit"] = "ignore"
    d["telegram"]["chat_id"] = "me"
    assert len(validate_config(d)) >= 4


def test_telegram_chat_id_helper() -> None:
    d = doc()
    assert telegram_chat_id(d) is None
    d["telegram"]["chat_id"] = 123456789
    assert validate_config(d) == []
    assert telegram_chat_id(d) == 123456789
    del d["telegram"]
    assert telegram_chat_id(d) is None


@pytest.mark.parametrize(
    ("value", "offset"),
    [
        ("Europe/Berlin", None),  # a zone name: the offset depends on the date
        ("UTC", dt.timedelta(0)),
        ("UTC-03:00", dt.timedelta(hours=-3)),
        ("UTC+05:30", dt.timedelta(hours=5, minutes=30)),
    ],
)
def test_timezone_names_and_fixed_offsets_parse(value: str, offset: dt.timedelta | None) -> None:
    d = doc()
    d["schedule"]["timezone"] = value
    assert validate_config(d) == []
    zone = timezone(d)
    if offset is None:
        assert str(zone) == value
    else:
        assert zone.utcoffset(None) == offset


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "UTC-15", "UTC+5:75", "GMT-6", "", 5])
def test_bad_timezones_are_reported(value: object) -> None:
    d = doc()
    d["schedule"]["timezone"] = value
    assert any(p.startswith("schedule.timezone:") for p in validate_config(d))


def test_a_missing_timezone_means_utc() -> None:
    d = doc()
    del d["schedule"]["timezone"]
    assert validate_config(d) == []
    assert timezone(d) is dt.UTC
    del d["schedule"]
    assert validate_config(d) == []
    late = dt.datetime(2026, 10, 28, 23, 30, tzinfo=dt.UTC)
    assert today(d, late) == dt.date(2026, 10, 28)
    assert today(None, late) == dt.date(2026, 10, 28)


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ("x", "roles must be a list"),
        (["x"], "must be a mapping"),
        ([{"title": "T"}], "company is required"),
        ([{"company": "C", "title": " "}], "title is required"),
        ([{"company": "C", "title": "T", "location": ""}], "location must be non-empty"),
        ([{"company": "C", "title": "T", "team": "x"}], "unknown keys"),
    ],
)
def test_role_level_exclusions(roles: Any, message: str) -> None:
    problems = validate_exclusions({"roles": roles})
    assert any(message in p for p in problems), problems


def test_exclude_applied_must_be_boolean() -> None:
    assert validate_exclusions({"exclude_applied": True}) == []
    assert validate_exclusions({"exclude_applied": "yes"})
