"""search.db's schema: posting identity, enums, and the fixed link to the EI record."""

from __future__ import annotations

import sqlite3
from itertools import count

import pytest

from careerhunter import enums

_ids = count(1)


def _corr() -> str:
    return f"{next(_ids):032x}"


def _company(conn: sqlite3.Connection, name: str = "Acme Widgets") -> int:
    cur = conn.execute("INSERT INTO companies (name, source) VALUES (?, 'manual')", (name,))
    return int(cur.lastrowid or 0)


def _posting(conn: sqlite3.Connection, company_id: int, title: str = "Clerk") -> int:
    cur = conn.execute(
        "INSERT INTO postings (company_id, title, first_seen, source) "
        "VALUES (?, ?, '2026-09-01', 'manual')",
        (company_id, title),
    )
    return int(cur.lastrowid or 0)


def _application(conn: sqlite3.Connection, posting_id: int, **cols: object) -> int:
    values = {
        "date_applied": "2026-09-01",
        "date_precision": "exact",
        "week_of": None,
        "method": "email",
    } | cols
    cur = conn.execute(
        "INSERT INTO applications (correlation_id, posting_id, date_applied, date_precision, "
        "week_of, method) VALUES (?, ?, ?, ?, ?, ?)",
        (
            _corr(),
            posting_id,
            values["date_applied"],
            values["date_precision"],
            values["week_of"],
            values["method"],
        ),
    )
    return int(cur.lastrowid or 0)


@pytest.mark.parametrize(
    ("cols", "why"),
    [
        ({"method": "carrier_pigeon"}, "bad method"),
        ({"date_precision": "exact", "date_applied": None}, "exact without date"),
        ({"date_precision": "week", "date_applied": None, "week_of": "2026-09-09"}, "not Monday"),
        ({"date_precision": "week", "date_applied": None, "week_of": None}, "week without week_of"),
        ({"date_precision": "unknown"}, "unknown with a date"),
        ({"date_applied": "2026-13-45"}, "invalid date"),
        ({"date_precision": "sometime"}, "bad precision"),
    ],
)
def test_application_checks(search: sqlite3.Connection, cols: dict[str, object], why: str) -> None:
    posting = _posting(search, _company(search))
    with pytest.raises(sqlite3.IntegrityError):
        _application(search, posting, **cols)


def test_application_precisions_accepted(search: sqlite3.Connection) -> None:
    c = _company(search)
    _application(search, _posting(search, c, "A"))
    _application(
        search,
        _posting(search, c, "B"),
        date_precision="week",
        date_applied=None,
        week_of="2026-09-07",
    )
    _application(search, _posting(search, c, "C"), date_precision="unknown", date_applied=None)


def test_reapplying_to_a_posting_on_another_date(search: sqlite3.Connection) -> None:
    posting = _posting(search, _company(search))
    _application(search, posting, date_applied="2026-03-10")
    _application(search, posting, date_applied="2026-07-15")
    with pytest.raises(sqlite3.IntegrityError):
        _application(search, posting, date_applied="2026-07-15")


def _raw_posting(conn: sqlite3.Connection, company_id: int, **cols: object) -> None:
    values = {
        "title": "Clerk",
        "location": "",
        "url": None,
        "requisition_id": None,
        "first_seen": "2026-03-02",
    } | cols
    conn.execute(
        "INSERT INTO postings (company_id, title, location, url, requisition_id, first_seen, "
        "source) VALUES (:c, :title, :location, :url, :requisition_id, :first_seen, 'manual')",
        {"c": company_id, **values},
    )


def test_posting_fallback_key_is_per_month(search: sqlite3.Connection) -> None:
    c = _company(search)
    _raw_posting(search, c, first_seen="2026-03-02")
    with pytest.raises(sqlite3.IntegrityError):
        _raw_posting(search, c, first_seen="2026-03-28", title="CLERK")  # same month, any case
    _raw_posting(search, c, first_seen="2026-07-01")  # re-posted later: a new posting


def test_posting_url_and_requisition_keys(search: sqlite3.Connection) -> None:
    c = _company(search)
    other = _company(search, "Globex Corporation")
    _raw_posting(search, c, url="https://acme.example.com/jobs/1")
    _raw_posting(search, c, url="https://acme.example.com/jobs/2")  # same title, other url
    with pytest.raises(sqlite3.IntegrityError):
        _raw_posting(search, c, url="https://acme.example.com/jobs/1", title="Other")
    _raw_posting(search, c, requisition_id="REQ-7")
    _raw_posting(search, other, requisition_id="REQ-7")  # requisition ids are per company
    with pytest.raises(sqlite3.IntegrityError):
        _raw_posting(search, c, requisition_id="REQ-7", first_seen="2026-08-01")


@pytest.mark.parametrize(
    "ats",
    [
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "successfactors",
        "icims",
        "taleo",
        "smartrecruiters",
        "jobvite",
        "bamboohr",
        "other",
    ],
)
def test_ats_types_accepted(search: sqlite3.Connection, ats: str) -> None:
    search.execute(
        "INSERT INTO companies (name, source, ats_type) VALUES ('X', 'manual', ?)", (ats,)
    )


@pytest.mark.parametrize(
    "method",
    [
        "company_site",
        "linkedin_easy_apply",
        "email",
        "referral",
        "agency",
        "recruiter_inbound",
        "other",
    ],
)
def test_methods_accepted(search: sqlite3.Connection, method: str) -> None:
    _application(search, _posting(search, _company(search)), method=method)


def test_schema_enums_match_python(search: sqlite3.Connection) -> None:
    for source in enums.SOURCES:
        search.execute(
            "INSERT INTO companies (name, source) VALUES (?, ?)", (f"Source {source}", source)
        )
    for ats in enums.ATS_TYPES:
        search.execute(
            "INSERT INTO companies (name, source, ats_type) VALUES (?, 'manual', ?)",
            (f"Co {ats}", ats),
        )
    c = _company(search, "Method Co")
    for i, method in enumerate(enums.METHODS):
        _application(search, _posting(search, c, f"Role {i}"), method=method)


def test_company_names_unique_case_insensitively(search: sqlite3.Connection) -> None:
    _company(search, "Acme Widgets")
    with pytest.raises(sqlite3.IntegrityError):
        _company(search, "ACME widgets")


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("INSERT INTO companies (name, source) VALUES ('X', 'linkedin')", ()),
        ("INSERT INTO companies (name, source, ats_type) VALUES ('X', 'manual', 'taleo9')", ()),
        ("INSERT INTO companies (name, source, watchlist) VALUES ('X', 'manual', 2)", ()),
        ("INSERT INTO companies (name, source) VALUES ('  ', 'manual')", ()),
        (
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (999, 'T', '2026-09-01', 'manual')",
            (),
        ),
        (
            "INSERT INTO postings (company_id, title, first_seen, source, work_model) "
            "VALUES (:c, 'T', '2026-09-01', 'manual', 'office')",
            ("c",),
        ),
        (
            "INSERT INTO postings (company_id, title, first_seen, source, fit_rating) "
            "VALUES (:c, 'T', '2026-09-01', 'manual', 11)",
            ("c",),
        ),
        (
            "INSERT INTO postings (company_id, title, first_seen, source, snapshot_path) "
            "VALUES (:c, 'T', '2026-09-01', 'manual', '/Volumes/x/a.html')",
            ("c",),
        ),
        (
            "INSERT INTO postings (company_id, title, first_seen, source) "
            "VALUES (:c, 'T', '2026-09-01', 'linkedin')",
            ("c",),
        ),
        ("INSERT INTO skips (posting_id, date, reason) VALUES (999, '2026-09-01', 'x')", ()),
    ],
)
def test_constraints_reject(search: sqlite3.Connection, sql: str, params: tuple[str, ...]) -> None:
    c = _company(search, "Base Co")
    with pytest.raises(sqlite3.IntegrityError):
        search.execute(sql, {"c": c} if params else ())


def test_status_values_checked(search: sqlite3.Connection) -> None:
    app = _application(search, _posting(search, _company(search)))
    for status in enums.STATUSES:
        search.execute(
            "INSERT INTO decision_history (application_id, status, status_date) "
            "VALUES (?, ?, '2026-09-02')",
            (app, status),
        )
    with pytest.raises(sqlite3.IntegrityError):
        search.execute(
            "INSERT INTO decision_history (application_id, status) VALUES (?, 'ghosted')", (app,)
        )


def test_search_is_editable(search: sqlite3.Connection) -> None:
    """No retention triggers here, so a later retention policy can compact this layer."""
    p = _posting(search, _company(search))
    a = _application(search, p)
    search.execute(
        "INSERT INTO decision_history (application_id, status) VALUES (?, 'applied')", (a,)
    )
    search.execute(
        "INSERT INTO skips (posting_id, date, reason) VALUES (?, '2026-09-01', 'x')", (p,)
    )
    search.execute("UPDATE postings SET fit_rating = 4, gaps = 'none'")
    search.execute("UPDATE decision_history SET status = 'rejected'")
    for table in ("decision_history", "skips", "applications", "postings", "companies"):
        search.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names


def test_correlation_id_is_fixed_and_opaque(search: sqlite3.Connection) -> None:
    a = _application(search, _posting(search, _company(search)))
    with pytest.raises(sqlite3.IntegrityError, match="can not be changed"):
        search.execute("UPDATE applications SET correlation_id = ? WHERE id = ?", (_corr(), a))
    search.execute("UPDATE applications SET notes = 'x' WHERE id = ?", (a,))  # others can change
    p = _posting(search, _company(search, "Other Co"))
    for bad in ("short", "G" * 32, "A" * 32):
        with pytest.raises(sqlite3.IntegrityError):
            search.execute(
                "INSERT INTO applications (correlation_id, posting_id, date_precision, method) "
                "VALUES (?, ?, 'unknown', 'email')",
                (bad, p),
            )


def test_ei_claim_is_set_once(search: sqlite3.Connection) -> None:
    a = _application(search, _posting(search, _company(search)))
    with pytest.raises(sqlite3.IntegrityError):
        search.execute("UPDATE applications SET ei_claim = '../x' WHERE id = ?", (a,))
    search.execute("UPDATE applications SET ei_claim = '2026-27' WHERE id = ?", (a,))
    search.execute("UPDATE applications SET ei_claim = '2026-27' WHERE id = ?", (a,))
    with pytest.raises(sqlite3.IntegrityError, match="set once"):
        search.execute("UPDATE applications SET ei_claim = '2027-28' WHERE id = ?", (a,))
    with pytest.raises(sqlite3.IntegrityError, match="set once"):
        search.execute("UPDATE applications SET ei_claim = NULL WHERE id = ?", (a,))


def test_scout_metadata_must_be_json(search: sqlite3.Connection) -> None:
    c = _company(search)
    search.execute(
        "INSERT INTO postings (company_id, title, first_seen, source, scout_metadata) "
        "VALUES (?, 'A', '2026-09-01', 'watcher', '{\"board\": \"greenhouse\"}')",
        (c,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        search.execute(
            "INSERT INTO postings (company_id, title, first_seen, source, scout_metadata) "
            "VALUES (?, 'B', '2026-09-01', 'watcher', 'not json')",
            (c,),
        )
