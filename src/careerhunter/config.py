"""Load and validate the user's config.yaml and exclusions.yaml.

Everything personal about a search (target titles, locations, fit rubric, hard gates,
schedule rules, watchlist, EI claims) lives in these files in the data directory, never
in code. The EI claim section is checked by claims.validate_section.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from careerhunter.claims import validate_section
from careerhunter.enums import ATS_TYPES, WORK_MODELS


class ConfigError(Exception):
    """Raised with a list of every problem found, so the user can fix them in one pass."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{path} has {len(problems)} problem(s):\n{detail}")


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _str_list(doc: dict[str, Any], key: str, where: str, problems: list[str]) -> None:
    value = doc.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        problems.append(f"{where}.{key} must be a list of non-empty strings")


def _number(doc: dict[str, Any], key: str, where: str, problems: list[str]) -> None:
    value = doc.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
        problems.append(f"{where}.{key} must be a number")


def _section(doc: dict[str, Any], key: str, problems: list[str]) -> dict[str, Any]:
    value = doc.get(key)
    if not isinstance(value, dict):
        problems.append(f"{key} must be a mapping")
        return {}
    return value


def validate_config(doc: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["top level must be a mapping"]

    search = _section(doc, "search", problems)
    if search:
        _str_list(search, "target_titles", "search", problems)
        _str_list(search, "locations", "search", problems)
        models = search.get("work_models", [])
        if not isinstance(models, list) or any(m not in WORK_MODELS for m in models):
            problems.append(f"search.work_models must only contain {list(WORK_MODELS)}")

    gates = _section(doc, "hard_gates", problems)
    for key in ("min_salary", "max_commute_km"):
        _number(gates, key, "hard_gates", problems)
    excluded = gates.get("exclude_work_models", [])
    if not isinstance(excluded, list) or any(m not in WORK_MODELS for m in excluded):
        problems.append(f"hard_gates.exclude_work_models must only contain {list(WORK_MODELS)}")

    rubric = _section(doc, "fit_rubric", problems)
    weights = rubric.get("weights")
    if not isinstance(weights, dict) or not weights:
        problems.append("fit_rubric.weights must be a non-empty mapping")
    else:
        for name, weight in weights.items():
            if isinstance(weight, bool) or not isinstance(weight, int | float) or weight < 0:
                problems.append(f"fit_rubric.weights.{name} must be a non-negative number")
    thresholds = rubric.get("thresholds")
    if not isinstance(thresholds, dict):
        problems.append("fit_rubric.thresholds must be a mapping")
    else:
        for key, value in thresholds.items():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
                problems.append(f"fit_rubric.thresholds.{key} must be an integer 0-10")

    schedule = _section(doc, "schedule", problems)
    if schedule:
        if not isinstance(schedule.get("timezone"), str):
            problems.append("schedule.timezone must be a string like America/Toronto")
        interval = schedule.get("watcher_interval_minutes")
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 15:
            problems.append("schedule.watcher_interval_minutes must be an integer >= 15")

    watchlist = doc.get("watchlist", [])
    if not isinstance(watchlist, list):
        problems.append("watchlist must be a list")
    else:
        for i, item in enumerate(watchlist):
            where = f"watchlist[{i}]"
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                problems.append(f"{where} needs a name")
                continue
            ats = item.get("ats_type")
            if ats is not None and ats not in ATS_TYPES:
                problems.append(f"{where}.ats_type must be one of {list(ATS_TYPES)}")

    problems.extend(validate_section(doc.get("ei")))
    return problems


def validate_exclusions(doc: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["top level must be a mapping"]
    for key in ("companies", "title_keywords", "description_keywords"):
        if key in doc:
            _str_list(doc, key, "exclusions", problems)
    return problems


def load_config(path: Path) -> dict[str, Any]:
    doc = _load_yaml(path)
    problems = validate_config(doc)
    if problems:
        raise ConfigError(path, problems)
    result: dict[str, Any] = doc
    return result


def load_exclusions(path: Path) -> dict[str, Any]:
    doc = _load_yaml(path)
    problems = validate_exclusions(doc)
    if problems:
        raise ConfigError(path, problems)
    result: dict[str, Any] = doc
    return result
