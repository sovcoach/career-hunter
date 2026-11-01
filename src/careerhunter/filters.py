"""Decide whether a fetched posting is shown, skipped, or flagged.

The checks run in one order. The first skip wins, and it is the reason stored on
`skips`. Nothing here fetches, scores, or sends a message.

1. exclusions: company, title keyword, description keyword, one role, already applied
2. hard gates: language, credential, shift, pay, seniority, then minimum salary
3. location rules, then the watchlist entry's role filters
4. schedule limits

A description keyword can match only when the list payload included a description.
Greenhouse list rows have none. The description is not stored.

One `role_filters` block is a set of AND constraints. It cannot say "this work model,
or else that place." A missing work model does not fail `work_models`, because a
Greenhouse list row has no work model.

`min_salary` applies only when it is a number. A posting that states no yearly pay is
kept. `max_commute_km` and `exclude_work_models` are not applied.

A stated weekend-day count above `weekend_days_per_month` follows `over_limit`:
`flag` sets `schedule` and keeps the posting, `exclude` skips it. An "on-call"
mention sets the schedule flag and keeps the posting, unless the weekend count already
excluded it. The word "weekends" on its own does neither.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_FOLD = re.compile(r"[\s_\-\u2010-\u2015]+")
_WEEKEND_COUNT = re.compile(r"\b(\d+)\s+weekend")
_ON_CALL = re.compile(r"\bon call\b|\boncall\b")
_MONEY = re.compile(
    r"\$\s*(?P<dollar>\d{1,3}(?:,\d{3})+|\d+)(?P<dollar_k>\s*[kK])?"
    r"|(?P<k>\d{2,3})\s*[kK]\b"
    r"|(?P<label>salary|pay|compensation)\D{0,24}(?P<amount>\d{1,3}(?:,\d{3})+|\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PostingText:
    """The fields a filter can see. Description is in memory for this decision only."""

    company: str
    title: str
    location: str
    work_model: str | None = None
    description: str | None = None
    requisition_id: str | None = None


@dataclass(frozen=True)
class Verdict:
    """A skip reason, or a schedule mark on a posting that is kept."""

    reason: str | None = None
    schedule: str | None = None


def decide(
    posting: PostingText,
    *,
    config: Mapping[str, Any],
    exclusions: Mapping[str, Any],
    role_filters: Mapping[str, Any] | None,
    applied: bool,
) -> Verdict:
    """The first matching check. No reason and no schedule means the posting is kept as-is."""
    limits = _mapping(config.get("schedule_limits"))
    reason = _excluded(posting, exclusions, applied)
    if reason is None:
        reason = _hard_gate(posting, _mapping(config.get("hard_gates")))
    if reason is None:
        reason = _location(posting, _mapping(_search(config).get("location_rules")))
    if reason is None:
        reason = _role(posting, role_filters)
    if reason is None:
        reason = _schedule_reason(posting, limits)
    if reason is not None:
        return Verdict(reason=reason)
    return Verdict(schedule=_schedule_flag(posting, limits))


def _excluded(posting: PostingText, exclusions: Mapping[str, Any], applied: bool) -> str | None:
    for name in _strings(exclusions.get("companies")):
        if _same(posting.company, name):
            return "excluded_company"
    if _any(posting.title, exclusions.get("title_keywords")):
        return "excluded_title"
    if posting.description and _any(posting.description, exclusions.get("description_keywords")):
        return "excluded_description"
    for role in exclusions.get("roles") or []:
        if isinstance(role, dict) and _role_excluded(posting, role):
            return "excluded_role"
    if applied and exclusions.get("exclude_applied") is True:
        return "exclude_applied"
    return None


def _role_excluded(posting: PostingText, role: Mapping[str, Any]) -> bool:
    company, title = role.get("company"), role.get("title")
    if not isinstance(company, str) or not isinstance(title, str):
        return False
    if not _same(posting.company, company) or not _has(posting.title, title):
        return False
    location = role.get("location")
    if isinstance(location, str) and not _has(posting.location, location):
        return False
    requisition = role.get("requisition_id")
    return not (
        isinstance(requisition, str) and _fold(posting.requisition_id or "") != _fold(requisition)
    )


def _hard_gate(posting: PostingText, gates: Mapping[str, Any]) -> str | None:
    body = _body(posting)
    checks = (
        ("required_languages", "required_language"),
        ("credentials", "credential"),
        ("shift_patterns", "shift_pattern"),
        ("pay_structures", "pay_structure"),
    )
    for key, reason in checks:
        if _any(body, gates.get(key)):
            return reason
    if _seniority(posting.title, body, gates):
        return "seniority"
    if _under_minimum(body, gates.get("min_salary")):
        return "min_salary"
    return None


def _seniority(title: str, body: str, gates: Mapping[str, Any]) -> bool:
    words = gates.get("seniority_words")
    if not _strings(words):
        return False
    if _any(title, gates.get("title_exceptions")):
        return False
    in_title = _any(title, words)
    if not in_title and not _any(body, words):
        return False
    # A never-gated family is skipped only when the title itself is explicitly senior.
    return not (_any(title, gates.get("never_gated_families")) and not in_title)


def _under_minimum(body: str, minimum: object) -> bool:
    """True when the highest stated yearly figure is below a configured minimum.

    No figure, or a minimum left unset, is not a decision. A range is judged by its
    highest figure, so a range that reaches the minimum is kept.
    """
    if isinstance(minimum, bool) or not isinstance(minimum, int | float):
        return False
    amounts = _yearly_amounts(body)
    return bool(amounts) and max(amounts) < float(minimum)


def _yearly_amounts(text: str) -> list[float]:
    found: list[float] = []
    for match in _MONEY.finditer(text):
        if match.group("dollar") is not None:
            amount = _number(match.group("dollar"), thousands=match.group("dollar_k") is not None)
            # A dollar amount under 1000 is hourly or a fragment, not yearly pay.
            if amount is not None and (match.group("dollar_k") or amount >= 1000):
                found.append(amount)
        elif match.group("k") is not None:
            amount = _number(match.group("k"), thousands=True)
            if amount is not None:
                found.append(amount)
        else:
            amount = _number(match.group("amount") or "", thousands=False)
            if amount is not None and amount >= 10000:
                found.append(amount)
    return found


def _number(raw: str, *, thousands: bool) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    if thousands:
        value *= 1000
    return value


def _location(posting: PostingText, rules: Mapping[str, Any]) -> str | None:
    if not rules:
        return None
    places = _places_for(posting.work_model, rules)
    if places and not _any(posting.location, places):
        return "location_rule"
    if rules.get("allow_travel_outside") is False and _outside_travel(posting, rules):
        return "location_rule"
    return None


def _places_for(work_model: str | None, rules: Mapping[str, Any]) -> list[str] | None:
    """Places the work model has to match, or None when this model is not constrained."""
    if work_model in ("onsite", "hybrid"):
        return _strings(rules.get("onsite_hybrid"))
    if work_model == "remote":
        return _strings(rules.get("remote"))
    return None


def _outside_travel(posting: PostingText, rules: Mapping[str, Any]) -> bool:
    if not _has(_body(posting), "travel") or not posting.location.strip():
        return False
    allowed = _strings(rules.get("onsite_hybrid")) + _strings(rules.get("remote"))
    return bool(allowed) and not _any(posting.location, allowed)


def _role(posting: PostingText, filters: Mapping[str, Any] | None) -> str | None:
    if not filters:
        return None
    families = _strings(filters.get("role_families"))
    if families and not _any(posting.title, families):
        return "role_filter"
    models = _strings(filters.get("work_models"))
    if models and posting.work_model is not None and posting.work_model not in models:
        return "role_filter"
    places = _strings(filters.get("locations"))
    if places and not _any(posting.location, places):
        return "role_filter"
    return None


def _schedule_reason(posting: PostingText, limits: Mapping[str, Any]) -> str | None:
    if _over_weekend(posting, limits) and limits.get("over_limit", "flag") == "exclude":
        return "schedule_limit"
    return None


def _schedule_flag(posting: PostingText, limits: Mapping[str, Any]) -> str | None:
    marks: list[str] = []
    if _over_weekend(posting, limits):
        marks.append("over_weekend_limit")
    if _ON_CALL.search(_fold(_body(posting))):
        marks.append("on_call")
    return " ".join(marks) or None


def _over_weekend(posting: PostingText, limits: Mapping[str, Any]) -> bool:
    cap = limits.get("weekend_days_per_month")
    count = _weekend_count(_body(posting))
    return isinstance(cap, int) and not isinstance(cap, bool) and count is not None and count > cap


def _weekend_count(text: str) -> int | None:
    match = _WEEKEND_COUNT.search(_fold(text))
    if match is None:
        return None
    return int(match.group(1))


def _body(posting: PostingText) -> str:
    if posting.description:
        return f"{posting.title}\n{posting.description}"
    return posting.title


def _search(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(config.get("search"))


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _fold(value: str) -> str:
    return _FOLD.sub(" ", value.casefold()).strip()


def _same(left: str, right: str) -> bool:
    return _fold(left) == _fold(right)


def _has(haystack: str, needle: str) -> bool:
    folded = _fold(needle)
    return bool(folded) and folded in _fold(haystack)


def _any(haystack: str, needle: object) -> bool:
    return any(_has(haystack, item) for item in _strings(needle))
