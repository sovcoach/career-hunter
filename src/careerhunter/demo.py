"""`careerhunter demo`: the pipeline on synthetic data, with nothing real touched (#44).

The demo builds `search.db` and one EI record in memory (`sqlite3.connect(":memory:")`),
gives `watch.run` a synthetic config, two fake boards and a fake local model, prints
the watch lines and the digest the bot would send, then records one application
through the ledger and prints the EI row an export would hold.

It never reads `CAREERHUNTER_DATA_DIR` and never calls the storage guard: there is no
data folder to guard, because nothing is read from or written to disk. That is not an
override of the guard (ADR-009). Every client is a fake defined here, so no socket is
opened, loopback included, and nothing goes to Telegram.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from careerhunter import bot, claims, db, ledger, search_store, watch
from careerhunter.config import validate_config
from careerhunter.deadline import Deadline
from careerhunter.export import APPLICATION_COLUMNS, collect_applications
from careerhunter.fetch import Fetched
from careerhunter.filters import PostingText
from careerhunter.models import Application, Dating
from careerhunter.score import Score, score_posting

TODAY = dt.date(2026, 11, 2)
NOW = dt.datetime(2026, 11, 2, 14, 0, tzinfo=dt.UTC)
CLAIM = claims.Claim("2026-27", dt.date(2026, 8, 2))

CONFIG: dict[str, Any] = {
    "search": {
        "lanes": [
            {
                "name": "operations",
                "priority": 1,
                "titles": ["Operations Coordinator", ["Project Coordinator", "Project Assistant"]],
            },
            {
                "name": "customer_success",
                "priority": 2,
                "fallback": True,
                "titles": ["Customer Success Specialist"],
            },
        ],
        "locations": ["Example City"],
        "work_models": ["remote", "hybrid", "onsite"],
        "off_lane_min_fit": "high",
        "location_rules": {
            "onsite_hybrid": ["Example City"],
            "remote": ["Example Region"],
            "allow_travel_outside": False,
        },
        "resume": None,
    },
    "hard_gates": {
        "min_salary": None,
        "max_commute_km": 40,
        "exclude_work_models": [],
        "shift_patterns": ["overnight"],
        "seniority_words": ["Senior", "Director"],
    },
    "fit_rubric": {
        "gap_tiers": {
            "tiers": [
                {"name": "strong", "max_gaps": 1, "fit": "high"},
                {"name": "possible", "max_gaps": 2, "fit": "medium"},
            ],
            "exclude_from_gaps": 3,
        },
    },
    "schedule": {"timezone": "UTC"},
    "watchlist": [
        {"name": "Example Robotics", "ats_type": "greenhouse", "ats_slug": "examplerobotics"},
        {"name": "Sample Logistics", "ats_type": "lever", "ats_slug": "samplelogistics"},
        {"name": "Example Staffing", "agency": True},
    ],
    "ei": {
        "active_claim": CLAIM.label,
        "claims": [{"label": CLAIM.label, "start": CLAIM.start}],
    },
}


def _greenhouse_job(ident: int, title: str, content: str) -> dict[str, Any]:
    return {
        "id": ident,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/examplerobotics/jobs/{ident}",
        "location": {"name": "Example City"},
        "requisition_id": f"REQ-{ident}",
        "first_published": "2026-10-30T09:00:00+00:00",
        "content": content,
    }


def _lever_job(ident: str, title: str, location: str, work: str, text: str) -> dict[str, Any]:
    created = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.UTC)
    return {
        "id": ident,
        "text": title,
        "hostedUrl": f"https://jobs.lever.co/samplelogistics/{ident}",
        "createdAt": int(created.timestamp() * 1000),
        "categories": {"location": location},
        "workplaceType": work,
        "descriptionPlain": text,
    }


BOARDS: dict[str, Any] = {
    "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs?content=true": {
        "jobs": [
            _greenhouse_job(
                1001,
                "Operations Coordinator",
                "&lt;p&gt;Coordinate synthetic shipments. Needs inventory software and "
                "a forklift certificate.&lt;/p&gt;",
            ),
            _greenhouse_job(
                1002,
                "Senior Director, Operations",
                "&lt;p&gt;Lead a synthetic operations group.&lt;/p&gt;",
            ),
        ]
    },
    "https://api.lever.co/v0/postings/samplelogistics?mode=json": [
        _lever_job(
            "demo-201",
            "Project Assistant",
            "Remote - Example Region",
            "remote",
            "Support synthetic projects. Needs a scheduling tool and status reports.",
        ),
        _lever_job(
            "demo-202",
            "Night Dispatcher",
            "Example City",
            "onsite",
            "Dispatch synthetic trucks on overnight shifts.",
        ),
        _lever_job(
            "demo-203",
            "Customer Success Specialist",
            "Remote - Example Region",
            "remote",
            "Help synthetic customers. Needs three tools the candidate has not used.",
        ),
        _lever_job(
            "demo-204",
            "Data Analyst",
            "Example City",
            "onsite",
            "Report on synthetic shipments. Needs spreadsheets.",
        ),
    ],
}

# What the fake local model answers for each title: the gaps, then key requirements.
ANSWERS: dict[str, tuple[list[dict[str, str]], list[str]]] = {
    "Operations Coordinator": (
        [{"name": "forklift certificate", "kind": "minor", "type": "credential"}],
        ["inventory software", "forklift certificate"],
    ),
    "Project Assistant": (
        [
            {"name": "scheduling tool", "kind": "minor", "type": "tool"},
            {"name": "status reports", "kind": "core", "type": "skill"},
        ],
        ["scheduling tool", "status reports"],
    ),
    "Customer Success Specialist": (
        [
            {"name": "first tool", "kind": "core", "type": "tool"},
            {"name": "second tool", "kind": "core", "type": "tool"},
            {"name": "third tool", "kind": "core", "type": "tool"},
        ],
        [],
    ),
    "Data Analyst": ([], ["spreadsheets"]),
}


def board_fetcher(address: str, **_kw: object) -> Fetched:
    """The fake board API: the canned payload for a known URL, else 404."""
    payload = BOARDS.get(address)
    if payload is None:
        return Fetched(address, 404, "application/json", b"{}")
    return Fetched(address, 200, "application/json", json.dumps(payload).encode())


def model_transport(_url: str, payload: bytes, **_kw: object) -> tuple[int, bytes]:
    """The fake local model: canned gaps for the posting's title."""
    request = json.loads(payload)
    view = json.loads(request["messages"][-1]["content"])
    gaps, requirements = ANSWERS.get(view["posting"]["title"], ([], []))
    content = json.dumps({"gaps": gaps, "requirements": requirements})
    return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def memory_db(track: str) -> sqlite3.Connection:
    """An in-memory database, migrated like a real file. Nothing touches the disk."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if track == "search":
        db.migrate(conn, "search")
    return conn


def _scorer(doc: Mapping[str, Any]) -> Callable[[PostingText], Score | None]:
    def scorer(posting: PostingText) -> Score | None:
        return score_posting(posting, doc, transport=model_transport)

    return scorer


def _apply(search: sqlite3.Connection, ei: sqlite3.Connection) -> list[str]:
    row = search.execute(
        "SELECT id FROM postings WHERE url = ?",
        ("https://boards.greenhouse.io/examplerobotics/jobs/1001",),
    ).fetchone()
    posting = search_store.posting_by_id(search, int(row["id"]))
    if posting is None:  # the watch run above stored it; anything else is a demo bug
        raise RuntimeError("the demo posting was not stored")
    active = claims.ActiveClaim(conn=ei, claim=CLAIM, path=Path(":memory:"))
    dating = Dating("exact", day=TODAY.isoformat())
    ledger.record_application(search, active, Application(posting, dating, "company_site"))
    lines = [f"Applied: {posting.company} — {posting.title}, {TODAY}, company site"]
    lines.append(f"EI record for claim {CLAIM.label}, as an export would list it:")
    for record in collect_applications(ei, CLAIM.start, TODAY):
        for column in APPLICATION_COLUMNS:
            lines.append(f"  {column}: {record[column]}".rstrip())
    return lines


def run(emit: Callable[[str], None] = print) -> int:
    """Run the demo, printing each line with `emit`. Returns the exit code."""
    problems = validate_config(CONFIG)
    if problems:  # the synthetic config must stay valid; a test holds it there
        raise AssertionError(problems)
    emit("careerhunter demo: synthetic data, in-memory databases, fake boards and model.")
    emit("Nothing is read from or written to disk, and nothing leaves this process.")
    emit("")
    emit("== watch run ==")
    search = memory_db("search")
    ei = memory_db("ei_record")
    try:
        claims.init_record(ei, CLAIM)
        lines = watch.run(
            search,
            CONFIG["watchlist"],
            today=TODAY.isoformat(),
            fetcher=board_fetcher,
            config=CONFIG,
            exclusions={},
            scorer=_scorer(CONFIG),
            data_dir=None,
            budget=Deadline(watch.RUN_BUDGET),
            now=lambda: NOW,
        )
        for line in lines:
            emit(line)
        emit("")
        emit("== the Telegram digest (printed here, not sent) ==")
        for text in bot.digest_texts(search, CONFIG):
            emit(text)
        emit("")
        emit("== Applied ==")
        for line in _apply(search, ei):
            emit(line)
    finally:
        search.close()
        ei.close()
    return 0
