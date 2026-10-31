"""Import an initial history from $CAREERHUNTER_DATA_DIR/seed.yaml.

The format is documented in seed.example.yaml at the repo root. Each record goes to its
layer:

- companies and postings: search.db only;
- applications: both layers. The EI record of the active claim gets the sanitized copy
  and an `applied` status; search.db gets the full entry, notes included, and an
  `applied` decision. Both share a new correlation id;
- activities: the active claim's EI record only.

Every application and activity must belong to the active claim, by the rule for its date
precision (see claims.check_dating). An exact date outside the claim is refused, which
catches typos. `route_out_of_claim=True` instead sends such applications to search.db
only, and lists each one in the result. Activities outside the claim are always refused.
With no active claim, applications go to search.db only and activities are refused.

The import is idempotent: rows are matched on natural keys, so re-running it never
duplicates anything. A re-run also fills in a side that is missing: an application in the
EI record but not in search.db, or the other way round, is completed with the same
correlation id. Existing rows are otherwise left as they are; the seed never edits or
deletes.

The whole file is validated before anything is written, and the write follows the
ledger's order: search.db's rows are written but held uncommitted, the EI record commits,
then search.db commits.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from careerhunter import ei_store, enums, search_store
from careerhunter.claims import ActiveClaim, Verdict, check_dating
from careerhunter.ei_store import EiApplication
from careerhunter.ledger import ei_first, new_correlation_id, sanitize
from careerhunter.models import Activity, Application, Dating, Posting


class SeedError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"seed file has {len(problems)} problem(s):\n{detail}")


@dataclass
class Tally:
    inserted: dict[str, int] = field(default_factory=dict)
    existing: dict[str, int] = field(default_factory=dict)

    def bump(self, table: str, was_inserted: bool) -> None:
        bucket = self.inserted if was_inserted else self.existing
        bucket[table] = bucket.get(table, 0) + 1

    @property
    def total_inserted(self) -> int:
        return sum(self.inserted.values())


@dataclass
class SeedResult:
    search: Tally = field(default_factory=Tally)
    ei: Tally = field(default_factory=Tally)
    claim: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Applications recorded in search.db only, each with the reason.
    search_only: list[str] = field(default_factory=list)
    # Company and posting ids already counted this run, so each is reported once.
    companies_seen: set[int] = field(default_factory=set)
    postings_seen: set[int] = field(default_factory=set)

    @property
    def total_inserted(self) -> int:
        return self.search.total_inserted + self.ei.total_inserted


@dataclass(frozen=True)
class SeedRows:
    companies: list[dict[str, Any]]
    applications: list[Application]
    activities: list[Activity]


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _as_date(value: Any, where: str, problems: list[str]) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        problems.append(f"{where}: {value!r} is not a YYYY-MM-DD date")
        return None


def _enum(value: Any, allowed: tuple[str, ...], where: str, problems: list[str]) -> str | None:
    if value is None:
        return None
    norm = enums.normalize(str(value))
    if norm not in allowed:
        problems.append(f"{where}: {value!r} is not one of {list(allowed)}")
        return None
    return norm


def _text(item: dict[str, Any], key: str, where: str, problems: list[str]) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        problems.append(f"{where}.{key} must be text")
        return None
    text = str(value).strip()
    return text or None


def _required(item: dict[str, Any], key: str, where: str, problems: list[str]) -> str:
    value = _text(item, key, where, problems)
    if value is None:
        problems.append(f"{where}.{key} is required")
        return ""
    return value


def _dating(item: dict[str, Any], date_key: str, where: str, problems: list[str]) -> Dating:
    """Validate the precision trio and return it with ISO date strings."""
    precision = _enum(item.get("date_precision"), enums.DATE_PRECISIONS, where, problems)
    if precision is None:
        if item.get("date_precision") is None:
            problems.append(f"{where}.date_precision is required (exact, week, unknown)")
        return Dating("unknown")
    day = _as_date(item.get(date_key), f"{where}.{date_key}", problems)
    week = _as_date(item.get("week_of"), f"{where}.week_of", problems)
    if precision == "exact":
        if day is None:
            problems.append(f"{where}: date_precision exact needs {date_key}")
            return Dating("unknown")
        return Dating(precision, day=day.isoformat())
    if precision == "week":
        if week is None:
            problems.append(f"{where}: date_precision week needs week_of")
            return Dating("unknown")
        # "week of" means the week containing that day; store its Monday.
        return Dating(precision, week_of=monday_of(week).isoformat())
    if day is not None or week is not None:
        problems.append(f"{where}: date_precision unknown must not have {date_key} or week_of")
    return Dating(precision)


def _company_row(item: dict[str, Any], where: str, problems: list[str]) -> dict[str, Any]:
    watchlist = item.get("watchlist", False)
    if not isinstance(watchlist, bool):
        problems.append(f"{where}.watchlist must be true or false")
    return {
        "name": _required(item, "name", where, problems),
        "ats_type": _enum(item.get("ats_type"), enums.ATS_TYPES, f"{where}.ats_type", problems),
        "ats_slug": _text(item, "ats_slug", where, problems),
        "careers_url": _text(item, "careers_url", where, problems),
        "watchlist": int(watchlist is True),
    }


def parse(doc: Any, today: dt.date) -> SeedRows:
    """Validate a loaded seed document and return normalized rows, or raise SeedError."""
    problems: list[str] = []
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise SeedError(["top level must be a mapping with companies/applications/activities"])
    unknown = set(doc) - {"companies", "applications", "activities"}
    if unknown:
        problems.append(f"unknown top-level keys: {sorted(unknown)}")

    sections: dict[str, list[dict[str, Any]]] = {}
    for key in ("companies", "applications", "activities"):
        items = doc.get(key) or []
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            problems.append(f"{key} must be a list of mappings")
            items = []
        sections[key] = items

    companies = [
        _company_row(item, f"companies[{i}]", problems)
        for i, item in enumerate(sections["companies"])
    ]

    applications: list[Application] = []
    for i, item in enumerate(sections["applications"]):
        where = f"applications[{i}]"
        dating = _dating(item, "date_applied", where, problems)
        method = _enum(item.get("method"), enums.METHODS, f"{where}.method", problems)
        if item.get("method") is None:
            problems.append(f"{where}.method is required")
        posting = Posting(
            company=_required(item, "company", where, problems),
            title=_required(item, "title", where, problems),
            first_seen=dating.sort_date or today.isoformat(),
            location=_text(item, "location", where, problems) or "",
            work_model=_enum(
                item.get("work_model"), enums.WORK_MODELS, f"{where}.work_model", problems
            ),
            schedule=_text(item, "schedule", where, problems),
            url=_text(item, "url", where, problems),
            requisition_id=_text(item, "requisition_id", where, problems),
        )
        applications.append(
            Application(
                posting=posting,
                dating=dating,
                method=method or "other",
                resume_file=_text(item, "resume_file", where, problems),
                notes=_text(item, "notes", where, problems),
            )
        )

    activities: list[Activity] = []
    for i, item in enumerate(sections["activities"]):
        where = f"activities[{i}]"
        dating = _dating(item, "date", where, problems)
        kind = _enum(item.get("type"), enums.ACTIVITY_TYPES, f"{where}.type", problems)
        if item.get("type") is None:
            problems.append(f"{where}.type is required")
        activities.append(
            Activity(
                dating=dating,
                type=kind or "other",
                description=_required(item, "description", where, problems),
                related_company=_text(item, "related_company", where, problems),
            )
        )

    if problems:
        raise SeedError(problems)
    return SeedRows(companies, applications, activities)


def _describe(i: int, app: Application) -> str:
    return f"applications[{i}] ({app.posting.company} / {app.posting.title})"


def _route(
    rows: SeedRows, active: ActiveClaim | None, route_out_of_claim: bool, result: SeedResult
) -> list[bool]:
    """Apply the claim check. Returns, per application, whether it goes to the EI record."""
    problems: list[str] = []
    to_ei: list[bool] = []
    for i, app in enumerate(rows.applications):
        if active is None:
            to_ei.append(False)
            continue
        verdict, message = check_dating(active.claim, app.dating)
        if verdict is Verdict.REFUSE:
            if route_out_of_claim:
                result.search_only.append(f"{_describe(i, app)}: {message}")
            else:
                problems.append(
                    f"{_describe(i, app)}: {message}. Fix the date, or re-run with "
                    "--route-out-of-claim-to-search to record it in search.db only"
                )
            to_ei.append(False)
            continue
        if verdict is Verdict.WARN:
            result.warnings.append(f"{_describe(i, app)}: {message}")
        to_ei.append(True)
    for i, act in enumerate(rows.activities):
        where = f"activities[{i}] ({act.description})"
        if active is None:
            problems.append(
                f"{where}: no EI claim is active, so there is no record to put it in. Set "
                "ei.active_claim in config.yaml, or remove the activity"
            )
            continue
        verdict, message = check_dating(active.claim, act.dating)
        if verdict is Verdict.REFUSE:
            problems.append(f"{where}: {message}")
        elif verdict is Verdict.WARN:
            result.warnings.append(f"{where}: {message}")
    if problems:
        raise SeedError(problems)
    return to_ei


def _count_posting(
    result: SeedResult, company_id: int, posting_id: int, company_new: bool, posting_new: bool
) -> None:
    if company_id not in result.companies_seen:
        result.companies_seen.add(company_id)
        result.search.bump("companies", company_new)
    if posting_id not in result.postings_seen:
        result.postings_seen.add(posting_id)
        result.search.bump("postings", posting_new)


def import_rows(
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    rows: SeedRows,
    *,
    route_out_of_claim: bool = False,
) -> SeedResult:
    result = SeedResult(claim=active.claim.label if active else None)
    to_ei = _route(rows, active, route_out_of_claim, result)
    label = result.claim
    problems: list[str] = []
    # Sanitized copies for the EI record, by correlation id, in seed order.
    pending: dict[str, EiApplication] = {}
    pending_keys: dict[tuple[Any, ...], int] = {}

    with ei_first(search, active):
        for company in rows.companies:
            company_id, new = search_store.upsert_company(search, **company)
            if company_id not in result.companies_seen:
                result.companies_seen.add(company_id)
                result.search.bump("companies", new)

        for i, (app, goes_to_ei) in enumerate(zip(rows.applications, to_ei, strict=True)):
            try:
                company_id, posting_id, company_new, posting_new = (
                    search_store.find_or_create_posting(search, app.posting, app.dating)
                )
            except search_store.IdentityConflict as exc:
                problems.append(str(exc))
                continue
            _count_posting(result, company_id, posting_id, company_new, posting_new)

            found = search_store.find_application(search, posting_id, app.dating)
            in_ei = None
            if goes_to_ei and active is not None:
                in_ei = ei_store.find_application(
                    active.conn,
                    app.posting.company,
                    app.posting.title,
                    app.posting.location,
                    app.posting.url,
                    app.dating,
                )
                if found is not None and in_ei is not None and found["correlation_id"] != in_ei:
                    problems.append(
                        f"{_describe(i, app)}: the EI record and search.db hold it under "
                        "different correlation ids. Run `careerhunter doctor`"
                    )
                    continue
                if found is not None and found["ei_claim"] not in (None, label):
                    problems.append(
                        f"{_describe(i, app)}: search.db already links it to claim "
                        f"{found['ei_claim']}"
                    )
                    continue

            correlation_id = (
                found["correlation_id"] if found is not None else in_ei or new_correlation_id()
            )
            if found is None:
                app_id = search_store.insert_application(
                    search, posting_id, app, correlation_id, label if goes_to_ei else None
                )
                search_store.add_decision(
                    search, app_id, "applied", app.dating.sort_date, "imported from seed"
                )
                result.search.bump("applications", True)
                result.search.bump("decision_history", True)
            else:
                result.search.bump("applications", False)
                if goes_to_ei and found["ei_claim"] is None:
                    search_store.link_to_claim(search, int(found["id"]), str(label))

            if not goes_to_ei:
                continue
            if in_ei is not None or correlation_id in pending:
                result.ei.bump("applications", False)
                continue
            ei_app = sanitize(app, correlation_id)
            key = (
                ei_app.employer.casefold(),
                ei_app.role.casefold(),
                ei_app.location.casefold(),
                ei_app.posting_url or "",
                ei_app.date_precision,
                ei_app.date_applied,
                ei_app.week_of,
            )
            if key in pending_keys:
                problems.append(
                    f"{_describe(pending_keys[key], app)} and {_describe(i, app)} are different "
                    "postings, but the EI record can't tell them apart (same employer, role, "
                    "location, url and date). Add each posting's url"
                )
                continue
            pending_keys[key] = i
            pending[correlation_id] = ei_app

        if problems:
            raise SeedError(problems)

        if active is not None:
            for ei_app in pending.values():
                ei_id = ei_store.insert_application(active.conn, ei_app)
                ei_store.add_status(
                    active.conn, ei_id, "applied", ei_app.date_applied or ei_app.week_of
                )
                result.ei.bump("applications", True)
                result.ei.bump("status_history", True)
            for act in rows.activities:
                result.ei.bump("activities", ei_store.insert_activity(active.conn, act))
    return result


def import_seed(
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    path: Path,
    today: dt.date | None = None,
    *,
    route_out_of_claim: bool = False,
) -> SeedResult:
    if not path.is_file():
        raise SeedError([f"{path} not found. Copy seed.example.yaml there and fill it in."])
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    rows = parse(doc, today or dt.date.today())
    return import_rows(search, active, rows, route_out_of_claim=route_out_of_claim)
