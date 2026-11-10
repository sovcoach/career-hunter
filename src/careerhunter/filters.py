"""Decide whether a fetched posting is shown, skipped, or flagged.

The checks run in one order. The first skip wins, and it is the reason stored on
`skips`. Nothing here fetches, scores, or sends a message.

1. exclusions: company, title keyword, description keyword, one role, already applied
2. hard gates: language, credential, shift, pay, seniority, then minimum salary
3. location: an empty location, then the location rules; then the watchlist
   entry's role filters
4. schedule limits

A description keyword can match only when the board sent a description. The
description is held in memory for the decision and is not stored, so a recheck of a
stored posting runs the description checks without it.

The work model. A board that gives one (Lever, Ashby) is used as given. With none
(Greenhouse), a location that has the whole word "remote" is inferred remote;
otherwise the location rules treat the posting as on-site. On-site and hybrid must
match `search.location_rules.onsite_hybrid`, remote must match `remote`. An inferred
remote posting may match either list, because "Example City or Remote" can be
either. A list that is unset or empty does not constrain its work models.

With `search.location_rules` set, a posting with blank location text and no work
model from the board is skipped as `empty_location`. A work model from the board
stands in for the location: such a posting is not skipped as empty, and with no text
there is nothing to match against the lists, so they do not skip it either. Without
location rules an empty location is not a reason to skip.

Seniority words match whole words of the title, ignoring case. The description is
checked too only when `seniority_in_description` is true. `title_exceptions` and
`never_gated_families` match whole words of the title the same way. A never-gated
family is gated only when the title itself has a seniority word.

One `role_filters` block is a set of AND constraints. It cannot say "this work model,
or else that place." `work_models` checks the board's work model, or remote when it is
inferred from the location. Otherwise the work model is unknown, and an unknown work
model does not fail `work_models`: the on-site default is for the location rules only.

`min_salary` applies only when it is a number, and only to figures in a pay context,
converted to yearly CAD (`_yearly_amounts` has the rules). A posting is skipped only
when the top of its highest stated range is below the minimum; a posting with no
countable figure is kept. `max_commute_km` and `exclude_work_models` are not applied.

A stated weekend-day count above `weekend_days_per_month` follows `over_limit`:
`flag` sets `schedule` and keeps the posting, `exclude` skips it. An "on-call"
mention sets the schedule flag and keeps the posting, unless the weekend count already
excluded it. The word "weekends" on its own does neither.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_FOLD = re.compile(r"[\s_\-\u2010-\u2015]+")
_WEEKEND_COUNT = re.compile(r"\b(\d+)\s+weekend")
_ON_CALL = re.compile(r"\bon call\b|\boncall\b")
# Salary parsing (`_yearly_amounts`). A sentence ends at a line break, after ! ? or ;,
# or at a full stop followed by a space and a capital, so "$22.50" stays whole.
_SENTENCE = re.compile(r"[\n\r]+|(?<=[!?;])\s+|(?<=\.)\s+(?=[A-Z])")
# A figure, with what marks it as money: a currency before it (`$`, `C$`, `US$`, `€`,
# `£`, or a three-letter code), a `k` after it, or a three-letter code after it.
_FIGURE = re.compile(
    r"(?P<pre>\b[A-Z]{1,3}\$\s?|\b[A-Z]{3}\s?[$€£]?\s?|[$€£]\s?)?"
    r"(?<![\w.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d,]*\d)"
    r"(?P<k>\s?[kK](?![A-Za-z(]))?"
    r"(?P<post>\s?[A-Z]{3}\b)?"
)
_RANGE_JOIN = re.compile(r"\s*(?:-|\u2013|\u2014|to)\s*", re.IGNORECASE)
_UNITS = {
    "hour": "hour",
    "hr": "hour",
    "h": "hour",
    "hourly": "hour",
    "year": "year",
    "yr": "year",
    "annum": "year",
    "annually": "year",
    "yearly": "year",
    "annual": "year",
    "month": "month",
    "mo": "month",
    "monthly": "month",
    "week": "week",
    "wk": "week",
    "weekly": "week",
    "day": "day",
    "daily": "day",
}
_UNIT_AFTER = re.compile(
    r"\s*(?:(?:/|per|an?|each)\s*(?P<unit>hour|hr|h|year|yr|annum|month|mo|week|wk|day)\b"
    r"|(?P<adverb>hourly|annually|yearly|monthly|weekly|daily)\b)",
    re.IGNORECASE,
)
_UNIT_IN_SENTENCE = re.compile(
    r"\b(?:hourly|annually|yearly|monthly|weekly|daily"
    r"|(?:per|an?)\s+(?:hour|year|annum|month|week|day))\b|/\s?(?:hr|hour|yr|year)\b",
    re.IGNORECASE,
)
# 40 hours a week, 52 weeks a year, and 260 working days a year.
_PER_YEAR = {"hour": 2080.0, "day": 260.0, "week": 52.0, "month": 12.0, "year": 1.0}
_PAY_WORD = re.compile(
    r"\b(?:salary|salaries|pay|pays|paid|compensation|base|wages?|earnings|range)\b",
    re.IGNORECASE,
)
_NOT_PAY = r"(?:bonus(?:es)?|signing|sign[- ]on|relocation|equity|stock|rsus?|options)"
_NOT_PAY_BEFORE = re.compile(rf"\b{_NOT_PAY}\b", re.IGNORECASE)
_NOT_PAY_AFTER = re.compile(
    rf"\s*(?:(?:annual|one[- ]time|target|cash|performance)\s+)?{_NOT_PAY}\b", re.IGNORECASE
)
# Where the words before a figure stop belonging to it.
_CLAUSE_BREAK = re.compile(r"[,;(+]|\bplus\b|\band\b", re.IGNORECASE)


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
    if _seniority(posting, gates):
        return "seniority"
    if _under_minimum(body, gates.get("min_salary")):
        return "min_salary"
    return None


def _seniority(posting: PostingText, gates: Mapping[str, Any]) -> bool:
    words = gates.get("seniority_words")
    if not _strings(words):
        return False
    title = posting.title
    if _any_phrase(title, gates.get("title_exceptions")):
        return False
    in_title = _any_phrase(title, words)
    in_description = (
        gates.get("seniority_in_description") is True
        and bool(posting.description)
        and _any_phrase(posting.description or "", words)
    )
    if not in_title and not in_description:
        return False
    # A never-gated family is skipped only when the title itself is explicitly senior.
    return not (_any_phrase(title, gates.get("never_gated_families")) and not in_title)


def _under_minimum(body: str, minimum: object) -> bool:
    """True when the highest stated yearly figure is below a configured minimum.

    No figure, or a minimum left unset, is not a decision. A range is judged by its
    highest figure, so a range that reaches the minimum is kept.
    """
    if isinstance(minimum, bool) or not isinstance(minimum, int | float):
        return False
    amounts = _yearly_amounts(body)
    return bool(amounts) and max(amounts) < float(minimum)


@dataclass(frozen=True)
class _Figure:
    value: float
    start: int
    end: int
    marked: bool  # a currency or a `k` marks it as money
    foreign: bool  # marked as a currency other than CAD
    k: bool  # written with a `k` for thousands


def _yearly_amounts(text: str) -> list[float]:
    """Every pay figure in `text`, as yearly CAD.

    Only a figure in a pay context counts: its sentence has a pay word (salary, pay,
    compensation, base, wage, range...), or a unit follows it (per hour, /yr, annually...).
    A bonus, signing, relocation or equity amount is not pay, whether the word comes
    just before the figure in the same clause or right after it. A figure marked as
    another currency (USD, US$, €, £, any three-letter code but CAD) is left out. A
    plain `$` is taken as CAD. A range shares its unit, currency and `k`.

    The unit after a figure wins; otherwise a single unit named in the sentence applies;
    otherwise the figure is yearly. Hourly x 2080 (40 h x 52), daily x 260, weekly x 52,
    monthly x 12. A yearly figure under 1000, or a bare number under 10000 with no
    currency, `k` or unit, is a fragment and is left out.
    """
    found: list[float] = []
    for sentence in _SENTENCE.split(text):
        found.extend(_sentence_amounts(sentence))
    return found


def _sentence_amounts(sentence: str) -> list[float]:
    figures = _figures(sentence)
    found: list[float] = []
    pay_word = _PAY_WORD.search(sentence) is not None
    named = {_UNITS[_unit_word(m.group(0))] for m in _UNIT_IN_SENTENCE.finditer(sentence)}
    index = 0
    while index < len(figures):
        group = [figures[index]]
        join = _RANGE_JOIN.match(sentence, figures[index].end)
        if join is not None and index + 1 < len(figures) and figures[index + 1].start == join.end():
            group.append(figures[index + 1])
        index += len(group)
        found.extend(_group_amounts(sentence, group, pay_word, named))
    return found


def _group_amounts(
    sentence: str, group: list[_Figure], pay_word: bool, named: set[str]
) -> list[float]:
    """One figure, or both ends of a range, as yearly amounts; [] when they don't count."""
    first, last = group[0], group[-1]
    if any(figure.foreign for figure in group):
        return []
    after = _UNIT_AFTER.match(sentence, last.end)
    tail = after.end() if after is not None else last.end
    if _NOT_PAY_AFTER.match(sentence, tail) or _not_pay_before(sentence[: first.start]):
        return []
    if after is not None:
        unit: str | None = _UNITS[(after.group("unit") or after.group("adverb")).casefold()]
    elif len(named) == 1:
        unit = next(iter(named))
    elif named:
        return []  # two different units in one sentence, neither attached: unclear
    else:
        unit = None
    if not pay_word and unit is None:
        return []  # no pay context
    marked = any(figure.marked for figure in group)
    values = [figure.value for figure in group]
    if len(group) == 2 and last.k and not first.k and values[0] < 1000:
        values[0] *= 1000  # "80-100k"
    if unit is None or unit == "year":
        explicit = after is not None
        floor = 1000.0 if marked or explicit else 10000.0
        return [value for value in values if value >= floor]
    return [value * _PER_YEAR[unit] for value in values if value > 0]


def _figures(sentence: str) -> list[_Figure]:
    figures: list[_Figure] = []
    for match in _FIGURE.finditer(sentence):
        raw = match.group("num")
        k = match.group("k") is not None
        if raw == "401" and k:
            continue  # a retirement plan, not pay
        try:
            value = float(raw.replace(",", ""))
        except ValueError:  # pragma: no cover - the pattern only matches numbers
            continue
        if k:
            value *= 1000
        pre = (match.group("pre") or "").strip()
        post = (match.group("post") or "").strip()
        figures.append(
            _Figure(
                value=value,
                start=match.start(),
                end=match.end(),
                marked=bool(pre or post or k),
                foreign=_foreign(pre) or _foreign(post),
                k=k,
            )
        )
    return figures


def _foreign(marker: str) -> bool:
    """A currency marker that is not CAD. A plain `$` is CAD."""
    if not marker:
        return False
    if "€" in marker or "£" in marker:
        return True
    letters = "".join(ch for ch in marker if ch.isalpha())
    if "$" in marker:
        return letters not in ("", "C", "CA", "CAD")
    return letters != "CAD"


def _unit_word(text: str) -> str:
    word = re.sub(r"^(?:per|an?)\s+|^/\s?", "", text.strip(), flags=re.IGNORECASE)
    return word.casefold()


def _not_pay_before(before: str) -> bool:
    window = before[-40:]
    breaks = list(_CLAUSE_BREAK.finditer(window))
    if breaks:
        window = window[breaks[-1].end() :]
    return _NOT_PAY_BEFORE.search(window) is not None


def _location(posting: PostingText, rules: Mapping[str, Any]) -> str | None:
    if not rules:
        return None
    blank = not posting.location.strip()
    if blank and posting.work_model is None:
        return "empty_location"
    if not blank and not _place_allowed(posting, rules):
        return "location_rule"
    if rules.get("allow_travel_outside") is False and _outside_travel(posting, rules):
        return "location_rule"
    return None


def _place_allowed(posting: PostingText, rules: Mapping[str, Any]) -> bool:
    """Whether the location matches the list for its work model. An empty list allows all."""
    onsite = _strings(rules.get("onsite_hybrid"))
    remote = _strings(rules.get("remote"))
    if posting.work_model in ("onsite", "hybrid"):
        return not onsite or _any(posting.location, onsite)
    if posting.work_model == "remote":
        return not remote or _any(posting.location, remote)
    if posting.work_model is None and inferred_remote(posting.location):
        # Inferred, not given: "Example City or Remote" may be either kind of job.
        return not remote or _any(posting.location, remote) or _any(posting.location, onsite)
    # No work model from the board and no "remote": treated as on-site.
    return not onsite or _any(posting.location, onsite)


def inferred_remote(location: str) -> bool:
    """True when the location text has the whole word "remote", ignoring case."""
    return has_phrase(location, "remote")


def _role_work_model(posting: PostingText) -> str | None:
    """The board's work model, or remote when inferred; None when unknown."""
    if posting.work_model is not None:
        return posting.work_model
    return "remote" if inferred_remote(posting.location) else None


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
    work_model = _role_work_model(posting)
    if models and work_model is not None and work_model not in models:
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
    """NFKC, so composed and decomposed accents and ligatures compare alike; then case,
    then NFKC again, because casefolding can leave a string that is not normalized
    ("İ" becomes "i" and a combining dot); then runs of separators as one space."""
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())
    return _FOLD.sub(" ", folded).strip()


def _same(left: str, right: str) -> bool:
    return _fold(left) == _fold(right)


def _has(haystack: str, needle: str) -> bool:
    folded = _fold(needle)
    return bool(folded) and folded in _fold(haystack)


def _any(haystack: str, needle: object) -> bool:
    return any(_has(haystack, item) for item in _strings(needle))


def _any_phrase(haystack: str, needle: object) -> bool:
    return any(has_phrase(haystack, item) for item in _strings(needle))


def has_phrase(text: str, phrase: str) -> bool:
    """True when `phrase` appears in `text` as whole words, ignoring case.

    Both sides are folded the same way as every other match here: NFKC, case, then runs
    of spaces, underscores, hyphens and dashes become one space, so "Vice President"
    matches "Vice-President". A match must have no letter, digit or combining mark
    directly before or after it: "Lead" matches "Team Lead" and "(Lead)", not "Leader",
    "Misleading", or "Lead" with an accent mark on its last letter. Punctuation inside
    the phrase is kept, so "Sr." matches "Sr. Manager". A blank phrase matches nothing.
    """
    needle = _fold(phrase)
    if not needle:
        return False
    haystack = _fold(text)
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        if not _in_word(haystack, start - 1) and not _in_word(haystack, end):
            return True
        start = haystack.find(needle, start + 1)
    return False


def _in_word(text: str, index: int) -> bool:
    """Whether the character at `index` is a letter, a digit or a combining mark.

    Python's \\w leaves combining marks out, so a decomposed accent would otherwise
    end a word.
    """
    if not 0 <= index < len(text):
        return False
    return unicodedata.category(text[index])[0] in "LNM"


def too_old(
    posted: dt.date | None, at_least: int | None, today: dt.date, max_days: int
) -> bool | None:
    """Whether a posting is older than `max_days` (#36); None when its age is not known.

    `posted` is the board's posted date. `at_least` is a lower bound on the age in days
    when the board gives only that (Workday's "30+ Days Ago"): it decides only when it
    is already over the limit. A date in the future is not too old.
    """
    if posted is not None:
        return (today - posted).days > max_days
    if at_least is not None and at_least > max_days:
        return True
    return None


# A requisition code: one token of letters, digits, hyphens and underscores, holding at
# least three digits ("R12345", "JR-2041", "104332"). "Level 2" has a space, so it is not.
_REQ_SHAPE = re.compile(r"[A-Za-z0-9_-]+")
_BRACKETED = re.compile(r"[(\[]\s*([^()\[\]\s]+)\s*[)\]]")
_TRAILING = re.compile(r"(?:\s+[-\u2013\u2014|:#/]|,)\s*([^\s\-\u2013\u2014|:#/,]\S*)\s*$")
_NOT_WORD = re.compile(r"[\W_]+")


def _requisition_code(token: str) -> bool:
    return bool(_REQ_SHAPE.fullmatch(token)) and sum(ch.isdigit() for ch in token) >= 3


def title_key(title: str) -> str:
    """A title folded for repost matching (#34). Two postings at one company are the same
    role when their keys are equal.

    NFKC and case folding first. Then a bracketed segment, or a trailing segment after a
    separator (a space then `-`, a dash, `|`, `:`, `#` or `/`; or a comma), is dropped
    only when it is one
    requisition code: a single token of letters, digits, `-` or `_`, with at least three
    digits and no spaces, such as "(R12345)", "[JR-2041]" or "- 104332". "(Level 2)" and
    "(Remote)" are kept. Last, every run of punctuation and spaces becomes one space.
    """
    text = unicodedata.normalize("NFKC", title).casefold()
    text = _BRACKETED.sub(lambda m: " " if _requisition_code(m.group(1)) else m.group(0), text)
    while (match := _TRAILING.search(text)) is not None and _requisition_code(match.group(1)):
        text = text[: match.start()]
    return " ".join(_NOT_WORD.sub(" ", text).split())
