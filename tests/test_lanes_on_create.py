"""A posting created by an application (import, /log) is checked against the lanes at
once, as the watcher checks its own. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

from careerhunter import backfill, ledger
from careerhunter.claims import ActiveClaim
from careerhunter.models import Application, Dating, Posting
from careerhunter.storage_guard import DataDir
from tests.test_backfill import entry, imports, planned, write  # noqa: F401 (fixture)
from tests.test_lanes_reassign import config

TODAY = dt.date(2026, 11, 10)


def _lane(search: sqlite3.Connection, title: str) -> tuple[Any, ...]:
    row = search.execute("SELECT lane, lane_checked FROM postings WHERE title = ?", (title,))
    return tuple(row.fetchone())


def _app(title: str) -> Application:
    day = "2026-09-15"
    posting = Posting(company="Example Robotics", title=title, first_seen=day, url=None)
    return Application(posting, Dating("exact", day=day), "company_site")


def test_a_recorded_application_checks_its_new_posting(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    ledger.record_application(
        search, active, _app("Client Experience Analyst"), lane_config=config()
    )
    ledger.record_application(search, active, _app("Synthetic Juggler"), lane_config=config())
    assert _lane(search, "Client Experience Analyst") == ("client_experience", 1)
    assert _lane(search, "Synthetic Juggler") == (None, 1)  # off-lane, and checked


def test_without_lanes_or_config_nothing_is_checked(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    ledger.record_application(search, active, _app("Client Experience Analyst"))
    ledger.record_application(search, active, _app("Operations Coordinator"), lane_config={})
    assert _lane(search, "Client Experience Analyst") == (None, 0)
    assert _lane(search, "Operations Coordinator") == (None, 0)


def test_an_already_checked_posting_keeps_its_lane(
    search: sqlite3.Connection, active: ActiveClaim
) -> None:
    ledger.record_application(search, active, _app("Operations Coordinator"), lane_config=config())
    search.execute("UPDATE postings SET lane = 'it_support'")  # as a reassign might leave it
    ledger.record_application(
        search,
        active,
        Application(
            _app("Operations Coordinator").posting, Dating("exact", day="2026-09-16"), "email"
        ),
        lane_config=config(),
    )
    assert _lane(search, "Operations Coordinator") == ("it_support", 1)


def test_the_import_checks_a_created_posting(
    datadir: DataDir,
    imports: Path,  # noqa: F811
    search: sqlite3.Connection,
    active: ActiveClaim,
) -> None:
    path = write(imports, entry(employer="Synthetic Widgets", role="IT Support Specialist"))
    plan = planned(datadir, search, active, path)
    backfill.apply(datadir.root, search, active, plan, TODAY, lane_config=config())
    assert _lane(search, "IT Support Specialist") == ("it_support", 1)
