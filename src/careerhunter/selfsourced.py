"""A self-sourced application in one step (#34): `careerhunter log` and the bot's `/log`.

Both front ends do the same thing. The link is fetched once (`fetch.py`, ADR-016) for
the company, the title and the location, unless both of the first two are given. Then
one call to `ledger.record_application` writes the posting (`source` self_sourced, or
the posting already stored under that link) and the application to both layers under
one correlation id. Nothing is written before that call, apart from the job-ad snapshot
file, which is taken first so its path goes into both layers in the same write
(ADR-017). The date is checked against the active claim before anything is fetched or
written.

The link is stored in its canonical form (`urls.canonical`). A link already stored for
a posting is matched canonically, and the posting keeps the form it was stored in. A
LinkedIn link is never fetched (`urls.no_fetch`), so the company and title must be
given with it (`LinkedInNamesMissing`).

The job description (ADR-024) is the caller's when it was pasted or sent as a file;
otherwise it is fetched from the page or the ATS's JSON when the host allows it. Its
snapshot file is saved just before the ledger call, like the job-ad snapshot, and goes
to both layers in the same write. An application with no JD is still recorded, and
flagged: both layers say a JD was expected.

When the same role was logged before (the repost match: the same companies row and
`filters.title_key`), the application is still recorded, and the reply gains one line
with the earlier application's date and nothing else.

Every failure is a `LogError` with a fixed, user-facing message that carries no path
and no page content. Config is the caller's to read: without it there is no date and
no claim check, so both front ends refuse before calling here.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from careerhunter import enums, jobdesc, ledger, search_store, snapshots, urls
from careerhunter.claims import ActiveClaim, Verdict, check_dating
from careerhunter.fetch import FetchError
from careerhunter.filters import title_key
from careerhunter.jobdesc import JobDescription
from careerhunter.models import Application, Dating, Posting
from careerhunter.search_store import IdentityConflict
from careerhunter.snapshots import Fetcher

MAX_URL = 2000

# The words the bot's method buttons show, and a few plain spellings, for each method.
METHOD_WORDS = {
    "company site": "company_site",
    "company": "company_site",
    "linkedin": "linkedin_easy_apply",
    "linkedin easy apply": "linkedin_easy_apply",
    "email": "email",
    "referral": "referral",
    "agency": "agency",
    "recruiter": "recruiter_inbound",
    "recruiter inbound": "recruiter_inbound",
    "other": "other",
}


class LogError(Exception):
    """The application was not recorded. The message is user-facing and fixed."""


class NamesMissing(LogError):
    """The page shows no company and title, and none were given; each front end says how
    to give them."""


class LinkedInNamesMissing(NamesMissing):
    """A LinkedIn link, which is never fetched, came without the company and title."""


@dataclass(frozen=True)
class Logged:
    company: str
    title: str
    correlation_id: str
    ei_claim: str | None
    snapshot_saved: bool
    earlier: str | None = None  # the repeat line, when this role was logged before
    jd_saved: bool = False  # a JD with text is in the record
    jd_file_without_text: bool = False  # a file was kept as the ad, but had no text


def earlier_line(search: sqlite3.Connection, company: str, title: str) -> str | None:
    """`You logged an application to this role on <date>.` when an application already
    exists for the same role: the same companies row and the same `filters.title_key`,
    the repost match (#34). The latest earlier date is named, and nothing else."""
    company_row = search.execute("SELECT id FROM companies WHERE name = ?", (company,)).fetchone()
    if company_row is None:
        return None
    key = title_key(title)
    rows = search.execute(
        "SELECT p.title, a.date_precision, a.date_applied, a.week_of FROM applications a "
        "JOIN postings p ON p.id = a.posting_id WHERE p.company_id = ? "
        "ORDER BY coalesce(a.date_applied, a.week_of) IS NULL, "
        "coalesce(a.date_applied, a.week_of) DESC, a.id DESC",
        (int(company_row["id"]),),
    )
    for row in rows:
        if title_key(str(row["title"])) != key:
            continue
        if row["date_precision"] == "exact":
            return f"You logged an application to this role on {row['date_applied']}."
        if row["date_precision"] == "week":
            return f"You logged an application to this role in the week of {row['week_of']}."
        return "You logged an application to this role before, on a date not known."
    return None


def parse_method(text: str) -> str:
    """A method code (`company_site`) or its plain words (`Company site`), as the code."""
    folded = " ".join(text.replace("_", " ").casefold().split())
    code = folded.replace(" ", "_")
    if code in enums.METHODS:
        return code
    if folded in METHOD_WORDS:
        return METHOD_WORDS[folded]
    raise LogError("The method must be one of: " + ", ".join(enums.METHODS) + ".")


def parse_day(text: str, today: dt.date) -> Dating:
    """`today`, `yesterday` or a YYYY-MM-DD date not after today, as an exact date."""
    word = text.strip().casefold()
    if word == "today":
        return Dating("exact", day=today.isoformat())
    if word == "yesterday":
        return Dating("exact", day=(today - dt.timedelta(days=1)).isoformat())
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", word):
        raise LogError("Send the date as YYYY-MM-DD, today or yesterday.")
    try:
        day = dt.date.fromisoformat(word)
    except ValueError:
        raise LogError(f"{word} isn't a real date.") from None
    if day > today:
        raise LogError("That date is in the future, so it can't be recorded.")
    return Dating("exact", day=day.isoformat())


def describe(
    url: str,
    fetcher: Fetcher,
    today: dt.date,
    *,
    company: str | None = None,
    title: str | None = None,
) -> Posting:
    """The posting behind `url`: the page's company, title and location, unless the
    company and title are given. A page that can't be read is a refusal only when they
    are not given."""
    if not url or len(url) > MAX_URL:
        raise LogError("That link is empty or too long.")
    url = urls.canonical(url)
    info = snapshots.PostingInfo()
    if urls.no_fetch(url):
        if not (company and title):
            raise LinkedInNamesMissing(
                "LinkedIn links are never fetched, so the company and title must be given."
            )
    else:
        try:
            page = fetcher(url)
            info = snapshots.posting_info(page.body)
        except FetchError as exc:
            if not (company and title):
                raise LogError(f"I couldn't read that link: {exc}.") from None
    found_company = company or info.company
    found_title = title or info.title
    if not found_company or not found_title:
        raise NamesMissing("I couldn't find the company and title on that page.")
    return Posting(
        company=found_company,
        title=found_title,
        first_seen=today.isoformat(),
        location=info.location or "",
        url=url,
        source="self_sourced",
    )


def record(
    data_dir: Path,
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    posting: Posting,
    method: str,
    dating: Dating,
    today: dt.date,
    fetcher: Fetcher,
    jd: JobDescription | None = None,
    notes: str | None = None,
    lane_config: Mapping[str, Any] | None = None,
) -> Logged:
    """Record the application, and the posting with it, in one ledger call.

    `jd` is a pasted or uploaded JD; without one, it is fetched when the host allows.
    `notes` go to search.db only (the import's notes). `lane_config` (config.yaml)
    checks the posting against `search.lanes`, as the watcher would.
    """
    if method not in enums.METHODS:
        raise LogError("That method isn't recognized.")
    if active is not None and check_dating(active.claim, dating)[0] is Verdict.REFUSE:
        raise LogError("Not recorded: that date is outside the active EI claim.")
    earlier = earlier_line(search, posting.company, posting.title)  # before this one exists
    stored = _stored(search, posting.url)
    if stored is not None:
        posting = replace(posting, url=stored[2])  # the form it was stored in
    path = stored[1] if stored else None
    fetchable = posting.url is not None and not urls.no_fetch(posting.url)
    if path is None and posting.url and fetchable:
        captured = snapshots.capture(posting.url, fetcher)
        if captured is not None:
            path = snapshots.save(data_dir, captured.data, captured.ext, today)
    if jd is None and posting.url and fetchable:
        jd = jobdesc.fetched(posting.url, fetcher)
    saved = jobdesc.save(data_dir, jd, today) if jd is not None else None
    app = Application(replace(posting, snapshot_path=path), dating, method, notes=notes)
    try:
        recorded = ledger.record_application(search, active, app, saved, lane_config=lane_config)
    except IdentityConflict:
        raise LogError("That link is already recorded for another company.") from None
    except sqlite3.IntegrityError:
        raise LogError("That application is already recorded.") from None
    posting_id = int(
        search.execute(
            "SELECT posting_id FROM applications WHERE correlation_id = ?",
            (recorded.correlation_id,),
        ).fetchone()[0]
    )
    shown = search_store.posting_by_id(search, posting_id)
    if shown is not None and path is not None and shown.snapshot_path is None:
        search_store.set_posting_snapshot(search, posting_id, path)  # a posting stored before
    return Logged(
        company=shown.company if shown else posting.company,
        title=shown.title if shown else posting.title,
        correlation_id=recorded.correlation_id,
        ei_claim=recorded.ei_claim,
        snapshot_saved=path is not None or saved is not None,
        earlier=earlier,
        jd_saved=saved is not None and saved.has_text,
        jd_file_without_text=saved is not None and not saved.has_text,
    )


def _stored(search: sqlite3.Connection, url: str | None) -> tuple[int, str | None, str] | None:
    """The posting already stored under this link, compared canonically: its id, its
    snapshot path, if any, and the link as it was stored."""
    if url is None:
        return None
    found = [(i, u) for i, u in search_store.posting_urls(search) if u == url]
    if not found:
        wanted = urls.canonical(url)
        found = [
            (i, u) for i, u in search_store.posting_urls(search) if urls.canonical(u) == wanted
        ]
    if not found:
        return None
    posting_id, stored = found[0]
    row = search.execute(
        "SELECT snapshot_path FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()
    snapshot = None if row["snapshot_path"] is None else str(row["snapshot_path"])
    return posting_id, snapshot, stored
