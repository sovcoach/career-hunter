from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from careerhunter.config import (
    ConfigError,
    alerts_max_per_poll,
    load_config,
    load_exclusions,
    validate_config,
    validate_exclusions,
)

ROOT = Path(__file__).parents[1]


def example() -> dict[str, object]:
    doc = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert isinstance(doc, dict)
    return doc


def test_example_templates_are_valid() -> None:
    config = load_config(ROOT / "config.example.yaml")
    assert config["schedule"]["timezone"]
    assert config["ei"] == {"active_claim": None, "claims": []}
    assert load_exclusions(ROOT / "exclusions.example.yaml")["companies"]


def test_config_without_an_ei_section_is_valid() -> None:
    doc = example()
    doc.pop("ei")
    assert validate_config(doc) == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("search"), "search must be a mapping"),
        (lambda d: d["search"].update(target_titles="Clerk"), "target_titles"),
        (lambda d: d["search"].update(work_models=["office"]), "work_models"),
        (lambda d: d["hard_gates"].update(min_salary="lots"), "min_salary"),
        (lambda d: d["hard_gates"].update(exclude_work_models=["moon"]), "exclude_work_models"),
        (lambda d: d["fit_rubric"].update(weights={}), "weights"),
        (lambda d: d["fit_rubric"]["weights"].update(title_match=-1), "title_match"),
        (lambda d: d["fit_rubric"].update(thresholds={"strong": 11}), "thresholds.strong"),
        (lambda d: d["schedule"].update(watcher_interval_minutes=1), "watcher_interval"),
        (lambda d: d["schedule"].update(timezone=5), "timezone"),
        (lambda d: d.update(watchlist=[{"ats_type": "lever"}]), "needs a name"),
        (lambda d: d.update(watchlist=[{"name": "X", "ats_type": "taleo9"}]), "ats_type"),
        (lambda d: d.update(watchlist="Acme"), "watchlist must be a list"),
        (lambda d: d.update(ei="2026-27"), "ei must be a mapping"),
        (lambda d: d["ei"].update(active_claim="2026-27"), "is not one of the claims listed"),
        (
            lambda d: d["ei"].update(claims=[{"label": "2026-27", "start": "2026-08-05"}]),
            "needs an end date",
        ),
    ],
)
def test_config_problems_reported(mutate: object, message: str) -> None:
    doc = example()
    mutate(doc)  # type: ignore[operator]
    assert any(message in p for p in validate_config(doc))


def test_bad_config_file_raises_with_all_problems(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("search: []\nwatchlist: 3\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert len(exc.value.problems) >= 4


def test_non_mapping_documents() -> None:
    assert validate_config([]) == ["top level must be a mapping"]
    assert validate_exclusions("x") == ["top level must be a mapping"]
    assert validate_exclusions({"companies": [1]}) != []


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("10", "alerts must be a mapping"),
        ({"max_per_poll": 0}, "alerts.max_per_poll must be an integer from 1 to 50"),
        ({"max_per_poll": 51}, "alerts.max_per_poll"),
        ({"max_per_poll": 2.5}, "alerts.max_per_poll"),
        ({"max_per_poll": True}, "alerts.max_per_poll"),
        ({"max_per_pol": 5}, "alerts has unknown keys"),
    ],
)
def test_alerts_section_is_checked(section: object, message: str) -> None:
    doc = example()
    doc["alerts"] = section
    assert any(message in problem for problem in validate_config(doc))


def test_alerts_max_per_poll_defaults_to_ten() -> None:
    doc = example()
    assert validate_config({**doc, "alerts": None}) == []
    assert validate_config({**doc, "alerts": {"max_per_poll": 50}}) == []
    assert alerts_max_per_poll({}) == 10
    assert alerts_max_per_poll({"alerts": None}) == 10
    assert alerts_max_per_poll({"alerts": {"max_per_poll": 3}}) == 3


def _workday(**keys: object) -> dict[str, object]:
    return {
        "name": "Example Robotics",
        "ats_type": "workday",
        "ats_slug": "examplerobotics",
        **keys,
    }


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (_workday(ats_host="wd", ats_site="External"), "ats_host must be wd and up to three"),
        (_workday(ats_host="wd5x", ats_site="External"), "ats_host must be wd and up to three"),
        (_workday(ats_host=5, ats_site="External"), "ats_host must be wd and up to three"),
        (_workday(ats_host="wd5", ats_site="a/b"), "ats_site must be one path segment"),
        (_workday(ats_host="wd5", ats_site=""), "ats_site must be one path segment"),
        (
            {"name": "X", "ats_type": "lever", "ats_slug": "x", "ats_host": "wd5"},
            "ats_host is for ats_type workday only",
        ),
        ({"name": "X", "ats_site": "External"}, "ats_site is for ats_type workday only"),
    ],
)
def test_workday_identity_problems_reported(item: dict[str, object], message: str) -> None:
    doc = example()
    doc["watchlist"] = [item]
    assert any(message in p for p in validate_config(doc))


@pytest.mark.parametrize(
    "item",
    [
        _workday(ats_host="wd5", ats_site="External"),
        _workday(ats_host="wd103", ats_site="Careers_Site-1"),
        _workday(),  # valid; the watcher logs and skips it
    ],
)
def test_workday_identity_accepted(item: dict[str, object]) -> None:
    doc = example()
    doc["watchlist"] = [item]
    assert validate_config(doc) == []
