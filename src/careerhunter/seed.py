"""Import an initial history from $CAREERHUNTER_DATA_DIR/seed.yaml.

The format is documented in seed.example.yaml at the repo root. The import is
idempotent: rows are matched on natural keys, so re-running it never duplicates
anything. Those keys are company name; for a posting, its url, else company +
requisition id, else company + title + location + month first seen; an application
is one posting on one date; an activity is type + description + date. Existing rows
are left as they are; the seed never edits or deletes.

The whole file is validated before anything is written, and the write happens in one
transaction: either everything new goes in, or nothing does.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from careerhunter import enums
from careerhunter.db import transaction


class SeedError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"seed file has {len(problems)} problem(s):\n{detail}")


@dataclass
class SeedResult:
    inserted: dict[str, int] = field(default_factory=dict)
    existing: dict[str, int] = field(default_factory=dict)
    # Company ids already counted this run, so a company named by several applications
    # is reported once.
    companies_seen: set[int] = field(default_factory=set)
    postings_seen: set[int] = field(default_factory=set)

    def bump(self, table: str, was_inserted: bool) -> None:
        bucket = self.inserted if was_inserted else self.existing
        bucket[table] = bucket.get(table, 0) + 1

    @property
    def total_inserted(self) -> int:
        return sum(self.inserted.values())


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


def _dating(
    item: dict[str, Any], date_key: str, where: str, problems: list[str]
) -> tuple[str, str | None, str | None]:
    """Validate the precision trio. Returns (precision, date, week_of) as ISO strings."""
    precision = _enum(item.get("date_precision"), enums.DATE_PRECISIONS, where, problems)
    if precision is None:
        if item.get("date_precision") is None:
            problems.append(f"{where}.date_precision is required (exact, week, unknown)")
        return "unknown", None, None
    day = _as_date(item.get(date_key), f"{where}.{date_key}", problems)
    week = _as_date(item.get("week_of"), f"{where}.week_of", problems)
    if precision == "exact":
        if day is None:
            problems.append(f"{where}: date_precision exact needs {date_key}")
            return precision, None, None
        return precision, day.isoformat(), None
    if precision == "week":
        if week is None:
            problems.append(f"{where}: date_precision week needs week_of")
            return precision, None, None
        # "week of" means the week containing that day; store its Monday.
        return precision, None, monday_of(week).isoformat()
    if day is not None or week is not None:
        problems.append(f"{where}: date_precision unknown must not have {date_key} or week_of")
    return precision, None, None


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


def parse(doc: Any, today: dt.date) -> dict[str, list[dict[str, Any]]]:
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

    applications: list[dict[str, Any]] = []
    for i, item in enumerate(sections["applications"]):
        where = f"applications[{i}]"
        precision, applied, week = _dating(item, "date_applied", where, problems)
        method = _enum(item.get("method"), enums.METHODS, f"{where}.method", problems)
        if item.get("method") is None:
            problems.append(f"{where}.method is required")
        applications.append(
            {
                "company": _required(item, "company", where, problems),
                "title": _required(item, "title", where, problems),
                "location": _text(item, "location", where, problems) or "",
                "work_model": _enum(
                    item.get("work_model"), enums.WORK_MODELS, f"{where}.work_model", problems
                ),
                "schedule": _text(item, "schedule", where, problems),
                "url": _text(item, "url", where, problems),
                "requisition_id": _text(item, "requisition_id", where, problems),
                "method": method,
                "date_precision": precision,
                "date_applied": applied,
                "week_of": week,
                "resume_file": _text(item, "resume_file", where, problems),
                "notes": _text(item, "notes", where, problems),
                "first_seen": applied or week or today.isoformat(),
            }
        )

    activities: list[dict[str, Any]] = []
    for i, item in enumerate(sections["activities"]):
        where = f"activities[{i}]"
        precision, day, week = _dating(item, "date", where, problems)
        kind = _enum(item.get("type"), enums.ACTIVITY_TYPES, f"{where}.type", problems)
        if item.get("type") is None:
            problems.append(f"{where}.type is required")
        activities.append(
            {
                "date": day,
                "date_precision": precision,
                "week_of": week,
                "type": kind,
                "description": _required(item, "description", where, problems),
                "related_company": _text(item, "related_company", where, problems),
            }
        )

    if problems:
        raise SeedError(problems)
    return {"companies": companies, "applications": applications, "activities": activities}


def _upsert_company(conn: sqlite3.Connection, row: dict[str, Any], result: SeedResult) -> int:
    cur = conn.execute(
        "INSERT INTO companies (name, ats_type, ats_slug, careers_url, watchlist, source) "
        "VALUES (:name, :ats_type, :ats_slug, :careers_url, :watchlist, 'manual') "
        "ON CONFLICT DO NOTHING",
        row,
    )
    found = conn.execute("SELECT id FROM companies WHERE name = ?", (row["name"],)).fetchone()
    company_id = int(found["id"])
    if company_id not in result.companies_seen:
        result.companies_seen.add(company_id)
        result.bump("companies", cur.rowcount == 1)
    return company_id


def _find_posting(conn: sqlite3.Connection, company_id: int, app: dict[str, Any]) -> int | None:
    """Match an existing posting using the schema's identity rules, strongest first."""
    if app["url"]:
        row = conn.execute(
            "SELECT id, company_id FROM postings WHERE url = ?", (app["url"],)
        ).fetchone()
        if row:
            if row["company_id"] != company_id:
                raise SeedError(
                    [
                        f"{app['company']} / {app['title']}: url {app['url']!r} is already "
                        "recorded for a posting at a different company"
                    ]
                )
            return int(row["id"])
    if app["requisition_id"]:
        row = conn.execute(
            "SELECT id FROM postings WHERE company_id = ? AND requisition_id = ?",
            (company_id, app["requisition_id"]),
        ).fetchone()
        if row:
            return int(row["id"])
    if app["url"] or app["requisition_id"]:
        return None
    fallback = (
        "SELECT p.id FROM postings p {join} WHERE p.company_id = ? AND p.title = ? "
        "AND p.location = ? AND p.url IS NULL AND p.requisition_id IS NULL {extra} LIMIT 1"
    )
    key = (company_id, app["title"], app["location"])
    row = conn.execute(
        fallback.format(join="", extra="AND substr(p.first_seen, 1, 7) = substr(?, 1, 7)"),
        (*key, app["first_seen"]),
    ).fetchone()
    if row is None and app["date_precision"] == "unknown":
        # An undated entry's first_seen is the import date, which moves between runs.
        # Match the earlier import of the same undated application instead.
        row = conn.execute(
            fallback.format(
                join="JOIN applications a ON a.posting_id = p.id",
                extra="AND a.date_precision = 'unknown'",
            ),
            key,
        ).fetchone()
    return int(row["id"]) if row else None


def _find_or_create_posting(
    conn: sqlite3.Connection, company_id: int, app: dict[str, Any], result: SeedResult
) -> int:
    posting_id = _find_posting(conn, company_id, app)
    inserted = posting_id is None
    if posting_id is None:
        cur = conn.execute(
            "INSERT INTO postings (company_id, title, location, work_model, schedule, url, "
            "requisition_id, first_seen, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')",
            (
                company_id,
                app["title"],
                app["location"],
                app["work_model"],
                app["schedule"],
                app["url"],
                app["requisition_id"],
                app["first_seen"],
            ),
        )
        posting_id = int(cur.lastrowid or 0)
    if posting_id not in result.postings_seen:
        result.postings_seen.add(posting_id)
        result.bump("postings", inserted)
    return posting_id


def import_rows(conn: sqlite3.Connection, rows: dict[str, list[dict[str, Any]]]) -> SeedResult:
    result = SeedResult()
    with transaction(conn):
        for company in rows["companies"]:
            _upsert_company(conn, company, result)

        for app in rows["applications"]:
            company_id = _upsert_company(
                conn,
                {
                    "name": app["company"],
                    "ats_type": None,
                    "ats_slug": None,
                    "careers_url": None,
                    "watchlist": 0,
                },
                result,
            )
            posting_id = _find_or_create_posting(conn, company_id, app, result)

            cur = conn.execute(
                "INSERT INTO applications (posting_id, date_applied, date_precision, week_of, "
                "method, resume_file, notes) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (
                    posting_id,
                    app["date_applied"],
                    app["date_precision"],
                    app["week_of"],
                    app["method"],
                    app["resume_file"],
                    app["notes"],
                ),
            )
            inserted = cur.rowcount == 1
            result.bump("applications", inserted)
            if inserted:
                conn.execute(
                    "INSERT INTO status_history (application_id, status, status_date, note) "
                    "VALUES (?, 'applied', ?, 'imported from seed')",
                    (cur.lastrowid, app["date_applied"] or app["week_of"]),
                )
                result.bump("status_history", True)

        for act in rows["activities"]:
            cur = conn.execute(
                "INSERT INTO activities (date, date_precision, week_of, type, description, "
                "related_company) VALUES (:date, :date_precision, :week_of, :type, "
                ":description, :related_company) ON CONFLICT DO NOTHING",
                act,
            )
            result.bump("activities", cur.rowcount == 1)
    return result


def import_seed(conn: sqlite3.Connection, path: Path, today: dt.date | None = None) -> SeedResult:
    if not path.is_file():
        raise SeedError([f"{path} not found. Copy seed.example.yaml there and fill it in."])
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return import_rows(conn, parse(doc, today or dt.date.today()))
