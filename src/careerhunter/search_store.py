"""Reads and writes on search.db, the search and learning layer."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from careerhunter.enums import FIT_LEVELS
from careerhunter.filters import title_key
from careerhunter.models import Application, Dating, Posting


class IdentityConflict(Exception):
    """A new record contradicts one already in search.db. The message is user-facing."""


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    *,
    ats_type: str | None = None,
    ats_slug: str | None = None,
    careers_url: str | None = None,
    watchlist: int = 0,
    source: str = "manual",
    ats_host: str | None = None,
    ats_site: str | None = None,
) -> tuple[int, bool]:
    """The company's id, inserting it if new. Returns (id, inserted).

    `ats_host` and `ats_site` are Workday's wdN host and site (search migration 0012).
    """
    cur = conn.execute(
        "INSERT INTO companies (name, ats_type, ats_slug, ats_host, ats_site, careers_url, "
        "watchlist, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (name, ats_type, ats_slug, ats_host, ats_site, careers_url, watchlist, source),
    )
    found = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    return int(found["id"]), cur.rowcount == 1


def find_posting(
    conn: sqlite3.Connection,
    company_id: int,
    posting: Posting,
    dating: Dating,
    *,
    ats_key: str | None = None,
) -> int | None:
    """Match an existing posting using the schema's identity rules, strongest first.

    `ats_key` is the posting's identity on its board (search migration 0015), the
    strongest: a Workday posting retitled or moved since it was stored has a new URL
    but the same key, so it is the same posting.
    """
    if ats_key:
        row = conn.execute(
            "SELECT id, company_id FROM postings WHERE ats_key = ?", (ats_key,)
        ).fetchone()
        if row:
            if row["company_id"] != company_id:
                raise IdentityConflict(
                    f"{posting.company} / {posting.title}: board identity {ats_key!r} is "
                    "already recorded for a posting at a different company"
                )
            return int(row["id"])
    if posting.url:
        row = conn.execute(
            "SELECT id, company_id FROM postings WHERE url = ?", (posting.url,)
        ).fetchone()
        if row:
            if row["company_id"] != company_id:
                raise IdentityConflict(
                    f"{posting.company} / {posting.title}: url {posting.url!r} is already "
                    "recorded for a posting at a different company"
                )
            return int(row["id"])
    if posting.requisition_id:
        row = conn.execute(
            "SELECT id FROM postings WHERE company_id = ? AND requisition_id = ?",
            (company_id, posting.requisition_id),
        ).fetchone()
        if row:
            return int(row["id"])
    if posting.url or posting.requisition_id:
        return None
    fallback = (
        "SELECT p.id FROM postings p {join} WHERE p.company_id = ? AND p.title = ? "
        "AND p.location = ? AND p.url IS NULL AND p.requisition_id IS NULL {extra} LIMIT 1"
    )
    key = (company_id, posting.title, posting.location)
    row = conn.execute(
        fallback.format(join="", extra="AND substr(p.first_seen, 1, 7) = substr(?, 1, 7)"),
        (*key, posting.first_seen),
    ).fetchone()
    if row is None and dating.precision == "unknown":
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


def create_posting(conn: sqlite3.Connection, company_id: int, posting: Posting) -> int:
    cur = conn.execute(
        "INSERT INTO postings (company_id, title, location, work_model, schedule, url, "
        "requisition_id, first_seen, source, fit_rating, gaps, llm_output, scout_metadata, "
        "snapshot_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            company_id,
            posting.title,
            posting.location,
            posting.work_model,
            posting.schedule,
            posting.url,
            posting.requisition_id,
            posting.first_seen,
            posting.source,
            posting.fit_rating,
            posting.gaps,
            posting.llm_output,
            posting.scout_metadata,
            posting.snapshot_path,
        ),
    )
    return int(cur.lastrowid or 0)


def find_or_create_posting(
    conn: sqlite3.Connection, posting: Posting, dating: Dating, *, ats_key: str | None = None
) -> tuple[int, int, bool, bool]:
    """Returns (company_id, posting_id, company inserted, posting inserted)."""
    company_id, company_new = upsert_company(conn, posting.company, source=posting.source)
    posting_id = find_posting(conn, company_id, posting, dating, ats_key=ats_key)
    if posting_id is not None:
        return company_id, posting_id, company_new, False
    return company_id, create_posting(conn, company_id, posting), company_new, True


def find_application(
    conn: sqlite3.Connection, posting_id: int, dating: Dating
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, correlation_id, ei_claim FROM applications WHERE posting_id = ? "
        "AND date_precision = ? AND coalesce(date_applied, '') = coalesce(?, '') "
        "AND coalesce(week_of, '') = coalesce(?, '')",
        (posting_id, dating.precision, dating.day, dating.week_of),
    ).fetchone()
    return row


def application_by_correlation(conn: sqlite3.Connection, correlation_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, correlation_id, ei_claim FROM applications WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    return row


def insert_application(
    conn: sqlite3.Connection,
    posting_id: int,
    app: Application,
    correlation_id: str,
    ei_claim: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO applications (correlation_id, ei_claim, posting_id, date_applied, "
        "date_precision, week_of, method, resume_file, reasoning, notes, jd_expected) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            correlation_id,
            ei_claim,
            posting_id,
            app.dating.day,
            app.dating.precision,
            app.dating.week_of,
            app.method,
            app.resume_file,
            app.reasoning,
            app.notes,
        ),
    )
    return int(cur.lastrowid or 0)


def set_application_jd(
    conn: sqlite3.Connection, application_id: int, jd_sha256: str | None, jd_path: str | None
) -> None:
    """The application's current JD: its text hash and its snapshot file."""
    conn.execute(
        "UPDATE applications SET jd_sha256 = ?, jd_path = ? WHERE id = ?",
        (jd_sha256, jd_path, application_id),
    )


@dataclass(frozen=True)
class JdReference:
    """An application's JD fields in search.db, with its posting's URL and snapshot."""

    application_id: int
    posting_id: int
    ei_claim: str | None
    jd_expected: int
    jd_sha256: str | None
    jd_path: str | None
    url: str | None
    snapshot_path: str | None


_JD_REFERENCES = (
    "SELECT a.correlation_id, a.id, a.posting_id, a.ei_claim, a.jd_expected, a.jd_sha256, "
    "a.jd_path, p.url, p.snapshot_path FROM applications a JOIN postings p ON p.id = a.posting_id"
)


def _jd_reference(row: sqlite3.Row) -> JdReference:
    return JdReference(
        application_id=int(row["id"]),
        posting_id=int(row["posting_id"]),
        ei_claim=row["ei_claim"],
        jd_expected=int(row["jd_expected"]),
        jd_sha256=row["jd_sha256"],
        jd_path=row["jd_path"],
        url=row["url"],
        snapshot_path=row["snapshot_path"],
    )


def jd_reference(conn: sqlite3.Connection, correlation_id: str) -> JdReference | None:
    row = conn.execute(_JD_REFERENCES + " WHERE a.correlation_id = ?", (correlation_id,)).fetchone()
    return _jd_reference(row) if row else None


def jd_references(conn: sqlite3.Connection) -> dict[str, JdReference]:
    """Every application's JD fields, keyed by correlation id."""
    return {str(r["correlation_id"]): _jd_reference(r) for r in conn.execute(_JD_REFERENCES)}


@dataclass(frozen=True)
class StoredApplication:
    """One application in search.db with its posting and company, for the import."""

    application_id: int
    correlation_id: str
    ei_claim: str | None
    posting_id: int
    company: str
    title: str
    location: str
    url: str | None
    snapshot_path: str | None
    dating: Dating
    jd_sha256: str | None


_STORED_APPLICATIONS = (
    "SELECT a.id, a.correlation_id, a.ei_claim, a.posting_id, c.name, p.title, p.location, "
    "p.url, p.snapshot_path, a.date_precision, a.date_applied, a.week_of, a.jd_sha256 "
    "FROM applications a JOIN postings p ON p.id = a.posting_id "
    "JOIN companies c ON c.id = p.company_id"
)


def _stored_application(row: sqlite3.Row) -> StoredApplication:
    return StoredApplication(
        application_id=int(row["id"]),
        correlation_id=str(row["correlation_id"]),
        ei_claim=row["ei_claim"],
        posting_id=int(row["posting_id"]),
        company=str(row["name"]),
        title=str(row["title"]),
        location=str(row["location"] or ""),
        url=row["url"],
        snapshot_path=row["snapshot_path"],
        dating=Dating(str(row["date_precision"]), row["date_applied"], row["week_of"]),
        jd_sha256=row["jd_sha256"],
    )


def stored_applications(conn: sqlite3.Connection) -> list[StoredApplication]:
    """Every application, oldest first."""
    return [_stored_application(r) for r in conn.execute(_STORED_APPLICATIONS + " ORDER BY a.id")]


def stored_application(conn: sqlite3.Connection, application_id: int) -> StoredApplication | None:
    row = conn.execute(_STORED_APPLICATIONS + " WHERE a.id = ?", (application_id,)).fetchone()
    return _stored_application(row) if row else None


def company_id(conn: sqlite3.Connection, name: str) -> int | None:
    """The id of the company with exactly this name, or None. Writes nothing."""
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    return int(row["id"]) if row else None


def posting_company(conn: sqlite3.Connection, posting_id: int) -> str | None:
    row = conn.execute(
        "SELECT c.name FROM postings p JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
        (posting_id,),
    ).fetchone()
    return str(row["name"]) if row else None


def jd_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Applications with no JD: (expected one, from before JDs were kept)."""
    row = conn.execute(
        "SELECT coalesce(sum(jd_expected = 1), 0), coalesce(sum(jd_expected = 0), 0) "
        "FROM applications WHERE jd_sha256 IS NULL"
    ).fetchone()
    return int(row[0]), int(row[1])


def posting_urls(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Every stored posting URL, as stored, with its posting id."""
    return [
        (int(r["id"]), str(r["url"]))
        for r in conn.execute("SELECT id, url FROM postings WHERE url IS NOT NULL")
    ]


def set_posting_url(conn: sqlite3.Connection, posting_id: int, url: str) -> None:
    """Fill a posting's empty URL. A posting that has one keeps it."""
    conn.execute("UPDATE postings SET url = ? WHERE id = ? AND url IS NULL", (url, posting_id))


def link_to_claim(conn: sqlite3.Connection, application_id: int, label: str) -> None:
    conn.execute("UPDATE applications SET ei_claim = ? WHERE id = ?", (label, application_id))


def add_decision(
    conn: sqlite3.Connection,
    application_id: int,
    status: str,
    status_date: str | None,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO decision_history (application_id, status, status_date, note) "
        "VALUES (?, ?, ?, ?)",
        (application_id, status, status_date, note),
    )


def set_posted_date(conn: sqlite3.Connection, posting_id: int, posted: str | None) -> None:
    """The posting's own posted date, YYYY-MM-DD (search migration 0013), or null."""
    conn.execute("UPDATE postings SET posted_date = ? WHERE id = ?", (posted, posting_id))


def posting_key_known(conn: sqlite3.Connection, key: str) -> bool:
    """Whether a posting with this ATS identity (search migration 0015) is stored."""
    found = conn.execute("SELECT 1 FROM postings WHERE ats_key = ?", (key,)).fetchone()
    return found is not None


def set_ats_key(conn: sqlite3.Connection, posting_id: int, key: str) -> bool:
    """Record a posting's ATS identity once. False when it has one already, or another
    posting holds that key (two stored rows for one posting are left as they are)."""
    cur = conn.execute(
        "UPDATE postings SET ats_key = ? WHERE id = ? AND ats_key IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM postings WHERE ats_key = ?)",
        (key, posting_id, key),
    )
    return cur.rowcount == 1


def posting_url_known(conn: sqlite3.Connection, url: str) -> bool:
    """Whether a posting with this exact URL is stored, from any source."""
    return conn.execute("SELECT 1 FROM postings WHERE url = ?", (url,)).fetchone() is not None


def posting_has_application(conn: sqlite3.Connection, posting_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM applications WHERE posting_id = ? LIMIT 1", (posting_id,)
    ).fetchone()
    return row is not None


def posting_has_skip(conn: sqlite3.Connection, posting_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM skips WHERE posting_id = ? LIMIT 1", (posting_id,)).fetchone()
    return row is not None


def set_posting_schedule(conn: sqlite3.Connection, posting_id: int, schedule: str | None) -> None:
    conn.execute("UPDATE postings SET schedule = ? WHERE id = ?", (schedule, posting_id))


def set_posting_score(
    conn: sqlite3.Connection,
    posting_id: int,
    *,
    gaps: str,
    llm_output: str,
    scout_metadata: str | None,
    fit: str | None,
    resume_sha256: str | None = None,
    key_requirements: Sequence[str] = (),
    run_id: int | None = None,
) -> None:
    """Store parsed gaps, model text, the tier, its fit, the resume's hash and the rest.

    `fit` is the tier's fit from config, or None when there is no tier or its name is
    not in config. `fit_rating` is not touched. `resume_sha256` is the resume the score
    was made with, or None without one. `key_requirements` (at most three, already
    checked by the scorer) are stored as a JSON array, or null when there are none.
    `run_id` is the watch run that made the score, or None outside one. Every value
    replaces the stored one in a single UPDATE, so they always change together.
    """
    requirements = (
        json.dumps(list(key_requirements), ensure_ascii=False) if key_requirements else None
    )
    conn.execute(
        "UPDATE postings SET gaps = ?, llm_output = ?, scout_metadata = ?, fit = ?, "
        "scored_resume_sha256 = ?, key_requirements = ?, score_run_id = ? WHERE id = ?",
        (gaps, llm_output, scout_metadata, fit, resume_sha256, requirements, run_id, posting_id),
    )


def fill_missing_fits(
    conn: sqlite3.Connection, fits: Mapping[str, str], run_id: int | None = None
) -> int:
    """Give a fit to scored, unalerted watched or discovered postings with a tier but no fit.

    The fit is looked up by the stored tier name in `fits` (from the current config).
    A name that is not there is left alone: that posting stays without a fit and is not
    alerted. A posting that gets a fit also gets `run_id` as its scoring run, since it
    becomes alertable in that run. Returns how many postings got a fit. The caller
    holds the transaction.
    """
    rows = conn.execute(
        "SELECT p.id, json_extract(p.scout_metadata, '$.tier') AS tier FROM postings p "
        "WHERE p.source IN ('watcher', 'discovery') AND p.llm_output IS NOT NULL AND p.fit IS NULL "
        "AND p.alerted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id) "
        "ORDER BY p.id"
    ).fetchall()
    filled = 0
    for row in rows:
        tier = row["tier"]
        fit = fits.get(tier) if isinstance(tier, str) else None
        if fit is None:
            continue
        conn.execute(
            "UPDATE postings SET fit = ?, score_run_id = ? WHERE id = ? AND fit IS NULL",
            (fit, run_id, row["id"]),
        )
        filled += 1
    return filled


def score_attempted_at(conn: sqlite3.Connection, posting_id: int) -> str | None:
    row = conn.execute(
        "SELECT score_attempted_at FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def set_score_attempted_at(conn: sqlite3.Connection, posting_id: int, stamp: str | None) -> None:
    """When the posting was last sent to the model; None clears it."""
    conn.execute("UPDATE postings SET score_attempted_at = ? WHERE id = ?", (stamp, posting_id))


@dataclass(frozen=True)
class HeldCounts:
    """Watcher and discovery postings with no skip held back from alerts, by why."""

    unscored: int  # no score yet: waiting to be scored or retried
    no_fit: int  # scored and not alerted, but its tier name has no fit in config
    off_lane: int = 0  # scored, off-lane, and below search.off_lane_min_fit


def held_counts(conn: sqlite3.Connection, *, off_lane_min_fit: str | None = None) -> HeldCounts:
    """How many watcher and discovery postings with no skip are held, read now.

    `off_lane_min_fit` is `search.off_lane_min_fit` while lane checking is on: an
    off-lane posting with a lower fit is held. None means lane checking is off, and no
    posting is held for its lane.
    """
    row = conn.execute(
        """
        SELECT
          coalesce(sum(p.llm_output IS NULL), 0) AS unscored,
          coalesce(sum(p.llm_output IS NOT NULL AND p.fit IS NULL AND p.alerted_at IS NULL), 0)
            AS no_fit
        FROM postings p
        WHERE p.source IN ('watcher', 'discovery')
          AND NOT EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id)
        """
    ).fetchone()
    off_lane = 0
    if off_lane_min_fit is not None:
        off_lane = int(
            conn.execute(
                "SELECT count(*) FROM postings p "
                "WHERE p.source IN ('watcher', 'discovery') AND p.llm_output IS NOT NULL "
                "AND p.alerted_at IS NULL AND p.lane_checked = 1 AND p.lane IS NULL "
                "AND (CASE p.fit WHEN 'low' THEN 0 WHEN 'medium' THEN 1 WHEN 'high' THEN 2 END) "
                "< ? "
                "AND NOT EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id)",
                (FIT_LEVELS.index(off_lane_min_fit),),
            ).fetchone()[0]
        )
    return HeldCounts(unscored=int(row["unscored"]), no_fit=int(row["no_fit"]), off_lane=off_lane)


COMPONENTS = ("lm_studio", "searxng", "boards")
FAILURE_KINDS = ("unreachable", "bad-reply", "throttled", "failed")


def record_component(
    conn: sqlite3.Connection, component: str, *, failed: str | None, stamp: str
) -> None:
    """One run's outcome for a component (search migration 0018). `failed` is the kind
    of failure, or None when the component worked. The caller holds the transaction.

    A failure adds one to the runs in a row that failed it. Working clears the count,
    and when the bot had sent a failure message for this outage, marks the recovery
    message due. Either way the postings held for scoring are counted now. A run that
    never reached the component does not call this, so it is counted as neither.
    """
    if component not in COMPONENTS:
        raise ValueError(f"unknown component {component!r}")
    if failed is not None and failed not in FAILURE_KINDS:
        raise ValueError(f"unknown failure kind {failed!r}")
    held = held_counts(conn).unscored
    if failed is None:
        conn.execute(
            "UPDATE component_health SET failures = 0, last_failure = NULL, held = ?, "
            "updated_at = ?, "
            "recovery_due = CASE WHEN alerted_failures IS NOT NULL THEN 1 ELSE recovery_due END, "
            "alerted_failures = NULL WHERE component = ?",
            (held, stamp, component),
        )
    else:
        conn.execute(
            "UPDATE component_health SET failures = failures + 1, last_failure = ?, held = ?, "
            "updated_at = ? WHERE component = ?",
            (failed, held, stamp, component),
        )


@dataclass(frozen=True)
class Health:
    component: str
    failures: int
    last_failure: str | None
    held: int | None
    alerted_failures: int | None
    recovery_due: bool


def component_health(conn: sqlite3.Connection) -> list[Health]:
    """Every component's row, in COMPONENTS order."""
    rows = {str(row["component"]): row for row in conn.execute("SELECT * FROM component_health")}
    return [
        Health(
            component=name,
            failures=int(rows[name]["failures"]),
            last_failure=None
            if rows[name]["last_failure"] is None
            else str(rows[name]["last_failure"]),
            held=None if rows[name]["held"] is None else int(rows[name]["held"]),
            alerted_failures=None
            if rows[name]["alerted_failures"] is None
            else int(rows[name]["alerted_failures"]),
            recovery_due=bool(rows[name]["recovery_due"]),
        )
        for name in COMPONENTS
        if name in rows
    ]


def mark_failure_told(conn: sqlite3.Connection, component: str, count: int, limit: int) -> None:
    """The bot sent this outage's failure message, naming `count` failures. The caller
    holds the transaction. If the component worked again since the bot read it (the
    count went back below `limit`), the outage is over: its recovery message is due."""
    cur = conn.execute(
        "UPDATE component_health SET alerted_failures = ? "
        "WHERE component = ? AND alerted_failures IS NULL AND failures >= ?",
        (count, component, limit),
    )
    if cur.rowcount == 0:
        conn.execute(
            "UPDATE component_health SET recovery_due = 1 "
            "WHERE component = ? AND alerted_failures IS NULL AND failures < ?",
            (component, limit),
        )


def mark_recovery_told(conn: sqlite3.Connection, component: str) -> None:
    """The bot sent this component's recovery message."""
    conn.execute(
        "UPDATE component_health SET recovery_due = 0 WHERE component = ? AND recovery_due = 1",
        (component,),
    )


def set_posting_lane(conn: sqlite3.Connection, posting_id: int, lane: str | None) -> bool:
    """Record the lane a posting matched (None: off-lane), once. False if already set."""
    cur = conn.execute(
        "UPDATE postings SET lane = ?, lane_checked = 1 WHERE id = ? AND lane_checked = 0",
        (lane, posting_id),
    )
    return cur.rowcount == 1


@dataclass(frozen=True)
class CheckedLane:
    """A posting already checked against the lanes, for `lanes reassign`."""

    posting_id: int
    title: str
    lane: str | None  # None: off-lane
    posted: str  # the posted date, or first_seen when the board gave none


def checked_lane_titles(conn: sqlite3.Connection) -> list[CheckedLane]:
    """Every posting already checked against the lanes, oldest first."""
    rows = conn.execute(
        "SELECT id, title, lane, coalesce(posted_date, first_seen) AS posted "
        "FROM postings WHERE lane_checked = 1 ORDER BY id"
    )
    return [
        CheckedLane(
            posting_id=int(row["id"]),
            title=str(row["title"]),
            lane=None if row["lane"] is None else str(row["lane"]),
            posted=str(row["posted"]),
        )
        for row in rows
    ]


def move_posting_lane(
    conn: sqlite3.Connection, posting_id: int, old: str | None, new: str | None
) -> bool:
    """Move a checked posting from lane `old` to `new` (None: off-lane). False when the
    posting is not checked or no longer has `old`. A trigger logs it to `lane_changes`."""
    cur = conn.execute(
        "UPDATE postings SET lane = ? WHERE id = ? AND lane_checked = 1 AND lane IS ?",
        (new, posting_id, old),
    )
    return cur.rowcount == 1


def unchecked_lane_titles(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Every posting whose title has not been checked against the lanes, oldest first."""
    rows = conn.execute("SELECT id, title FROM postings WHERE lane_checked = 0 ORDER BY id")
    return [(int(row["id"]), str(row["title"])) for row in rows]


def start_watch_run(conn: sqlite3.Connection, started_at: str) -> int:
    """A new, unfinished `watch_runs` row. Its counts stay null until it finishes."""
    cur = conn.execute("INSERT INTO watch_runs (started_at) VALUES (?)", (started_at,))
    return int(cur.lastrowid or 0)


def finish_watch_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    budget_reached: bool,
    boards_fetched: int,
    boards_failed: int,
    boards_left: int,
    held: HeldCounts,
    useful: bool | None = None,
) -> None:
    """Set the finish time, the budget flag, every count and whether the run did useful
    work (search migration 0018) in one UPDATE."""
    conn.execute(
        "UPDATE watch_runs SET finished_at = ?, budget_reached = ?, boards_fetched = ?, "
        "boards_failed = ?, boards_left = ?, held_unscored = ?, held_no_fit = ?, "
        "held_off_lane = ?, useful = ? WHERE id = ? AND finished_at IS NULL",
        (
            finished_at,
            int(budget_reached),
            boards_fetched,
            boards_failed,
            boards_left,
            held.unscored,
            held.no_fit,
            held.off_lane,
            None if useful is None else int(useful),
            run_id,
        ),
    )


@dataclass(frozen=True)
class PendingAlert:
    """A watcher or discovery posting the bot may alert: scored, with a fit, no skip, not
    alerted. Discovery postings alert directly, with no approval (#41)."""

    posting_id: int
    fit: str
    lane: str | None
    lane_checked: bool
    first_seen: str
    run_id: int | None = None  # the watch run that scored it (search migration 0011)
    run_finished: bool = False  # that run has finished its watch_runs row
    run_started_at: str | None = None


def pending_alerts(conn: sqlite3.Connection) -> list[PendingAlert]:
    """Watcher and discovery postings with a score and a fit, no skip, no application,
    no alert yet, not given up after Telegram refused it too often (#42), and not a
    repost of a role the operator logged or skipped (#34).
    Oldest id first.

    A posting without a fit (unscored, or scored into a tier name not in config) is held.
    The bot orders these, and holds some for their lane (`lanes.alert_order`).
    """
    rows = conn.execute(
        """
        SELECT p.id, p.company_id, p.title, p.fit, p.lane, p.lane_checked, p.first_seen,
               p.score_run_id, r.finished_at, r.started_at
        FROM postings p LEFT JOIN watch_runs r ON r.id = p.score_run_id
        WHERE p.source IN ('watcher', 'discovery')
          AND p.llm_output IS NOT NULL
          AND p.fit IS NOT NULL
          AND p.alerted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id)
          AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id)
          AND NOT EXISTS (SELECT 1 FROM alert_refusals r
                          WHERE r.posting_id = p.id AND r.given_up_at IS NOT NULL)
        ORDER BY p.id
        """
    ).fetchall()
    # A repost of a role the operator logged or skipped is never alerted, even before
    # the next watch run gives it its `already_handled` skip (#34).
    handled = handled_title_keys(conn) if rows else {}
    rows = [
        row
        for row in rows
        if title_key(str(row["title"])) not in handled.get(int(row["company_id"]), set())
    ]
    return [
        PendingAlert(
            posting_id=int(row["id"]),
            fit=str(row["fit"]),
            lane=None if row["lane"] is None else str(row["lane"]),
            lane_checked=bool(row["lane_checked"]),
            first_seen=str(row["first_seen"]),
            run_id=None if row["score_run_id"] is None else int(row["score_run_id"]),
            run_finished=row["finished_at"] is not None,
            run_started_at=None if row["started_at"] is None else str(row["started_at"]),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class DigestFields:
    """What one digest row shows. Never the gap names, the model text or notes."""

    posting_id: int
    company: str
    title: str
    location: str
    url: str | None
    schedule: str | None
    fit: str | None
    gap_count: int | None  # None when the stored gaps are not a JSON list
    key_requirements: tuple[str, ...]


def digest_fields(conn: sqlite3.Connection, posting_ids: Sequence[int]) -> dict[int, DigestFields]:
    """The digest fields of these postings, by id. A posting no longer stored is absent.

    The gap count is read from the stored gap list in SQL, so the gap names themselves
    never leave the database here.
    """
    found: dict[int, DigestFields] = {}
    for posting_id in posting_ids:
        row = conn.execute(
            """
            SELECT p.id, c.name AS company, p.title, p.location, p.url, p.schedule, p.fit,
                   CASE WHEN json_valid(p.gaps) AND json_type(p.gaps) = 'array'
                        THEN json_array_length(p.gaps) END AS gap_count,
                   p.key_requirements
            FROM postings p JOIN companies c ON c.id = p.company_id
            WHERE p.id = ?
            """,
            (posting_id,),
        ).fetchone()
        if row is None:
            continue
        found[posting_id] = DigestFields(
            posting_id=posting_id,
            company=str(row["company"]),
            title=str(row["title"]),
            location=str(row["location"] or ""),
            url=None if row["url"] is None else str(row["url"]),
            schedule=None if row["schedule"] is None else str(row["schedule"]),
            fit=None if row["fit"] is None else str(row["fit"]),
            gap_count=None if row["gap_count"] is None else int(row["gap_count"]),
            key_requirements=_requirements(row["key_requirements"]),
        )
    return found


def _requirements(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str):
        return ()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(item for item in loaded if isinstance(item, str) and item.strip())


def pending_alert_ids(conn: sqlite3.Connection) -> list[int]:
    """The ids of `pending_alerts`, oldest id first, before any lane ordering."""
    return [alert.posting_id for alert in pending_alerts(conn)]


def record_refusal(
    conn: sqlite3.Connection, posting_id: int, stamp: str, *, give_up_after: int
) -> bool:
    """Count one Telegram refusal of this posting (search migration 0017). True when this
    one reached `give_up_after`, so the posting is now given up. One statement, so two
    writers can't lose a count."""
    conn.execute(
        "INSERT INTO alert_refusals (posting_id, refusals, last_refused_at) VALUES (?, 1, ?) "
        "ON CONFLICT (posting_id) DO UPDATE SET refusals = refusals + 1, "
        "last_refused_at = excluded.last_refused_at",
        (posting_id, stamp),
    )
    cur = conn.execute(
        "UPDATE alert_refusals SET given_up_at = ? "
        "WHERE posting_id = ? AND given_up_at IS NULL AND refusals >= ?",
        (stamp, posting_id, give_up_after),
    )
    return cur.rowcount == 1


def refused_postings(conn: sqlite3.Connection) -> set[int]:
    """Postings Telegram refused that are not given up: they go behind the others."""
    rows = conn.execute("SELECT posting_id FROM alert_refusals WHERE given_up_at IS NULL")
    return {int(row["posting_id"]) for row in rows}


def clear_refusal(conn: sqlite3.Connection, posting_id: int) -> None:
    """Telegram accepted the posting: forget its refusals."""
    conn.execute("DELETE FROM alert_refusals WHERE posting_id = ?", (posting_id,))


def given_up_count(conn: sqlite3.Connection) -> int:
    """Postings the bot stopped sending after too many refusals."""
    row = conn.execute("SELECT count(*) FROM alert_refusals WHERE given_up_at IS NOT NULL")
    return int(row.fetchone()[0])


def bot_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_bot_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def alerts_held_since(conn: sqlite3.Connection) -> str | None:
    """When the bot started holding watcher alerts because config was unreadable."""
    row = conn.execute("SELECT value FROM bot_state WHERE key = 'alerts_held_since'").fetchone()
    return None if row is None else str(row["value"])


def set_alerts_held_since(conn: sqlite3.Connection, stamp: str | None) -> None:
    """Record when the hold started, or clear it with None."""
    if stamp is None:
        conn.execute("DELETE FROM bot_state WHERE key = 'alerts_held_since'")
    else:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES ('alerts_held_since', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (stamp,),
        )


def mark_alerted(conn: sqlite3.Connection, posting_id: int, alerted_at: str) -> None:
    """Record that Telegram accepted the alert. The caller sends first."""
    conn.execute(
        "UPDATE postings SET alerted_at = ? WHERE id = ?",
        (alerted_at, posting_id),
    )


# A posting the operator has dealt with: it has an application, or a skip they chose.
_HANDLED = (
    "(EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id) "
    "OR EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id AND s.by_operator = 1))"
)


def handled_title_keys(conn: sqlite3.Connection) -> dict[int, set[str]]:
    """The `filters.title_key` of every posting the operator logged or skipped, by company.

    A posting counts once it has an application (any source, a self-sourced one
    included) or a skip the operator chose (search migration 0016). A watcher skip does
    not count, so a changed config can still let a repost through.
    """
    keys: dict[int, set[str]] = {}
    rows = conn.execute(f"SELECT p.company_id, p.title FROM postings p WHERE {_HANDLED}")  # noqa: S608
    for row in rows:
        keys.setdefault(int(row["company_id"]), set()).add(title_key(str(row["title"])))
    return keys


def is_handled_repost(conn: sqlite3.Connection, company_id: int, title: str) -> bool:
    """Whether `title` at this company is a role the operator already logged or skipped
    (company plus `filters.title_key`), whatever its url or requisition id."""
    return title_key(title) in handled_title_keys(conn).get(company_id, set())


def unhandled_reposts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Watcher and discovery postings not alerted, with no skip and no application, that
    repeat a role the operator already logged or skipped. Oldest id first."""
    keys = handled_title_keys(conn)
    if not keys:
        return []
    rows = conn.execute(
        "SELECT p.id, p.company_id, c.name AS company, p.title FROM postings p "
        "JOIN companies c ON c.id = p.company_id "
        "WHERE p.source IN ('watcher', 'discovery') AND p.alerted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM skips s WHERE s.posting_id = p.id) "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id) "
        "ORDER BY p.id"
    ).fetchall()
    return [
        row
        for row in rows
        if title_key(str(row["title"])) in keys.get(int(row["company_id"]), set())
    ]


# A status that means the employer answered: screening and later, a rejection included.
RESPONSE_STATUSES = (
    "screening",
    "assessment_submitted",
    "interview_scheduled",
    "offer",
    "hired",
    "rejected",
)
# A status that ends an application one way or another.
OUTCOME_STATUSES = ("offer", "hired", "rejected", "no_response", "withdrawn")
WEEK_COUNTS = ("alerts", "applied", "skipped", "responses", "outcomes")


def _in(values: Sequence[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def weekly_counts(conn: sqlite3.Connection, start: str, end: str) -> dict[str, dict[str, int]]:
    """Counts by lane for the UTC window [start, end) (#43). Counts only.

    - alerts: postings Telegram accepted an alert for in the window;
    - applied: applications recorded in the window (their `applied` decision);
    - skipped: skips the operator chose in the window;
    - responses: applications whose first status of RESPONSE_STATUSES was recorded in
      the window, so one application counts once;
    - outcomes: statuses of OUTCOME_STATUSES recorded in the window.

    The lane is the posting's stored lane name, `Off-lane` once checked with none, or
    `No lane` when it was never checked (lanes off, or a self-sourced posting).
    """
    lane = (
        "CASE WHEN p.lane IS NOT NULL THEN p.lane "
        "WHEN p.lane_checked = 1 THEN 'Off-lane' ELSE 'No lane' END"
    )
    queries = {
        "alerts": f"SELECT {lane} AS lane, count(*) FROM postings p "  # noqa: S608
        "WHERE p.alerted_at >= ? AND p.alerted_at < ? GROUP BY 1",
        "applied": f"SELECT {lane} AS lane, count(*) FROM decision_history d "  # noqa: S608
        "JOIN applications a ON a.id = d.application_id JOIN postings p ON p.id = a.posting_id "
        "WHERE d.status = 'applied' AND d.recorded_at >= ? AND d.recorded_at < ? GROUP BY 1",
        "skipped": f"SELECT {lane} AS lane, count(*) FROM skips s "  # noqa: S608
        "JOIN postings p ON p.id = s.posting_id "
        "WHERE s.by_operator = 1 AND s.created_at >= ? AND s.created_at < ? GROUP BY 1",
        "responses": f"SELECT {lane} AS lane, count(*) FROM ("  # noqa: S608
        "SELECT d.application_id, min(d.recorded_at) AS first_at FROM decision_history d "
        f"WHERE d.status IN {_in(RESPONSE_STATUSES)} GROUP BY d.application_id) f "
        "JOIN applications a ON a.id = f.application_id JOIN postings p ON p.id = a.posting_id "
        "WHERE f.first_at >= ? AND f.first_at < ? GROUP BY 1",
        "outcomes": f"SELECT {lane} AS lane, count(*) FROM decision_history d "  # noqa: S608
        "JOIN applications a ON a.id = d.application_id JOIN postings p ON p.id = a.posting_id "
        f"WHERE d.status IN {_in(OUTCOME_STATUSES)} "
        "AND d.recorded_at >= ? AND d.recorded_at < ? GROUP BY 1",
    }
    counts: dict[str, dict[str, int]] = {}
    for name, sql in queries.items():
        for row in conn.execute(sql, (start, end)):
            lanes = counts.setdefault(str(row[0]), dict.fromkeys(WEEK_COUNTS, 0))
            lanes[name] = int(row[1])
    return counts


@dataclass(frozen=True)
class Example:
    """One posting the operator labelled, as the scoring prompt shows it (#43). `company`
    is kept only to leave the posting being scored out; it is not sent."""

    company: str
    title: str
    lane: str | None  # the lane name, "off-lane", or None when not checked
    gap_count: int | None
    label: str  # "skipped", or the application's latest status


def labelled_examples(conn: sqlite3.Connection, limit: int) -> list[Example]:
    """The `limit` most recently labelled postings, newest label first.

    Labels come only from the operator: an application (its latest status, `applied` or
    later, from the bot or the CLI) or a skip they chose with the Skipped button. The
    system never predicts one, and a watcher skip is not a label.
    """
    if limit < 1:
        return []
    rows = conn.execute(
        """
        WITH labels AS (
            SELECT a.posting_id, d.status AS label, d.recorded_at AS at, d.id AS seq, 1 AS kind
            FROM decision_history d JOIN applications a ON a.id = d.application_id
            UNION ALL
            SELECT s.posting_id, 'skipped', s.created_at, s.id, 0
            FROM skips s WHERE s.by_operator = 1
        ),
        latest AS (
            SELECT posting_id, label, at,
                   row_number() OVER (PARTITION BY posting_id ORDER BY at DESC, kind DESC,
                                      seq DESC) AS n
            FROM labels
        )
        SELECT c.name AS company, p.title, p.lane, p.lane_checked, l.label,
               CASE WHEN json_valid(p.gaps) AND json_type(p.gaps) = 'array'
                    THEN json_array_length(p.gaps) END AS gap_count
        FROM latest l
        JOIN postings p ON p.id = l.posting_id
        JOIN companies c ON c.id = p.company_id
        WHERE l.n = 1
        ORDER BY l.at DESC, p.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        Example(
            company=str(row["company"]),
            title=str(row["title"]),
            lane=str(row["lane"]) if row["lane"] else ("off-lane" if row["lane_checked"] else None),
            gap_count=None if row["gap_count"] is None else int(row["gap_count"]),
            label=str(row["label"]),
        )
        for row in rows
    ]


def insert_skip(
    conn: sqlite3.Connection,
    posting_id: int,
    date: str,
    reason: str,
    *,
    by_operator: bool = False,
) -> int:
    """A skip. `by_operator` marks one the operator chose (search migration 0016); only
    those suppress a repost of the same role. The watcher's own skips leave it False."""
    cur = conn.execute(
        "INSERT INTO skips (posting_id, date, reason, by_operator) VALUES (?, ?, ?, ?)",
        (posting_id, date, reason, int(by_operator)),
    )
    return int(cur.lastrowid or 0)


def claim_links(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Every application's correlation id and the claim it was recorded under."""
    return {
        r["correlation_id"]: r["ei_claim"]
        for r in conn.execute("SELECT correlation_id, ei_claim FROM applications")
    }


def decision_sequences(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str | None]]]:
    sequences: dict[str, list[tuple[str, str | None]]] = {c: [] for c in claim_links(conn)}
    for r in conn.execute(
        "SELECT a.correlation_id, d.status, d.status_date FROM decision_history d "
        "JOIN applications a ON a.id = d.application_id ORDER BY d.id"
    ):
        sequences[r["correlation_id"]].append((r["status"], r["status_date"]))
    return sequences


def posting_by_id(conn: sqlite3.Connection, posting_id: int) -> Posting | None:
    """Rebuild a Posting from search.db, so it can be recorded through the ledger."""
    row = conn.execute(
        "SELECT c.name AS company, p.* FROM postings p JOIN companies c ON c.id = p.company_id "
        "WHERE p.id = ?",
        (posting_id,),
    ).fetchone()
    if row is None:
        return None
    return Posting(
        company=row["company"],
        title=row["title"],
        first_seen=row["first_seen"],
        location=row["location"],
        work_model=row["work_model"],
        schedule=row["schedule"],
        url=row["url"],
        requisition_id=row["requisition_id"],
        source=row["source"],
        fit_rating=row["fit_rating"],
        gaps=row["gaps"],
        llm_output=row["llm_output"],
        scout_metadata=row["scout_metadata"],
        snapshot_path=row["snapshot_path"],
    )


def set_posting_snapshot(conn: sqlite3.Connection, posting_id: int, path: str) -> None:
    conn.execute("UPDATE postings SET snapshot_path = ? WHERE id = ?", (path, posting_id))


def fill_posting_snapshot(conn: sqlite3.Connection, posting_id: int, path: str) -> None:
    """Set the posting's snapshot only when it has none."""
    conn.execute(
        "UPDATE postings SET snapshot_path = ? WHERE id = ? AND snapshot_path IS NULL",
        (path, posting_id),
    )


def _blob(value: object) -> bytes | None:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    return None


def resume_embedding(conn: sqlite3.Connection) -> tuple[str, int, bytes] | None:
    """The one stored resume vector: sha256, dimension, float32 bytes. No resume text."""
    row = conn.execute(
        "SELECT content_sha256, dim, vector FROM resume_embedding WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    blob = _blob(row["vector"])
    if blob is None:
        return None
    return str(row["content_sha256"]), int(row["dim"]), blob


def save_resume_embedding(
    conn: sqlite3.Connection,
    content_sha256: str,
    dim: int,
    vector: bytes,
    embedded_at: str,
) -> None:
    conn.execute(
        "INSERT INTO resume_embedding (id, content_sha256, dim, vector, embedded_at) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "content_sha256 = excluded.content_sha256, dim = excluded.dim, "
        "vector = excluded.vector, embedded_at = excluded.embedded_at",
        (content_sha256, dim, vector, embedded_at),
    )


def posting_embedding(conn: sqlite3.Connection, posting_id: int) -> tuple[int, bytes] | None:
    row = conn.execute(
        "SELECT dim, vector FROM posting_embeddings WHERE posting_id = ?",
        (posting_id,),
    ).fetchone()
    if row is None:
        return None
    blob = _blob(row["vector"])
    if blob is None:
        return None
    return int(row["dim"]), blob


def posting_embeddings(conn: sqlite3.Connection) -> list[tuple[int, int, bytes]]:
    found: list[tuple[int, int, bytes]] = []
    for row in conn.execute(
        "SELECT posting_id, dim, vector FROM posting_embeddings ORDER BY posting_id"
    ):
        blob = _blob(row["vector"])
        if blob is None:
            continue
        found.append((int(row["posting_id"]), int(row["dim"]), blob))
    return found


def save_posting_embedding(
    conn: sqlite3.Connection,
    posting_id: int,
    dim: int,
    vector: bytes,
    embedded_at: str,
) -> None:
    conn.execute(
        "INSERT INTO posting_embeddings (posting_id, dim, vector, embedded_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(posting_id) DO UPDATE SET "
        "dim = excluded.dim, vector = excluded.vector, embedded_at = excluded.embedded_at",
        (posting_id, dim, vector, embedded_at),
    )


def set_embedding_score(conn: sqlite3.Connection, posting_id: int, score: float | None) -> None:
    """The cosine beside the LLM tier, or null. Does not set fit_rating."""
    conn.execute(
        "UPDATE postings SET embedding_score = ? WHERE id = ?",
        (score, posting_id),
    )


def recent_applications(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """The latest applications, newest first, with company, title and current status."""
    rows: list[sqlite3.Row] = conn.execute(
        """
        SELECT a.id, a.correlation_id, a.ei_claim, c.name AS company, p.title, p.url,
               (SELECT d.status FROM decision_history d WHERE d.application_id = a.id
                ORDER BY d.id DESC LIMIT 1) AS status
        FROM applications a
        JOIN postings p ON p.id = a.posting_id
        JOIN companies c ON c.id = p.company_id
        ORDER BY a.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows


def application_summary(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT a.id, a.correlation_id, a.ei_claim, a.posting_id, c.name AS company, p.title "
        "FROM applications a JOIN postings p ON p.id = a.posting_id "
        "JOIN companies c ON c.id = p.company_id WHERE a.id = ?",
        (application_id,),
    ).fetchone()
    return row
