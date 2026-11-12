"""Load and validate the user's config.yaml and exclusions.yaml.

Everything personal about a search (target titles, locations, fit rubric, hard gates,
schedule rules, watchlist, EI claims, the Telegram chat) lives in these files in the
data directory, never in code. The EI claim section is checked by
claims.validate_section.

The structured fit criteria (issue #9) are optional sections alongside the original
fields. Validation lives here. `careerhunter watch` applies exclusions, hard gates,
location rules, role filters and schedule limits. `score.py` then scores
survivors against `gap_tiers` on a local loopback model and leaves `fit_rating` null.
The bot sends one alert digest per watch run. Quiet hours are not
implemented. `schedule.watcher_interval_minutes` is how often the watch
LaunchAgent runs, default 60 and at least 15.

- search.lanes: named groups of target titles, by priority, optionally a fallback;
- search.off_lane_min_fit: the lowest fit (low, medium or high, default high) at which
  a posting whose title matches no lane still alerts;
- search.location_rules: places for on-site and hybrid work, regions for remote;
- fit_rubric.gap_tiers: a rating from the number of missing requirements. Every tier
  names its fit (low, medium or high), and a boundary outcome takes the fit of the
  tier it names;
- hard_gates: language, credential, shift-pattern, pay-structure and seniority gates,
  with title exceptions;
- schedule_limits: a cap on weekend work, flagged rather than excluded;
- watchlist role filters and an agency flag;
- exclusions: role-level exclusions and "exclude roles already applied to";
- alerts.max_per_poll: the per-poll cap on watcher alerts, an integer from 1 to 50,
  default 10.
- alerts.weekly_digest: the day (monday to sunday) and hour (0 to 23) in
  schedule.timezone of the weekly counts by lane, or null for none;
- learning.labelled_examples: how many of the operator's recent labelled postings the
  scoring prompt shows the local model, an integer from 0 to 20, default 5 (0: none);
- health.unproductive_runs: how many watch runs or discovery passes in a row may do no
  useful work before doctor warns, and health.failure_runs: how many runs in a row the
  local model, SearXNG or every board may fail before the bot says so. Both are
  integers from 1 to 50, default 3.
- scoring.chat_model and scoring.embedding_model: the model identifiers sent to the
  local server, text of 1 to 200 characters with no control characters; defaults
  `local-model` and `local-embedding`. The server URLs are not config.

With lanes present, search.target_titles becomes optional; with gap_tiers present, the
fit_rubric weights and thresholds do; and schedule.watcher_interval_minutes is always
optional. search.resume and fit_rubric.embedding_weight are optional. resume is a path
relative to the data folder. When it is set, the watcher stores a cosine beside the
tier. embedding_weight is a number from 0 to 1. The watcher does not read it for
skips, alerts, or stored scores. embed-eval may rank a blend with it.
discovery.searxng_url and discovery.career_hosts are optional. A null URL leaves
discovery off. The URL is plain http to a loopback host, with no userinfo. A career
host is a hostname only. discovery.interval_minutes is optional. Missing or null
means 1440 minutes, and a set value is an integer of at least 60. Discovery runs
inside the watch agent, and the interval is the least time between two passes.
discovery.queries_per_run (1 to 40, default 12) and discovery.query_delay_seconds (2
to 30, default 5) are optional; together the pauses of one pass are at most 120
seconds. Every problem is reported in one pass.

schedule.timezone is the only place a time zone is set. It takes an IANA name
(Europe/Berlin) or a fixed offset (UTC, UTC+05:30); a fixed offset doesn't
depend on the system's time zone database being current. If it's missing, dates use
UTC and doctor suggests setting it.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import zoneinfo
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeGuard

import yaml

from careerhunter.claims import validate_section
from careerhunter.enums import ATS_TYPES, FIT_LEVELS, WORK_MODELS

DEFAULT_WATCH_INTERVAL_MINUTES = 60
MIN_WATCH_INTERVAL_MINUTES = 15
DEFAULT_DISCOVERY_INTERVAL_MINUTES = 1440
MIN_DISCOVERY_INTERVAL_MINUTES = 60
# discovery.queries_per_run: how many queries one pass sends (#40).
DEFAULT_QUERIES_PER_RUN = 12
QUERIES_PER_RUN_LIMITS = (1, 40)
# discovery.query_delay_seconds: the pause between two queries of one pass.
DEFAULT_QUERY_DELAY_SECONDS = 5
QUERY_DELAY_LIMITS = (2, 30)
MAX_PASS_PAUSES = 120
OVER_LIMIT_ACTIONS = ("flag", "exclude")
DEFAULT_OFF_LANE_MIN_FIT = "high"
DEFAULT_MAX_POSTING_AGE_DAYS = 7
MAX_POSTING_AGE_LIMIT = 365
DEFAULT_MAX_PER_POLL = 10
MAX_PER_POLL_LIMIT = 50
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_LABELLED_EXAMPLES = 5
LABELLED_EXAMPLES_LIMIT = 20
DEFAULT_HEALTH_RUNS = 3
HEALTH_RUNS_LIMIT = 50
HEALTH_KEYS = ("unproductive_runs", "failure_runs")
# scoring.chat_model and scoring.embedding_model: the model identifiers sent to the
# local server. The URLs are loopback constants in score.py and embed.py, never config.
DEFAULT_CHAT_MODEL = "local-model"
DEFAULT_EMBEDDING_MODEL = "local-embedding"
MODEL_KEYS = ("chat_model", "embedding_model")
SCORING_KEYS = (*MODEL_KEYS, "timeout_seconds")
MAX_MODEL_ID = 200
# scoring.timeout_seconds: the most one scoring call may take. A reasoning model thinks
# before it answers, and the request does not stream. The upper bound leaves room in
# the 600-second watch run for the boards and one call.
DEFAULT_SCORE_TIMEOUT_SECONDS = 300
MIN_SCORE_TIMEOUT_SECONDS = 60
MAX_SCORE_TIMEOUT_SECONDS = 540
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CAREER_HOST = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


class ConfigError(Exception):
    """Raised with a list of every problem found, so the user can fix them in one pass."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{path} has {len(problems)} problem(s):\n{detail}")


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _is_text_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _is_title(value: Any) -> bool:
    """A lane title: a non-empty string, or a non-empty list of variant strings."""
    if isinstance(value, str):
        return bool(value.strip())
    return _is_text_list(value) and bool(value)


def _str_list(doc: dict[str, Any], key: str, where: str, problems: list[str]) -> None:
    if not _is_text_list(doc.get(key)):
        problems.append(f"{where}.{key} must be a list of non-empty strings")


def _optional_str_list(doc: dict[str, Any], key: str, where: str, problems: list[str]) -> None:
    if key in doc and doc[key] is not None:
        _str_list(doc, key, where, problems)


def _number(doc: dict[str, Any], key: str, where: str, problems: list[str]) -> None:
    value = doc.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
        problems.append(f"{where}.{key} must be a number")


def _resume_path(value: Any, problems: list[str]) -> None:
    """search.resume is null, or a relative path with no '..' segment."""
    if value is None:
        return
    message = "search.resume must be a relative path inside the data folder"
    if not isinstance(value, str) or not value.strip():
        problems.append(message)
        return
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts:
        problems.append(message)


def _embedding_weight(value: Any, problems: list[str]) -> None:
    """fit_rubric.embedding_weight is null, or a number from 0 to 1. Bools are refused."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        problems.append("fit_rubric.embedding_weight must be a number from 0 to 1")


def _searxng_url(value: Any, problems: list[str]) -> None:
    """discovery.searxng_url is null, or plain http to a loopback host."""
    if value is None:
        return
    message = "discovery.searxng_url must be an http loopback URL"
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        problems.append(message)
        return
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        problems.append(message)
        return
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or parts.hostname not in _LOOPBACK_HOSTS
        or parts.query
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        problems.append(message)


def _career_hosts(value: Any, problems: list[str]) -> None:
    """discovery.career_hosts is null or a list of hostnames. No scheme, path, or wildcard."""
    if value is None:
        return
    if not isinstance(value, list):
        problems.append("discovery.career_hosts must be a list of hostnames")
        return
    seen: set[str] = set()
    for i, host in enumerate(value):
        if not isinstance(host, str) or _CAREER_HOST.fullmatch(host) is None:
            problems.append(f"discovery.career_hosts[{i}] must be a hostname")
            continue
        key = host.casefold()
        if key in seen:
            problems.append("discovery.career_hosts names must be unique")
        seen.add(key)


def _validate_discovery(section: Any, problems: list[str]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append("discovery must be a mapping")
        return
    extra = set(section) - {
        "searxng_url",
        "career_hosts",
        "interval_minutes",
        "queries_per_run",
        "query_delay_seconds",
    }
    if extra:
        problems.append(f"discovery has unknown keys: {sorted(extra)}")
    if "searxng_url" in section:
        _searxng_url(section["searxng_url"], problems)
    if "career_hosts" in section:
        _career_hosts(section["career_hosts"], problems)
    if "interval_minutes" in section:
        _discovery_interval(section["interval_minutes"], problems)
    low, high = QUERIES_PER_RUN_LIMITS
    if not _int(value := section.get("queries_per_run", DEFAULT_QUERIES_PER_RUN)) or not (
        low <= value <= high
    ):
        problems.append(
            f"discovery.queries_per_run must be an integer from {low} to {high} "
            f"(default {DEFAULT_QUERIES_PER_RUN})"
        )
    low, high = QUERY_DELAY_LIMITS
    if not _int(value := section.get("query_delay_seconds", DEFAULT_QUERY_DELAY_SECONDS)) or not (
        low <= value <= high
    ):
        problems.append(
            f"discovery.query_delay_seconds must be an integer from {low} to {high} "
            f"(default {DEFAULT_QUERY_DELAY_SECONDS})"
        )
    elif discovery_queries_per_run({"discovery": section}) * value > MAX_PASS_PAUSES:
        # The pauses of one pass must leave time for the queries inside its share of
        # the watch run (discover.PASS_SECONDS).
        problems.append(
            "discovery.queries_per_run times discovery.query_delay_seconds must be at most "
            f"{MAX_PASS_PAUSES} seconds"
        )


def _discovery_interval(value: Any, problems: list[str]) -> None:
    """discovery.interval_minutes is null, or an integer of at least 60. Bools are refused."""
    if value is None:
        return
    if not _int(value) or value < MIN_DISCOVERY_INTERVAL_MINUTES:
        problems.append(
            f"discovery.interval_minutes must be an integer >= {MIN_DISCOVERY_INTERVAL_MINUTES}"
        )


def _int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _section(doc: dict[str, Any], key: str, problems: list[str]) -> dict[str, Any]:
    value = doc.get(key)
    if not isinstance(value, dict):
        problems.append(f"{key} must be a mapping")
        return {}
    return value


def _optional_mapping(doc: dict[str, Any], key: str, where: str, problems: list[str]) -> Any:
    value = doc.get(key)
    if value is not None and not isinstance(value, dict):
        problems.append(f"{where}.{key} must be a mapping")
        return None
    return value


def _work_models(value: Any, where: str, problems: list[str]) -> None:
    if not isinstance(value, list) or any(m not in WORK_MODELS for m in value):
        problems.append(f"{where} must only contain {list(WORK_MODELS)}")


def _validate_lanes(lanes: Any, problems: list[str]) -> None:
    if not isinstance(lanes, list) or not lanes:
        problems.append("search.lanes must be a non-empty list of lanes")
        return
    names, priorities = [], []
    for i, lane in enumerate(lanes):
        where = f"search.lanes[{i}]"
        if not isinstance(lane, dict):
            problems.append(f"{where} must be a mapping with name, priority and titles")
            continue
        extra = set(lane) - {"name", "priority", "fallback", "titles"}
        if extra:
            problems.append(f"{where} has unknown keys: {sorted(extra)}")
        name = lane.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}.name must be a non-empty string")
        else:
            names.append(name)
        priority = lane.get("priority")
        if not _int(priority) or priority < 1:
            problems.append(f"{where}.priority must be an integer >= 1")
        else:
            priorities.append(priority)
        if not isinstance(lane.get("fallback", False), bool):
            problems.append(f"{where}.fallback must be true or false")
        titles = lane.get("titles")
        # A title entry is a string, or a list of variants that count as one title.
        if not isinstance(titles, list) or not titles or not all(map(_is_title, titles)):
            problems.append(
                f"{where}.titles must be a non-empty list of titles (a title may be a list "
                "of variants)"
            )
    for label, values in (("names", names), ("priorities", priorities)):
        if len(values) != len(set(values)):
            problems.append(f"search.lanes {label} must be unique")


def _validate_location_rules(rules: Any, problems: list[str]) -> None:
    where = "search.location_rules"
    extra = set(rules) - {"onsite_hybrid", "remote", "allow_travel_outside"}
    if extra:
        problems.append(f"{where} has unknown keys: {sorted(extra)}")
    _optional_str_list(rules, "onsite_hybrid", where, problems)
    _optional_str_list(rules, "remote", where, problems)
    if not isinstance(rules.get("allow_travel_outside", True), bool):
        problems.append(f"{where}.allow_travel_outside must be true or false")


def _validate_gap_tiers(rubric: Any, problems: list[str]) -> None:
    where = "fit_rubric.gap_tiers"
    tiers = rubric.get("tiers")
    names: list[str] = []
    maxes: list[int] = []
    unmapped: list[str] = []
    if not isinstance(tiers, list) or not tiers:
        problems.append(f"{where}.tiers must be a non-empty list, best tier first")
    else:
        for i, tier in enumerate(tiers):
            if not isinstance(tier, dict) or not isinstance(tier.get("name"), str):
                problems.append(f"{where}.tiers[{i}] needs a name and max_gaps")
                continue
            names.append(tier["name"])
            if not _int(tier.get("max_gaps")) or tier["max_gaps"] < 0:
                problems.append(f"{where}.tiers[{i}].max_gaps must be an integer >= 0")
            else:
                maxes.append(tier["max_gaps"])
            if tier.get("fit") not in FIT_LEVELS:
                unmapped.append(tier["name"])
        if unmapped:
            problems.append(
                f"{where}.tiers need fit: {' | '.join(FIT_LEVELS)}; "
                f"missing or not one of those on {unmapped}"
            )
        if maxes != sorted(maxes) or len(set(maxes)) != len(maxes):
            problems.append(f"{where}.tiers must be ordered by increasing max_gaps")
        if len(set(names)) != len(names):
            problems.append(f"{where}.tiers names must be unique")
    exclude = rubric.get("exclude_from_gaps")
    if not _int(exclude) or (maxes and exclude <= max(maxes)):
        problems.append(f"{where}.exclude_from_gaps must be an integer above every max_gaps")
    boundary = _optional_mapping(rubric, "boundary", where, problems)
    if boundary:
        if not _int(boundary.get("at_gaps")):
            problems.append(f"{where}.boundary.at_gaps must be an integer")
        for key in ("all_minor", "any_core"):
            if boundary.get(key) not in names:
                problems.append(f"{where}.boundary.{key} must name one of the tiers {names}")
    _optional_str_list(rubric, "exempt_requirements", where, problems)


_HARD_GATES = frozenset(
    {
        "min_salary",
        "max_commute_km",
        "exclude_work_models",
        "required_languages",
        "credentials",
        "shift_patterns",
        "pay_structures",
        "seniority_words",
        "seniority_in_description",
        "title_exceptions",
        "never_gated_families",
    }
)


def _validate_seniority(gates: dict[str, Any], problems: list[str]) -> None:
    for key in (
        "required_languages",
        "credentials",
        "shift_patterns",
        "pay_structures",
        "seniority_words",
        "title_exceptions",
        "never_gated_families",
    ):
        _optional_str_list(gates, key, "hard_gates", problems)
    if gates.get("title_exceptions") and not gates.get("seniority_words"):
        problems.append("hard_gates.title_exceptions needs hard_gates.seniority_words")
    if not isinstance(gates.get("seniority_in_description", False), bool):
        problems.append("hard_gates.seniority_in_description must be true or false")


_WORKDAY_HOST = re.compile(r"wd[0-9]{1,3}")
_WORKDAY_SITE = re.compile(r"[A-Za-z0-9_-]+")


def _validate_watchlist_item(item: dict[str, Any], where: str, problems: list[str]) -> None:
    ats = item.get("ats_type")
    if ats is not None and ats not in ATS_TYPES:
        problems.append(f"{where}.ats_type must be one of {list(ATS_TYPES)}")
    _validate_workday_identity(item, where, problems)
    if not isinstance(item.get("agency", False), bool):
        problems.append(f"{where}.agency must be true or false")
    filters = _optional_mapping(item, "role_filters", where, problems)
    if filters:
        extra = set(filters) - {"role_families", "work_models", "locations"}
        if extra:
            problems.append(f"{where}.role_filters has unknown keys: {sorted(extra)}")
        _optional_str_list(filters, "role_families", f"{where}.role_filters", problems)
        _optional_str_list(filters, "locations", f"{where}.role_filters", problems)
        if "work_models" in filters:
            _work_models(filters["work_models"], f"{where}.role_filters.work_models", problems)


def _validate_workday_identity(item: dict[str, Any], where: str, problems: list[str]) -> None:
    """A Workday board is a tenant (ats_slug) on a wdN host with a site (#35).

    `ats_host` and `ats_site` belong to Workday entries only. A Workday entry without
    them is valid; the watcher logs and skips it.
    """
    for key in ("ats_host", "ats_site"):
        if key not in item:
            continue
        if item.get("ats_type") != "workday":
            problems.append(f"{where}.{key} is for ats_type workday only")
            continue
        value = item[key]
        pattern = _WORKDAY_HOST if key == "ats_host" else _WORKDAY_SITE
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            shape = "wd and up to three digits" if key == "ats_host" else "one path segment"
            problems.append(f"{where}.{key} must be {shape}")


def _validate_alerts(section: Any, problems: list[str]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append("alerts must be a mapping")
        return
    extra = set(section) - {"max_per_poll", "weekly_digest"}
    if extra:
        problems.append(f"alerts has unknown keys: {sorted(extra)}")
    value = section.get("max_per_poll")
    if value is not None and (not _int(value) or not 1 <= value <= MAX_PER_POLL_LIMIT):
        problems.append(
            f"alerts.max_per_poll must be an integer from 1 to {MAX_PER_POLL_LIMIT} "
            f"(default {DEFAULT_MAX_PER_POLL})"
        )
    weekly = section.get("weekly_digest")
    if weekly is None:
        return
    if not isinstance(weekly, dict) or set(weekly) != {"day", "hour"}:
        problems.append("alerts.weekly_digest must be a mapping with day and hour, or null")
        return
    if weekly["day"] not in WEEKDAYS:
        problems.append("alerts.weekly_digest.day must be a weekday such as monday")
    if not _int(weekly["hour"]) or not 0 <= weekly["hour"] <= 23:
        problems.append("alerts.weekly_digest.hour must be an integer from 0 to 23")


def _validate_learning(section: Any, problems: list[str]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append("learning must be a mapping")
        return
    extra = set(section) - {"labelled_examples"}
    if extra:
        problems.append(f"learning has unknown keys: {sorted(extra)}")
    value = section.get("labelled_examples")
    if value is not None and (not _int(value) or not 0 <= value <= LABELLED_EXAMPLES_LIMIT):
        problems.append(
            f"learning.labelled_examples must be an integer from 0 to {LABELLED_EXAMPLES_LIMIT} "
            f"(default {DEFAULT_LABELLED_EXAMPLES}; 0 sends none)"
        )


def _validate_health(section: Any, problems: list[str]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append("health must be a mapping")
        return
    extra = set(section) - set(HEALTH_KEYS)
    if extra:
        problems.append(f"health has unknown keys: {sorted(extra)}")
    for key in HEALTH_KEYS:
        value = section.get(key)
        if value is not None and (not _int(value) or not 1 <= value <= HEALTH_RUNS_LIMIT):
            problems.append(
                f"health.{key} must be an integer from 1 to {HEALTH_RUNS_LIMIT} "
                f"(default {DEFAULT_HEALTH_RUNS})"
            )


def _model_id(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_MODEL_ID
        and value == value.strip()
        and value.isprintable()
    )


def _validate_scoring(section: Any, problems: list[str]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append("scoring must be a mapping")
        return
    extra = set(section) - set(SCORING_KEYS)
    if extra:
        problems.append(f"scoring has unknown keys: {sorted(extra)}")
    for key in MODEL_KEYS:
        value = section.get(key)
        if value is not None and not _model_id(value):
            problems.append(
                f"scoring.{key} must be a model identifier: text of 1 to {MAX_MODEL_ID} "
                "characters, with no control characters and no leading or trailing spaces"
            )
    timeout = section.get("timeout_seconds")
    if timeout is not None and (
        not _int(timeout) or not MIN_SCORE_TIMEOUT_SECONDS <= timeout <= MAX_SCORE_TIMEOUT_SECONDS
    ):
        problems.append(
            f"scoring.timeout_seconds must be an integer from {MIN_SCORE_TIMEOUT_SECONDS} "
            f"to {MAX_SCORE_TIMEOUT_SECONDS}"
        )


def _validate_telegram(section: Any, problems: list[str]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append("telegram must be a mapping with chat_id")
        return
    extra = set(section) - {"chat_id"}
    if extra:
        problems.append(f"telegram has unknown keys: {sorted(extra)}")
    chat_id = section.get("chat_id")
    if chat_id is None:
        return  # not set yet; the bot refuses to start without it
    if not _int(chat_id) or chat_id <= 0:
        problems.append("telegram.chat_id must be your private chat's positive numeric id")


def validate_config(doc: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["top level must be a mapping"]

    search = _section(doc, "search", problems)
    if search:
        if "lanes" in search:
            _validate_lanes(search["lanes"], problems)
            _optional_str_list(search, "target_titles", "search", problems)
        else:
            _str_list(search, "target_titles", "search", problems)
        _str_list(search, "locations", "search", problems)
        _work_models(search.get("work_models", []), "search.work_models", problems)
        rules = _optional_mapping(search, "location_rules", "search", problems)
        if rules is not None:
            _validate_location_rules(rules, problems)
        if "resume" in search:
            _resume_path(search["resume"], problems)
        age = search.get("max_posting_age_days", DEFAULT_MAX_POSTING_AGE_DAYS)
        if not _int(age) or not 1 <= age <= MAX_POSTING_AGE_LIMIT:
            problems.append(
                f"search.max_posting_age_days must be an integer from 1 to "
                f"{MAX_POSTING_AGE_LIMIT} (default {DEFAULT_MAX_POSTING_AGE_DAYS})"
            )
        if search.get("off_lane_min_fit", DEFAULT_OFF_LANE_MIN_FIT) not in FIT_LEVELS:
            problems.append(
                f"search.off_lane_min_fit must be one of {' | '.join(FIT_LEVELS)} "
                f"(default {DEFAULT_OFF_LANE_MIN_FIT})"
            )

    gates = _section(doc, "hard_gates", problems)
    extra = set(gates) - _HARD_GATES
    if extra:
        problems.append(f"hard_gates has unknown keys: {sorted(extra)}")
    for key in ("min_salary", "max_commute_km"):
        _number(gates, key, "hard_gates", problems)
    _work_models(gates.get("exclude_work_models", []), "hard_gates.exclude_work_models", problems)
    _validate_seniority(gates, problems)

    rubric = _section(doc, "fit_rubric", problems)
    if "embedding_weight" in rubric:
        _embedding_weight(rubric["embedding_weight"], problems)
    gap_tiers = _optional_mapping(rubric, "gap_tiers", "fit_rubric", problems)
    if gap_tiers is not None:
        _validate_gap_tiers(gap_tiers, problems)
    weights = rubric.get("weights")
    if weights is None and gap_tiers is not None:
        pass  # the gap-count rubric replaces the weighted one
    elif not isinstance(weights, dict) or not weights:
        problems.append("fit_rubric.weights must be a non-empty mapping")
    else:
        for name, weight in weights.items():
            if isinstance(weight, bool) or not isinstance(weight, int | float) or weight < 0:
                problems.append(f"fit_rubric.weights.{name} must be a non-negative number")
    thresholds = rubric.get("thresholds")
    if thresholds is None and gap_tiers is not None:
        pass
    elif not isinstance(thresholds, dict):
        problems.append("fit_rubric.thresholds must be a mapping")
    else:
        for key, value in thresholds.items():
            if not _int(value) or not 0 <= value <= 10:
                problems.append(f"fit_rubric.thresholds.{key} must be an integer 0-10")

    schedule = _optional_mapping(doc, "schedule", "config", problems)
    if schedule:
        tz = schedule.get("timezone")
        if tz is not None:
            try:
                parse_timezone(tz)
            except ValueError as exc:
                problems.append(f"schedule.timezone: {exc}")
        interval = schedule.get("watcher_interval_minutes")
        if interval is not None and (not _int(interval) or interval < MIN_WATCH_INTERVAL_MINUTES):
            problems.append(
                "schedule.watcher_interval_minutes must be an integer >= "
                f"{MIN_WATCH_INTERVAL_MINUTES}"
            )

    limits = _optional_mapping(doc, "schedule_limits", "config", problems)
    if limits:
        days = limits.get("weekend_days_per_month")
        if days is not None and (not _int(days) or days < 0):
            problems.append("schedule_limits.weekend_days_per_month must be an integer >= 0")
        if limits.get("over_limit", "flag") not in OVER_LIMIT_ACTIONS:
            problems.append(f"schedule_limits.over_limit must be one of {list(OVER_LIMIT_ACTIONS)}")

    watchlist = doc.get("watchlist", [])
    if not isinstance(watchlist, list):
        problems.append("watchlist must be a list")
    else:
        for i, item in enumerate(watchlist):
            where = f"watchlist[{i}]"
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                problems.append(f"{where} needs a name")
                continue
            _validate_watchlist_item(item, where, problems)

    _validate_telegram(doc.get("telegram"), problems)
    _validate_alerts(doc.get("alerts"), problems)
    _validate_health(doc.get("health"), problems)
    _validate_learning(doc.get("learning"), problems)
    _validate_scoring(doc.get("scoring"), problems)
    _validate_discovery(doc.get("discovery"), problems)
    problems.extend(validate_section(doc.get("ei")))
    return problems


def validate_exclusions(doc: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["top level must be a mapping"]
    for key in ("companies", "title_keywords", "description_keywords"):
        if key in doc:
            _str_list(doc, key, "exclusions", problems)
    roles = doc.get("roles", [])
    if not isinstance(roles, list):
        problems.append("exclusions.roles must be a list")
    else:
        for i, role in enumerate(roles):
            where = f"exclusions.roles[{i}]"
            if not isinstance(role, dict):
                problems.append(f"{where} must be a mapping with company and title")
                continue
            extra = set(role) - {"company", "title", "location", "requisition_id"}
            if extra:
                problems.append(f"{where} has unknown keys: {sorted(extra)}")
            for key in ("company", "title"):
                if not isinstance(role.get(key), str) or not role[key].strip():
                    problems.append(f"{where}.{key} is required")
            for key in ("location", "requisition_id"):
                if key in role and (not isinstance(role[key], str) or not role[key].strip()):
                    problems.append(f"{where}.{key} must be non-empty text")
    if not isinstance(doc.get("exclude_applied", False), bool):
        problems.append("exclusions.exclude_applied must be true or false")
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


def telegram_chat_id(doc: dict[str, Any]) -> int | None:
    """The allowlisted chat id from an already validated config, if one is set."""
    section = doc.get("telegram") or {}
    value = section.get("chat_id")
    return int(value) if value is not None else None


def alerts_max_per_poll(doc: Mapping[str, Any] | None) -> int:
    """The per-poll cap on watcher alerts from an already validated config; 10 if unset."""
    section = doc.get("alerts") if isinstance(doc, Mapping) else None
    value = section.get("max_per_poll") if isinstance(section, Mapping) else None
    if _int(value) and 1 <= value <= MAX_PER_POLL_LIMIT:
        return value
    return DEFAULT_MAX_PER_POLL


def _health(doc: Mapping[str, Any] | None, key: str) -> int:
    section = doc.get("health") if isinstance(doc, Mapping) else None
    value = section.get(key) if isinstance(section, Mapping) else None
    if _int(value) and 1 <= value <= HEALTH_RUNS_LIMIT:
        return value
    return DEFAULT_HEALTH_RUNS


def weekly_digest(doc: Mapping[str, Any] | None) -> tuple[int, int] | None:
    """(weekday, hour) of the weekly digest from an already validated config, Monday 0,
    in schedule.timezone; None when it is off (#43)."""
    section = doc.get("alerts") if isinstance(doc, Mapping) else None
    weekly = section.get("weekly_digest") if isinstance(section, Mapping) else None
    if not isinstance(weekly, Mapping) or weekly.get("day") not in WEEKDAYS:
        return None
    hour = weekly.get("hour")
    if not _int(hour) or not 0 <= hour <= 23:
        return None
    return WEEKDAYS.index(weekly["day"]), hour


def learning_labelled_examples(doc: Mapping[str, Any] | None) -> int:
    """How many labelled examples go in the scoring prompt; 5 if unset, 0 for none (#43)."""
    section = doc.get("learning") if isinstance(doc, Mapping) else None
    value = section.get("labelled_examples") if isinstance(section, Mapping) else None
    if _int(value) and 0 <= value <= LABELLED_EXAMPLES_LIMIT:
        return value
    return DEFAULT_LABELLED_EXAMPLES


def _scoring_model(doc: Mapping[str, Any] | None, key: str, default: str) -> str:
    section = doc.get("scoring") if isinstance(doc, Mapping) else None
    value = section.get(key) if isinstance(section, Mapping) else None
    return value if _model_id(value) else default


def chat_model(doc: Mapping[str, Any] | None) -> str:
    """The chat model id the scorer sends; `local-model` if unset."""
    return _scoring_model(doc, "chat_model", DEFAULT_CHAT_MODEL)


def scoring_timeout_seconds(doc: Mapping[str, Any] | None) -> int:
    """The most one scoring call may take, in seconds; 300 if unset."""
    section = doc.get("scoring") if isinstance(doc, Mapping) else None
    value = section.get("timeout_seconds") if isinstance(section, Mapping) else None
    if _int(value) and MIN_SCORE_TIMEOUT_SECONDS <= value <= MAX_SCORE_TIMEOUT_SECONDS:
        return int(value)
    return DEFAULT_SCORE_TIMEOUT_SECONDS


def embedding_model(doc: Mapping[str, Any] | None) -> str:
    """The embedding model id the embedder sends; `local-embedding` if unset."""
    return _scoring_model(doc, "embedding_model", DEFAULT_EMBEDDING_MODEL)


def health_unproductive_runs(doc: Mapping[str, Any] | None) -> int:
    """Unproductive runs in a row before doctor warns; 3 if unset (#42)."""
    return _health(doc, "unproductive_runs")


def health_failure_runs(doc: Mapping[str, Any] | None) -> int:
    """Failed runs of one component in a row before the bot says so; 3 if unset (#42)."""
    return _health(doc, "failure_runs")


def discovery_queries_per_run(doc: Mapping[str, Any] | None) -> int:
    """How many queries one discovery pass sends. 12 when it is not set."""
    section = doc.get("discovery") if isinstance(doc, Mapping) else None
    value = section.get("queries_per_run") if isinstance(section, Mapping) else None
    low, high = QUERIES_PER_RUN_LIMITS
    return value if _int(value) and low <= value <= high else DEFAULT_QUERIES_PER_RUN


def discovery_query_delay_seconds(doc: Mapping[str, Any] | None) -> int:
    """Seconds between two discovery queries. 5 when it is not set."""
    section = doc.get("discovery") if isinstance(doc, Mapping) else None
    value = section.get("query_delay_seconds") if isinstance(section, Mapping) else None
    low, high = QUERY_DELAY_LIMITS
    return value if _int(value) and low <= value <= high else DEFAULT_QUERY_DELAY_SECONDS


def max_posting_age_days(doc: Mapping[str, Any] | None) -> int:
    """How many days old a posting may be (#36). 7 when it is not set."""
    search = doc.get("search") if isinstance(doc, Mapping) else None
    value = search.get("max_posting_age_days") if isinstance(search, Mapping) else None
    if _int(value) and 1 <= value <= MAX_POSTING_AGE_LIMIT:
        return value
    return DEFAULT_MAX_POSTING_AGE_DAYS


def off_lane_min_fit(doc: Mapping[str, Any] | None) -> str:
    """The lowest fit an off-lane posting needs to alert. `high` when it is not set."""
    search = doc.get("search") if isinstance(doc, Mapping) else None
    value = search.get("off_lane_min_fit") if isinstance(search, Mapping) else None
    return value if isinstance(value, str) and value in FIT_LEVELS else DEFAULT_OFF_LANE_MIN_FIT


def tier_fits(doc: Mapping[str, Any] | None) -> dict[str, str]:
    """Each gap tier's name and its fit, from an already validated config.

    A tier without a valid fit is left out, so a name missing here has no fit. With no
    gap tiers the mapping is empty.
    """
    if not isinstance(doc, Mapping):
        return {}
    rubric = doc.get("fit_rubric")
    gap_tiers = rubric.get("gap_tiers") if isinstance(rubric, dict) else None
    tiers = gap_tiers.get("tiers") if isinstance(gap_tiers, dict) else None
    if not isinstance(tiers, list):
        return {}
    return {
        tier["name"]: tier["fit"]
        for tier in tiers
        if isinstance(tier, dict)
        and isinstance(tier.get("name"), str)
        and tier.get("fit") in FIT_LEVELS
    }


_UTC_OFFSET = re.compile(r"UTC(?:([+-])(\d{1,2})(?::([0-5]\d))?)?")


def parse_timezone(value: Any) -> dt.tzinfo:
    """An IANA name (Europe/Berlin) or a fixed offset (UTC, UTC+05:30)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a time zone name like Europe/Berlin, or an offset like UTC+05:30")
    offset = _UTC_OFFSET.fullmatch(value.strip())
    if offset:
        sign, hours, minutes = offset.groups()
        if sign is None:
            return dt.UTC
        delta = dt.timedelta(hours=int(hours), minutes=int(minutes or 0))
        if delta > dt.timedelta(hours=14):
            raise ValueError(f"{value!r} is more than 14 hours from UTC")
        return dt.timezone(delta if sign == "+" else -delta, value.strip())
    try:
        return zoneinfo.ZoneInfo(value.strip())
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"{value!r} isn't a known time zone name or a UTC offset like UTC+05:30"
        ) from None


def discovery_enabled(doc: dict[str, Any] | None) -> bool:
    """True when discovery.searxng_url is set. Null or missing leaves discovery off."""
    if not isinstance(doc, dict):
        return False
    section = doc.get("discovery")
    if not isinstance(section, dict):
        return False
    url = section.get("searxng_url")
    return isinstance(url, str) and bool(url.strip())


def discovery_interval_minutes(doc: dict[str, Any] | None) -> int:
    """Minutes between discovery runs. A missing or unusable value uses the default."""
    if not isinstance(doc, dict):
        return DEFAULT_DISCOVERY_INTERVAL_MINUTES
    section = doc.get("discovery")
    if not isinstance(section, dict):
        return DEFAULT_DISCOVERY_INTERVAL_MINUTES
    value = section.get("interval_minutes", DEFAULT_DISCOVERY_INTERVAL_MINUTES)
    if value is None or not _int(value) or value < MIN_DISCOVERY_INTERVAL_MINUTES:
        return DEFAULT_DISCOVERY_INTERVAL_MINUTES
    return value


def watch_interval_minutes(doc: dict[str, Any] | None) -> int:
    """Minutes between watch runs. A missing or unusable value uses the default."""
    if not isinstance(doc, dict):
        return DEFAULT_WATCH_INTERVAL_MINUTES
    schedule = doc.get("schedule")
    if not isinstance(schedule, dict):
        return DEFAULT_WATCH_INTERVAL_MINUTES
    value = schedule.get("watcher_interval_minutes", DEFAULT_WATCH_INTERVAL_MINUTES)
    if not _int(value) or value < MIN_WATCH_INTERVAL_MINUTES:
        return DEFAULT_WATCH_INTERVAL_MINUTES
    return value


def timezone_name(doc: dict[str, Any]) -> str | None:
    """The configured schedule.timezone, or None if it isn't set (dates then use UTC)."""
    schedule = doc.get("schedule") or {}
    value = schedule.get("timezone")
    return str(value) if value is not None else None


def timezone(doc: dict[str, Any]) -> dt.tzinfo:
    """The one place a time zone comes from. Missing means UTC."""
    name = timezone_name(doc)
    return parse_timezone(name) if name is not None else dt.UTC


def today(doc: dict[str, Any] | None, now: dt.datetime | None = None) -> dt.date:
    """Today's date in the configured time zone (UTC with no config or no zone set)."""
    moment = now or dt.datetime.now(dt.UTC)
    return moment.astimezone(timezone(doc) if doc else dt.UTC).date()
