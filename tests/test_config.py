from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from careerhunter.config import (
    ConfigError,
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
