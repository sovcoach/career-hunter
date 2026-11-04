"""A scout prompt built from the exclusions and hard gates the watcher skips on.

The command prints this text and writes nothing. The prompt is not stored in the repo.
Empty lists are left out. Commute distance and excluded work models are not skip rules,
so they are left out too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render(config: Mapping[str, Any], exclusions: Mapping[str, Any]) -> str:
    """One prompt. No file, database, network, or model."""
    lines = [
        "You are scouting job postings. Offer a role only when it survives every rule below.",
        "Skip it otherwise. Do not invent exceptions.",
        *_section("Exclusions", _exclusion_rules(exclusions)),
        *_section("Hard gates", _gate_rules(_mapping(config.get("hard_gates")))),
        "",
        "These rules are the same ones careerhunter watch uses to skip a posting.",
    ]
    return "\n".join(lines) + "\n"


def _section(title: str, rules: list[str]) -> list[str]:
    if not rules:
        return []
    return ["", title, *[f"- {rule}" for rule in rules]]


def _exclusion_rules(exclusions: Mapping[str, Any]) -> list[str]:
    rules: list[str] = []
    companies = _texts(exclusions.get("companies"))
    if companies:
        rules.append(f"Skip these companies: {_join(companies)}.")
    titles = _texts(exclusions.get("title_keywords"))
    if titles:
        rules.append(f"Skip a title containing any of: {_join(titles)}.")
    descriptions = _texts(exclusions.get("description_keywords"))
    if descriptions:
        rules.append(f"Skip a description containing any of: {_join(descriptions)}.")
    rules.extend(_role_rules(exclusions.get("roles")))
    if exclusions.get("exclude_applied") is True:
        rules.append("Do not offer a role the operator has already applied to.")
    return rules


def _role_rules(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    rules: list[str] = []
    for role in value:
        if not isinstance(role, Mapping):
            continue
        company, title = role.get("company"), role.get("title")
        if not isinstance(company, str) or not isinstance(title, str):
            continue
        if not company.strip() or not title.strip():
            continue
        detail = f"{company.strip()}, title {title.strip()}"
        location = role.get("location")
        if isinstance(location, str) and location.strip():
            detail = f"{detail}, location {location.strip()}"
        requisition = role.get("requisition_id")
        if isinstance(requisition, str) and requisition.strip():
            detail = f"{detail}, requisition {requisition.strip()}"
        rules.append(f"Skip this one role: {detail}.")
    return rules


def _gate_rules(gates: Mapping[str, Any]) -> list[str]:
    rules: list[str] = []
    named = (
        ("required_languages", "Skip a posting that requires any of these languages"),
        ("credentials", "Skip a posting that requires any of these credentials"),
        ("shift_patterns", "Skip a posting with any of these shift patterns"),
        ("pay_structures", "Skip a posting with any of these pay structures"),
    )
    for key, lead in named:
        values = _texts(gates.get(key))
        if values:
            rules.append(f"{lead}: {_join(values)}.")
    words = _texts(gates.get("seniority_words"))
    if words:
        # The watcher checks the description only when the option is on.
        where = "title or description" if gates.get("seniority_in_description") is True else "title"
        rules.append(
            f"Skip a {where} with any of these seniority words as whole words, "
            f"ignoring case: {_join(words)}."
        )
        allowed = _texts(gates.get("title_exceptions"))
        if allowed:
            rules.append(
                f"These titles stay allowed, as whole words, ignoring case: {_join(allowed)}."
            )
        families = _texts(gates.get("never_gated_families"))
        if families:
            rules.append(
                "These families, as whole words, ignoring case, are gated for seniority only "
                f"when the title itself contains one of those words: {_join(families)}."
            )
    salary = _salary(gates.get("min_salary"))
    if salary is not None:
        rules.append(f"Skip a posting whose stated yearly pay is below {salary}.")
    return rules


def _salary(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _join(items: list[str]) -> str:
    return ", ".join(items)
