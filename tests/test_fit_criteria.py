"""The structured fit-criteria schema (#9): validation only, generic values only."""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter.config import (
    discovery_enabled,
    discovery_interval_minutes,
    off_lane_min_fit,
    requirement_tiers,
    telegram_chat_id,
    tier_fits,
    timezone,
    today,
    validate_config,
    validate_exclusions,
)
from tests.fakes import template_watchlist

ROOT = Path(__file__).parents[1]
TEMPLATE = yaml.safe_load((ROOT / "config.example.yaml").read_text())
EXCLUSIONS = yaml.safe_load((ROOT / "exclusions.example.yaml").read_text())


def doc() -> dict[str, Any]:
    return copy.deepcopy(TEMPLATE)


def test_the_template_uses_every_new_field_and_is_valid() -> None:
    d = doc()
    assert validate_config(d) == []
    assert {"lanes", "location_rules"} <= set(d["search"])
    assert d["search"]["resume"] is None
    assert d["fit_rubric"]["embedding_weight"] is None
    assert d["discovery"]["searxng_url"] is None
    assert d["discovery"]["career_hosts"] == []
    assert d["discovery"]["interval_minutes"] == 1440
    assert "requirement_tiers" in d["fit_rubric"] and "gap_tiers" not in d["fit_rubric"]
    assert {"seniority_words", "title_exceptions", "shift_patterns"} <= set(d["hard_gates"])
    assert "schedule_limits" in d
    assert d["watchlist"] == []  # inert until edited; see test_template_watchlist
    assert any("role_filters" in w for w in template_watchlist())
    assert any(w.get("agency") for w in template_watchlist())
    assert validate_exclusions(EXCLUSIONS) == []


def test_lanes_make_target_titles_optional() -> None:
    d = doc()
    del d["search"]["target_titles"]
    assert validate_config(d) == []
    del d["search"]["lanes"]
    assert any("target_titles" in p for p in validate_config(d))


def test_requirement_tiers_make_weights_and_thresholds_optional() -> None:
    d = doc()
    del d["fit_rubric"]["weights"], d["fit_rubric"]["thresholds"]
    assert validate_config(d) == []
    del d["fit_rubric"]["requirement_tiers"]
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


def test_discovery_interval_defaults_to_a_day() -> None:
    d = doc()
    assert discovery_enabled(d) is False
    assert discovery_interval_minutes(d) == 1440
    d["discovery"]["searxng_url"] = "http://127.0.0.1:8888/search"
    assert discovery_enabled(d) is True
    d["discovery"]["searxng_url"] = None
    del d["discovery"]["interval_minutes"]
    assert validate_config(d) == []
    assert discovery_interval_minutes(d) == 1440
    d["discovery"]["interval_minutes"] = None
    assert validate_config(d) == []
    assert discovery_interval_minutes(d) == 1440
    d["discovery"]["interval_minutes"] = 60
    assert validate_config(d) == []
    assert discovery_interval_minutes(d) == 60
    del d["discovery"]
    assert discovery_interval_minutes(d) == 1440
    assert discovery_interval_minutes(None) == 1440
    assert discovery_enabled(d) is False
    assert discovery_enabled(None) is False


def test_discovery_settings_are_optional() -> None:
    d = doc()
    del d["discovery"]
    assert validate_config(d) == []
    d["discovery"] = {"searxng_url": None, "career_hosts": None}
    assert validate_config(d) == []
    for url in (
        "http://127.0.0.1:8888/search",
        "http://localhost:8888/search",
        "http://[::1]:8888/search",
    ):
        d["discovery"] = {"searxng_url": url, "career_hosts": ["jobs.example.com"]}
        assert validate_config(d) == [], url


def test_resume_and_embedding_weight_are_optional() -> None:
    d = doc()
    del d["search"]["resume"]
    del d["fit_rubric"]["embedding_weight"]
    assert validate_config(d) == []
    d["search"]["resume"] = "notes/sample.txt"
    for weight in (0, 1, 0.0, 1.0, 0.5, None):
        d["fit_rubric"]["embedding_weight"] = weight
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
        (
            _set("search.location_rules.allow_travel_outside", False),
            "search.location_rules.allow_travel_outside is no longer a setting: delete that "
            "line from config.yaml",
        ),
        (_set("search.location_rules.radius", 5), "unknown keys"),
        (
            _set("fit_rubric.requirement_tiers.high_max_minor", -1),
            "high_max_minor must be an integer from 0 to 20",
        ),
        (
            _set("fit_rubric.requirement_tiers.exclude_at_core", 1),
            "exclude_at_core must be an integer from 2 to 20",
        ),
        (
            _set("fit_rubric.requirement_tiers.entry_level_extra_minor", True),
            "entry_level_extra_minor must be an integer",
        ),
        (_set("fit_rubric.requirement_tiers.exempt_types", ["degree"]), "exempt_types must be"),
        (_set("fit_rubric.requirement_tiers.max_gaps", 3), "unknown keys: ['max_gaps']"),
        (
            _set("fit_rubric.gap_tiers", {"tiers": []}),
            "fit_rubric.gap_tiers is no longer a setting: replace it with "
            "fit_rubric.requirement_tiers",
        ),
        (_set("hard_gates.shift_patterns", "overnight"), "hard_gates.shift_patterns"),
        (_set("hard_gates.seniority_words", []), "title_exceptions needs"),
        (
            _set("hard_gates.seniority_in_description", "yes"),
            "hard_gates.seniority_in_description must be true or false",
        ),
        (
            _set("hard_gates.seniority_in_desc", True),
            "hard_gates has unknown keys: ['seniority_in_desc']",
        ),
        (_set("schedule_limits.weekend_days_per_month", -1), "weekend_days_per_month"),
        (_set("schedule_limits.over_limit", "ignore"), "over_limit must be one of"),
        (_set("watchlist.1.agency", "yes"), "agency must be true or false"),
        (_set("watchlist.1.role_filters.work_models", ["office"]), "role_filters.work_models"),
        (_set("watchlist.1.role_filters.seniority", ["junior"]), "unknown keys"),
        (_set("fit_rubric.embedding_weight", 1.5), "number from 0 to 1"),
        (_set("fit_rubric.embedding_weight", -0.1), "number from 0 to 1"),
        (_set("fit_rubric.embedding_weight", True), "number from 0 to 1"),
        (_set("fit_rubric.embedding_weight", False), "number from 0 to 1"),
        (_set("fit_rubric.embedding_weight", "high"), "number from 0 to 1"),
        (_set("search.resume", "../outside.txt"), "search.resume must be a relative path"),
        (_set("search.resume", "notes/../../outside.txt"), "search.resume must be a relative path"),
        (
            _set("search.resume", "/Volumes/ExampleDrive/ExampleFolder/notes.txt"),
            "search.resume must be a relative path",
        ),
        (_set("search.resume", ""), "search.resume must be a relative path"),
        (_set("search.resume", "  "), "search.resume must be a relative path"),
        (_set("search.resume", 3), "search.resume must be a relative path"),
        (_set("discovery.searxng_url", "https://127.0.0.1/search"), "http loopback URL"),
        (_set("discovery.searxng_url", "http://example.com/search"), "http loopback URL"),
        (_set("discovery.searxng_url", "http://user@127.0.0.1/search"), "http loopback URL"),
        (_set("discovery.searxng_url", "http://10.0.0.1/search"), "http loopback URL"),
        (_set("discovery.searxng_url", "http://127.0.0.1/search?q=1"), "http loopback URL"),
        (_set("discovery.searxng_url", ""), "http loopback URL"),
        (_set("discovery.searxng_url", "  "), "http loopback URL"),
        (_set("discovery.searxng_url", 3), "http loopback URL"),
        (_set("discovery.career_hosts", "jobs.example.com"), "list of hostnames"),
        (_set("discovery.career_hosts", ["https://jobs.example.com"]), "must be a hostname"),
        (_set("discovery.career_hosts", ["jobs.example.com/careers"]), "must be a hostname"),
        (_set("discovery.career_hosts", ["*.example.com"]), "must be a hostname"),
        (_set("discovery.career_hosts", ["jobs..example.com"]), "must be a hostname"),
        (_set("discovery.career_hosts", ["localhost"]), "must be a hostname"),
        (
            _set("discovery.career_hosts", ["jobs.example.com", "Jobs.Example.com"]),
            "names must be unique",
        ),
        (_set("discovery.interval_minutes", 59), "integer >= 60"),
        (_set("discovery.interval_minutes", True), "integer >= 60"),
        (_set("discovery.interval_minutes", False), "integer >= 60"),
        (_set("discovery.interval_minutes", "daily"), "integer >= 60"),
        (_set("discovery.interval_minutes", 60.5), "integer >= 60"),
        (_set("discovery", "on"), "discovery must be a mapping"),
        (_set("discovery.token", "x"), "unknown keys"),
        (_set("telegram.chat_id", "me"), "telegram.chat_id"),
        (_set("telegram.chat_id", -100123), "positive numeric id"),
        (_set("telegram.token", "x"), "unknown keys"),
        (_set("telegram", "chat"), "telegram must be a mapping"),
    ],
)
def test_problems_are_reported(mutate: Any, message: str) -> None:
    d = doc()
    d["watchlist"] = template_watchlist()  # the commented examples, uncommented
    if "title_exceptions needs" in message:
        d["hard_gates"]["title_exceptions"] = ["Example Manager"]
    mutate(d)
    problems = validate_config(d)
    assert any(message in p for p in problems), problems


def test_tier_fits_name_the_three_tiers_only_with_requirement_tiers() -> None:
    assert tier_fits(doc()) == {"high": "high", "medium": "medium", "low": "low"}
    assert tier_fits(None) == {}
    assert tier_fits({"fit_rubric": {"weights": {"a": 1}}}) == {}
    assert tier_fits({"fit_rubric": {"requirement_tiers": "x"}}) == {}


def test_requirement_tiers_fill_in_their_defaults() -> None:
    assert requirement_tiers({"fit_rubric": {"requirement_tiers": {}}}) == {
        "high_max_minor": 2,
        "entry_level_extra_minor": 2,
        "exclude_at_core": 3,
        "exempt_types": [],
    }
    assert requirement_tiers({"fit_rubric": {"requirement_tiers": {"high_max_minor": 4}}}) == {
        "high_max_minor": 4,
        "entry_level_extra_minor": 2,
        "exclude_at_core": 3,
        "exempt_types": [],
    }
    assert requirement_tiers({}) is None


def test_off_lane_min_fit_defaults_to_high() -> None:
    d = doc()
    assert d["search"]["off_lane_min_fit"] == "high"
    assert off_lane_min_fit(d) == "high"
    del d["search"]["off_lane_min_fit"]
    assert validate_config(d) == []
    assert off_lane_min_fit(d) == "high"
    assert off_lane_min_fit(None) == "high"
    assert off_lane_min_fit({"search": "x"}) == "high"
    for level in ("low", "medium", "high"):
        d["search"]["off_lane_min_fit"] = level
        assert validate_config(d) == []
        assert off_lane_min_fit(d) == level


@pytest.mark.parametrize("value", ["High", "great", "", None, 2, True])
def test_a_bad_off_lane_min_fit_is_reported(value: object) -> None:
    d = doc()
    d["search"]["off_lane_min_fit"] = value
    assert validate_config(d) == [
        "search.off_lane_min_fit must be one of low | medium | high (default high)"
    ]


def test_every_problem_is_reported_in_one_pass() -> None:
    d = doc()
    d["search"]["lanes"][0]["priority"] = 0
    d["fit_rubric"]["requirement_tiers"]["exclude_at_core"] = 0
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


def test_the_seniority_option_is_optional_and_takes_true_or_false() -> None:
    d = doc()
    del d["hard_gates"]["seniority_in_description"]
    assert validate_config(d) == []
    d["hard_gates"]["seniority_in_description"] = True
    assert validate_config(d) == []


def test_an_unknown_hard_gate_names_the_key_and_no_path() -> None:
    d = doc()
    d["hard_gates"]["seniority_in_desc"] = True
    problems = validate_config(d)
    assert problems == ["hard_gates has unknown keys: ['seniority_in_desc']"]
    assert "/" not in problems[0]
