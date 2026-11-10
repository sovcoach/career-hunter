"""The posting age gate (#36): a fresh, a stale and an undated posting on every ATS.

Synthetic boards only; no sockets. The run date is DAY (2026-11-01) and the default
limit is seven days, so 2026-10-30 is fresh and 2026-10-01 is stale.
"""

from __future__ import annotations

import copy
import datetime as dt
import sqlite3
from typing import Any

import pytest

from careerhunter import ats, filters
from careerhunter import watch as w
from careerhunter.config import max_posting_age_days, validate_config
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url
from tests.test_watch_ats import (
    BH,
    BH_LIST,
    SR,
    WD,
    Posted,
    bh_detail,
    bh_job,
    bh_list,
    bh_opening,
    routes,
    sr_ad,
    sr_detail,
    sr_list,
    sr_page,
    sr_posting,
    wd_info,
    wd_page,
    wd_path,
    wd_row,
)

WHEN = ("fresh", "stale", "none")
_MS = {
    "fresh": int(dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC).timestamp() * 1000),
    "stale": int(dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC).timestamp() * 1000),
}
_ISO = {"fresh": "2026-10-30T16:00:00.000+00:00", "stale": "2026-10-01T16:00:00.000+00:00"}
_WD_BASE = "https://examplerobotics.wd5.myworkdayjobs.com/wday/cxs/examplerobotics/External"


def _board(ats_type: str, when: str) -> tuple[dict[str, object], Scripted]:
    """A one-posting board of `ats_type` whose posted date is fresh, stale or missing."""
    if ats_type == "greenhouse":
        payload = one_greenhouse("examplerobotics")
        job = payload["jobs"][0]  # type: ignore[index]
        job.pop("first_published")
        if when != "none":
            job["first_published"] = {
                "fresh": "2026-10-30T09:00:00-04:00",
                "stale": "2026-10-01T09:00:00-04:00",
            }[when]
        item = company("Example Robotics", "greenhouse", "examplerobotics")
        return item, Scripted({url("greenhouse", "examplerobotics"): fetched(payload)})
    if ats_type == "lever":
        lever: dict[str, object] = {
            "id": "abc-123",
            "text": "Operations Coordinator",
            "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
            "categories": {"location": "Example City"},
        }
        if when != "none":
            lever["createdAt"] = _MS[when]
        item = company("Sample Logistics", "lever", "samplelogistics")
        return item, Scripted({url("lever", "samplelogistics"): fetched([lever])})
    if ats_type == "ashby":
        ashby: dict[str, object] = {
            "id": "uuid-1",
            "title": "Operations Coordinator",
            "jobUrl": "https://jobs.ashbyhq.com/exampleorg/uuid-1",
            "location": "Example City",
        }
        if when != "none":
            ashby["publishedAt"] = _ISO[when]
        item = company("Example Org", "ashby", "exampleorg")
        return item, Scripted({url("ashby", "exampleorg"): fetched({"jobs": [ashby]})})
    if ats_type == "smartrecruiters":
        ident = "744000000000001"
        row = sr_posting(ident)
        row.pop("releasedDate")
        if when != "none":
            row["releasedDate"] = {
                "fresh": "2026-10-30T10:00:00.000Z",
                "stale": "2026-10-01T10:00:00.000Z",
            }[when]
        board = {sr_list(0): sr_page([row], total=1), sr_detail(ident): sr_ad(ident, "Team.")}
        return SR, Scripted(routes(board))
    if ats_type == "bamboohr":
        opening = bh_opening("A friendly team.")
        detail = opening["result"]["jobOpening"]  # type: ignore[index]
        detail["datePosted"] = {"fresh": "2026-10-30", "stale": "2026-10-01", "none": None}[when]
        return BH, Scripted(routes({BH_LIST: bh_list([bh_job("41")]), bh_detail("41"): opening}))
    posted = {"fresh": "Posted 2 Days Ago", "stale": "Posted 30+ Days Ago", "none": "Recently"}
    return WD, Posted(
        routes({f"{_WD_BASE}{wd_path(1)}": wd_info("A friendly team.")}),
        {0: fetched(wd_page([wd_row(1, posted=posted[when])], total=1))},
    )


def _run(
    conn: sqlite3.Connection, item: dict[str, object], fetcher: Scripted, **kw: Any
) -> list[str]:
    poster = fetcher.post if isinstance(fetcher, Posted) else None
    return w.run(conn, [item], today=DAY, fetcher=fetcher, poster=poster, **kw)


def _state(conn: sqlite3.Connection) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT p.posted_date, (SELECT reason FROM skips s WHERE s.posting_id = p.id) AS skip "
        "FROM postings p"
    ).fetchone()
    return tuple(row)


ATS = ("greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr", "workday")
EXPECTED_DATE = {
    "greenhouse": "2026-10-30",
    "lever": "2026-10-30",
    "ashby": "2026-10-30",
    "smartrecruiters": "2026-10-30",
    "bamboohr": "2026-10-30",
    "workday": "2026-10-30",  # two days before the run date
}


@pytest.mark.parametrize("ats_type", ATS)
def test_a_fresh_posting_is_kept_with_its_posted_date(
    search: sqlite3.Connection, ats_type: str
) -> None:
    item, fetcher = _board(ats_type, "fresh")
    lines = _run(search, item, fetcher)
    assert _state(search) == (EXPECTED_DATE[ats_type], None)
    assert not any("no posted date" in line for line in lines)


@pytest.mark.parametrize("ats_type", ATS)
def test_a_stale_posting_is_skipped_too_old(search: sqlite3.Connection, ats_type: str) -> None:
    item, fetcher = _board(ats_type, "stale")
    lines = _run(search, item, fetcher)
    stored = None if ats_type == "workday" else "2026-10-01"  # Workday gives "30+" only
    assert _state(search) == (stored, "too_old")
    assert any(line.endswith("skipped (too_old)") for line in lines)


@pytest.mark.parametrize("ats_type", ATS)
def test_an_undated_posting_uses_first_seen_with_a_line(
    search: sqlite3.Connection, ats_type: str
) -> None:
    item, fetcher = _board(ats_type, "none")
    lines = _run(search, item, fetcher)
    assert _state(search) == (None, None)
    assert any(
        line.endswith("'Operations Coordinator': no posted date; first_seen used") for line in lines
    )


@pytest.mark.parametrize("ats_type", ("smartrecruiters", "workday"))
def test_a_posting_the_list_shows_as_stale_gets_no_detail_request(
    search: sqlite3.Connection, ats_type: str
) -> None:
    item, fetcher = _board(ats_type, "stale")
    _run(search, item, fetcher)
    assert fetcher.calls == ([sr_list(0)] if ats_type == "smartrecruiters" else [])


def test_the_limit_comes_from_config(search: sqlite3.Connection) -> None:
    item, fetcher = _board("lever", "stale")
    _run(search, item, fetcher, config={"search": {"max_posting_age_days": 60}})
    assert _state(search) == ("2026-10-01", None)


def test_workday_30_plus_days_under_a_longer_limit_falls_back_to_first_seen(
    search: sqlite3.Connection,
) -> None:
    item, fetcher = _board("workday", "stale")
    lines = _run(search, item, fetcher, config={"search": {"max_posting_age_days": 45}})
    assert _state(search) == (None, None)
    assert any(line.endswith("no posted date; first_seen used") for line in lines)


def test_the_recheck_skips_a_stored_posting_that_grew_too_old(
    search: sqlite3.Connection,
) -> None:
    item, fetcher = _board("lever", "fresh")
    _run(search, item, fetcher)
    later = (dt.date.fromisoformat(DAY) + dt.timedelta(days=10)).isoformat()
    lines = w.run(search, [item], today=later, fetcher=Scripted({}))
    assert _state(search) == ("2026-10-30", "too_old")
    assert "watch: Sample Logistics: 'Operations Coordinator' skipped (too_old)" in lines


def test_the_recheck_uses_first_seen_when_no_date_was_stored(search: sqlite3.Connection) -> None:
    item, fetcher = _board("lever", "none")
    _run(search, item, fetcher)
    _run(search, item, copy.deepcopy(fetcher))  # same day: still kept
    assert _state(search) == (None, None)
    later = (dt.date.fromisoformat(DAY) + dt.timedelta(days=8)).isoformat()
    w.run(search, [item], today=later, fetcher=Scripted({}))
    assert _state(search) == (None, "too_old")


def test_another_gate_keeps_its_own_reason(search: sqlite3.Connection) -> None:
    item, fetcher = _board("lever", "stale")
    _run(search, item, fetcher, config={"hard_gates": {"seniority_words": ["Coordinator"]}})
    assert _state(search) == ("2026-10-01", "seniority")


@pytest.mark.parametrize(
    ("posted", "at_least", "expected"),
    [
        (dt.date(2026, 10, 25), None, False),  # exactly seven days: kept
        (dt.date(2026, 10, 24), None, True),
        (dt.date(2026, 11, 5), None, False),  # in the future: not too old
        (None, 30, True),
        (None, 5, None),  # a lower bound under the limit decides nothing
        (None, None, None),
    ],
)
def test_too_old(posted: dt.date | None, at_least: int | None, expected: bool | None) -> None:
    assert filters.too_old(posted, at_least, dt.date(2026, 11, 1), 7) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-30T23:30:00-04:00", dt.date(2026, 10, 31)),  # read in UTC
        ("2026-10-30T10:00:00.000Z", dt.date(2026, 10, 30)),
        ("2026-10-30T10:00:00", None),  # no offset
        ("not a date", None),
        (None, None),
    ],
)
def test_iso_posted_dates_are_read_in_utc(value: object, expected: dt.date | None) -> None:
    assert ats._utc_date(value) == expected


@pytest.mark.parametrize("value", [True, -1, "1793361600000", 10**20, None])
def test_bad_epoch_dates_give_no_date(value: object) -> None:
    assert ats._epoch_ms_date(value) is None


@pytest.mark.parametrize("value", ["2026-10-30T00:00:00", "2026-13-01", "", 20261030])
def test_bad_plain_dates_give_no_date(value: object) -> None:
    assert ats._plain_date(value) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Posted Today", (dt.date(2026, 11, 1), None)),
        ("Posted 3 Days Ago", (dt.date(2026, 10, 29), None)),
        ("Posted 30+ Days Ago", (None, 30)),
        ("Posted Yesterday", (None, None)),  # not a form seen on a live board
        ("Posted 1 Day Ago", (None, None)),
        (7, (None, None)),
    ],
)
def test_workday_relative_dates(text: object, expected: tuple[object, object]) -> None:
    assert ats._workday_posted(text, dt.date(2026, 11, 1)) == expected
    assert ats._workday_posted("Posted Today", None) == (None, None)


@pytest.mark.parametrize("value", [0, 366, "7", True, 7.5])
def test_max_posting_age_days_is_checked(value: object) -> None:
    doc = {"search": {"max_posting_age_days": value}}
    assert any("max_posting_age_days" in p for p in validate_config(doc))
    assert max_posting_age_days(doc) == 7


def test_max_posting_age_days_defaults_to_seven() -> None:
    assert max_posting_age_days({}) == 7
    assert max_posting_age_days({"search": {"max_posting_age_days": 30}}) == 30
