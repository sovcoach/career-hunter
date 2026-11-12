"""`careerhunter import`: backfill earlier applications and their job descriptions.

The input is a JSON file (version 1, documented in import.example.json at the repo
root and in docs/SETUP.md). It must sit under `imports/` in the data folder, with
symlinks followed, and so must every `jd_file` it names (resolved against the JSON
file's folder). Any other path is refused (`ImportPathError`).

The file is validated strictly, by hand. A problem with the file as a whole
(`ImportFileError`) stops the run; a problem with one entry refuses that entry, and
every problem is listed at once, like `seed`. Then each entry gets one class against
search.db, printed in capitals:

- create: nothing matches. It goes through `selfsourced.record`, the `/log` path, with
  a fetcher that refuses everything, so the import never fetches. The link is stored
  in its canonical form, and the JD is saved as `/log` saves it (ADR-024).
- attach: exactly one application has the same `titles.employer_key` and
  `titles.canonical` title, or several do and exactly one has the same dating. The JD,
  and the link when the application has none, go through
  `ledger.attach_job_description`. The application's date never changes.
  `jd_unavailable` (instead of `jd_text` or `jd_file`) marks the JD unavailable, with
  why, through `ledger.mark_jd_unavailable`, unless the application has a JD or the same
  explanation already; a create is marked the same way once recorded.
- unchanged: the matched application already has this JD (or no JD is given), and its
  link is the same, or empty with none given. A `"new"` entry already recorded (the
  posting and date natural key) is unchanged too.
- ambiguous: several candidates and no single one with the same dating; titles that
  are only `titles.equivalent`; two entries for one application. Listed with each
  candidate, and skipped. `"resolve": <search application id>` or `"new"` settles it.
- conflict: the application has a different link, compared canonically, or a different
  current JD. Listed, and nothing is changed.
- refused: a bad field, a date in the future, a date outside the active claim (unless
  `route_out_of_claim`, which records it in search.db only, as `seed` does), or an
  application whose claim record can't change.

`plan` reads only, so `--dry-run` writes nothing, not even a snapshot file, and an
applying run does exactly what the same plan says. Notes go to search.db only, with a
new application; an existing application's notes are left as they are. No SQL here:
every read and write goes through the store modules and the ledger.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from careerhunter import ei_store, enums, jobdesc, ledger, search_store, selfsourced, titles, urls
from careerhunter.claims import ActiveClaim, Claim, Verdict, check_dating
from careerhunter.fetch import Fetched, FetchRefused
from careerhunter.jobdesc import JobDescError, JobDescription
from careerhunter.models import Dating, Posting
from careerhunter.search_store import StoredApplication
from careerhunter.storage_guard import DataDir

VERSION = 1
MAX_NAME = 200
MAX_URL = 2000
MAX_NOTES = 2000
PRECISIONS = ("day", "week")

CREATE = "create"
ATTACHING = "attach"  # printed as the class name in capitals
UNCHANGED = "unchanged"
AMBIGUOUS = "ambiguous"
CONFLICT = "conflict"
REFUSED = "refused"
DONE = (CREATE, ATTACHING, UNCHANGED)  # classes that leave nothing for the operator to do

_KEYS = frozenset(
    {
        "employer",
        "role",
        "location",
        "work_model",
        "date",
        "date_precision",
        "method",
        "url",
        "jd_text",
        "jd_file",
        "jd_unavailable",
        "notes",
        "resolve",
    }
)
_REQUIRED = ("employer", "role", "date", "date_precision", "method")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class ImportPathError(Exception):
    """The import file is not under imports/ in the data folder. User-facing."""


class ImportFileError(Exception):
    """The file as a whole is not a version 1 import file; nothing was done."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"import file has {len(problems)} problem(s):\n{detail}")


@dataclass(frozen=True)
class Entry:
    """One valid entry, normalized: the link canonical, a week as its Monday."""

    number: int  # 1-based, as the dry run shows it
    employer: str
    role: str
    location: str
    work_model: str | None
    dating: Dating
    method: str
    url: str | None
    jd: JobDescription | None = field(repr=False)
    notes: str | None = field(repr=False)
    resolve: int | str | None
    jd_unavailable: str | None = None  # why the JD can't be kept (EI migration 0003)

    @property
    def posting(self) -> Posting:
        return Posting(
            company=self.employer,
            title=self.role,
            first_seen=self.dating.sort_date or "",
            location=self.location,
            work_model=self.work_model,
            url=self.url,
            source="self_sourced",
        )


@dataclass(frozen=True)
class Parsed:
    """One entry of the file: valid (`entry`), or refused with its `problems`."""

    number: int
    entry: Entry | None
    problems: tuple[str, ...] = ()
    label: str = ""  # employer / role, as given, for a refused entry


@dataclass(frozen=True)
class Planned:
    """What the import does with one entry. `apply` does exactly this."""

    number: int
    kind: str
    entry: Entry | None
    target: StoredApplication | None = None  # the matched application
    candidates: tuple[StoredApplication, ...] = ()  # for AMBIGUOUS
    reasons: tuple[str, ...] = ()
    to_ei: bool = False  # CREATE: the active claim's record gets it too
    set_url: bool = False  # ATTACHING: the empty link is filled
    add_jd: bool = False  # ATTACHING: the JD becomes the current one
    set_snapshot: bool = False  # ATTACHING: a text-less JD file becomes the job-ad copy
    mark_unavailable: bool = False  # ATTACHING: the JD is marked unavailable, with why
    warning: str = ""
    label: str = ""


@dataclass(frozen=True)
class Outcome:
    number: int
    kind: str
    detail: str = ""


# --- The file -------------------------------------------------------------------------


def _within(path: Path, base: Path) -> bool:
    return base in path.parents


def import_file(datadir: DataDir, given: Path) -> Path:
    """The import file, resolved, or ImportPathError unless it is under imports/."""
    base = datadir.imports
    if not base.is_dir():
        raise ImportPathError(
            "there is no imports/ folder in the data folder; run careerhunter init"
        )
    try:
        resolved = given.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ImportPathError("the import file was not found") from None
    if not _within(resolved, base.resolve()):
        raise ImportPathError("the import file must be under imports/ in the data folder")
    if not resolved.is_file():
        raise ImportPathError("the import file is not a file")
    return resolved


def _jd_file(name: str, json_path: Path, base: Path, problems: list[str]) -> JobDescription | None:
    where = "jd_file"
    try:
        resolved = (json_path.parent / name).resolve(strict=True)
    except (OSError, RuntimeError):
        problems.append(f"{where} was not found")
        return None
    if not _within(resolved, base.resolve()) or not resolved.is_file():
        problems.append(f"{where} must be a file under imports/ in the data folder")
        return None
    try:
        with resolved.open("rb") as fh:
            data = fh.read(jobdesc.MAX_FILE_BYTES + 1)
    except OSError:
        problems.append(f"{where} could not be read")
        return None
    try:
        return jobdesc.from_file(data)
    except JobDescError as exc:
        problems.append(f"{where}: {exc}")
        return None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ImportFileError([f"the key {key!r} appears twice in one object"])
        seen[key] = value
    return seen


def _no_constant(name: str) -> Any:
    raise ImportFileError([f"{name} is not allowed in an import file"])


def read(path: Path) -> Any:
    """The JSON document, strictly: no duplicate keys, no NaN or Infinity."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ImportFileError(["the file is not UTF-8 text"]) from None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise ImportFileError([f"not valid JSON: {exc.msg} (line {exc.lineno})"]) from None


def _string(
    item: dict[str, Any], key: str, problems: list[str], *, low: int = 0, high: int
) -> str | None:
    value = item.get(key)
    if not isinstance(value, str):
        problems.append(f"{key} must be text")
        return None
    if len(value) < low or len(value) > high:
        problems.append(f"{key} must be {low} to {high} characters")
        return None
    return value


def _name(item: dict[str, Any], key: str, problems: list[str]) -> str:
    value = _string(item, key, problems, low=1, high=MAX_NAME)
    if value is not None and not value.strip():
        problems.append(f"{key} is blank")
        return ""
    return (value or "").strip()


def _dating(item: dict[str, Any], today: dt.date, problems: list[str]) -> Dating | None:
    value = item.get("date")
    precision = item.get("date_precision")
    if precision not in PRECISIONS:
        problems.append(f"date_precision must be one of {list(PRECISIONS)}")
    if not isinstance(value, str) or not _DATE.match(value):
        problems.append("date must be a YYYY-MM-DD date")
        return None
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        problems.append(f"date {value} is not a real date")
        return None
    if day > today:
        problems.append(f"date {value} is in the future")
        return None
    if precision == "day":
        return Dating("exact", day=day.isoformat())
    if precision == "week":
        return Dating("week", week_of=(day - dt.timedelta(days=day.weekday())).isoformat())
    return None


def _url(item: dict[str, Any], problems: list[str]) -> str | None:
    value = item.get("url")
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL:
        problems.append(f"url must be text of 1 to {MAX_URL} characters, or null")
        return None
    try:
        parts = urllib.parse.urlsplit(value.strip())
        host = parts.hostname
    except ValueError:
        host = None
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not host:
        problems.append("url must be an http or https link")
        return None
    return urls.canonical(value)


def _entry(number: int, item: Any, json_path: Path, base: Path, today: dt.date) -> Parsed:
    if not isinstance(item, dict):
        return Parsed(number, None, ("the entry must be an object",))
    problems: list[str] = []
    unknown = sorted(set(item) - _KEYS)
    if unknown:
        problems.append(f"unknown keys: {unknown}")
    problems.extend(f"{key} is required" for key in _REQUIRED if key not in item)
    employer = _name(item, "employer", problems) if "employer" in item else ""
    role = _name(item, "role", problems) if "role" in item else ""
    label = f"{employer or '?'} / {role or '?'}"
    location = ""
    if "location" in item:
        location = (_string(item, "location", problems, high=MAX_NAME) or "").strip()
    work_model = item.get("work_model")
    if work_model is not None and work_model not in enums.WORK_MODELS:
        problems.append(f"work_model must be one of {[*enums.WORK_MODELS, None]}")
    method = item.get("method")
    if "method" in item and method not in enums.METHODS:
        problems.append(f"method must be one of {list(enums.METHODS)}")
    dating = _dating(item, today, problems) if "date" in item else None
    url = _url(item, problems)
    jd: JobDescription | None = None
    unavailable: str | None = None
    if sum(key in item for key in ("jd_text", "jd_file", "jd_unavailable")) > 1:
        problems.append("give one of jd_text, jd_file and jd_unavailable, not more")
    elif "jd_unavailable" in item:
        try:
            unavailable = jobdesc.unavailable_explanation(item["jd_unavailable"])
        except JobDescError as exc:
            problems.append(f"jd_unavailable: {exc}")
    elif "jd_text" in item:
        text = _string(item, "jd_text", problems, low=1, high=jobdesc.MAX_CHARS)
        if text is not None:
            try:
                jd = jobdesc.from_paste(text)
            except JobDescError as exc:
                problems.append(f"jd_text: {exc}")
    elif "jd_file" in item:
        name = _string(item, "jd_file", problems, low=1, high=4096)
        if name is not None:
            jd = _jd_file(name, json_path, base, problems)
    notes = item.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > MAX_NOTES):
        problems.append(f"notes must be text of at most {MAX_NOTES} characters, or null")
    resolve = item.get("resolve")
    if "resolve" in item and not (resolve == "new" or (type(resolve) is int and resolve >= 1)):
        problems.append('resolve must be a search application id (1 or more) or "new"')
    if problems or dating is None:
        return Parsed(number, None, tuple(problems), label)
    return Parsed(
        number,
        Entry(
            number=number,
            employer=employer,
            role=role,
            location=location,
            work_model=work_model,
            dating=dating,
            method=str(method),
            url=url,
            jd=jd,
            notes=notes.strip() or None if isinstance(notes, str) else None,
            resolve=resolve,
            jd_unavailable=unavailable,
        ),
        label=label,
    )


def parse(doc: Any, json_path: Path, base: Path, today: dt.date) -> list[Parsed]:
    """Every entry, valid or refused. A bad file as a whole is an ImportFileError."""
    if not isinstance(doc, dict):
        raise ImportFileError(["the top level must be an object with version and applications"])
    problems: list[str] = []
    unknown = sorted(set(doc) - {"version", "applications"})
    if unknown:
        problems.append(f"unknown top-level keys: {unknown}")
    version = doc.get("version")
    if type(version) is not int or version != VERSION:
        problems.append(f"version must be {VERSION}")
    raw = doc.get("applications")
    items: list[Any] = raw if isinstance(raw, list) else []
    if not items:
        problems.append("applications must be a list of at least one entry")
    if problems:
        raise ImportFileError(problems)
    return [_entry(i, item, json_path, base, today) for i, item in enumerate(items, start=1)]


def load(datadir: DataDir, given: Path, today: dt.date) -> list[Parsed]:
    """Read and validate the import file. Reads only."""
    path = import_file(datadir, given)
    return parse(read(path), path, datadir.imports, today)


# --- The plan -------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """The active claim as the plan sees it: its dates, and its file when it exists."""

    claim: Claim | None
    conn: sqlite3.Connection | None = None


def _same_dating(a: Dating, b: Dating) -> bool:
    return (a.precision, a.day, a.week_of) == (b.precision, b.day, b.week_of)


def _canonical(url: str | None) -> str | None:
    return urls.canonical(url) if url else None


def _url_owner(search: sqlite3.Connection, url: str, posting_id: int | None) -> int | None:
    """The posting, other than `posting_id`, that has this link, compared canonically."""
    for other, stored in search_store.posting_urls(search):
        if other != posting_id and urls.canonical(stored) == url:
            return other
    return None


def _natural(search: sqlite3.Connection, entry: Entry) -> tuple[str, StoredApplication | None]:
    """Where a CREATE would land: ("ok", None), ("recorded", the application) when the
    posting and date are already recorded, or ("other company", None)."""
    posting_id: int | None = None
    if entry.url is not None:
        posting_id = _url_owner(search, entry.url, None)
        if posting_id is not None and search_store.posting_company(search, posting_id) != (
            entry.employer
        ):
            return "other company", None
    else:
        company = search_store.company_id(search, entry.employer)
        if company is not None:
            posting_id = search_store.find_posting(search, company, entry.posting, entry.dating)
    if posting_id is None:
        return "ok", None
    found = search_store.find_application(search, posting_id, entry.dating)
    if found is None:
        return "ok", None
    return "recorded", search_store.stored_application(search, int(found["id"]))


def _create(search: sqlite3.Connection, record: Record, entry: Entry, route: bool) -> Planned:
    where, existing = _natural(search, entry)
    if where == "other company":
        return Planned(
            entry.number, CONFLICT, entry, reasons=("that link is recorded for another company",)
        )
    if existing is not None:
        if entry.resolve == "new":
            return Planned(
                entry.number, UNCHANGED, entry, target=existing, reasons=("already recorded",)
            )
        return Planned(
            entry.number,
            AMBIGUOUS,
            entry,
            candidates=(existing,),
            reasons=("its link and date are already recorded under another title",),
        )
    claim = record.claim
    if claim is None:
        return Planned(entry.number, CREATE, entry, to_ei=False)
    verdict, message = check_dating(claim, entry.dating)
    if verdict is Verdict.REFUSE:
        if route:
            return Planned(entry.number, CREATE, entry, to_ei=False, warning=message)
        return Planned(
            entry.number,
            REFUSED,
            entry,
            reasons=(
                f"{message}. Fix the date, or re-run with --route-out-of-claim-to-search "
                "to record it in search.db only",
            ),
        )
    return Planned(entry.number, CREATE, entry, to_ei=True, warning=message)


def _ei_fields(record: Record, target: StoredApplication) -> sqlite3.Row | None:
    if record.conn is None or record.claim is None or target.ei_claim != record.claim.label:
        return None
    return ei_store.application_fields(record.conn, target.correlation_id)


def _against(
    search: sqlite3.Connection, record: Record, entry: Entry, target: StoredApplication
) -> Planned:
    """ATTACHING, UNCHANGED, CONFLICT or REFUSED for an entry matched to `target`."""
    ei_row = _ei_fields(record, target)
    links = [u for u in (target.url, ei_row["posting_url"] if ei_row else None) if u]
    if entry.url is not None and any(urls.canonical(u) != entry.url for u in links):
        return Planned(
            entry.number,
            CONFLICT,
            entry,
            target=target,
            reasons=("the application has a different link",),
        )
    jd = entry.jd
    sha = jd.text_sha256 if jd is not None else None
    if sha is not None and target.jd_sha256 is not None and sha != target.jd_sha256:
        return Planned(
            entry.number,
            CONFLICT,
            entry,
            target=target,
            reasons=("the application has a different job description",),
        )
    set_url = entry.url is not None and (
        target.url is None or (ei_row is not None and ei_row["posting_url"] is None)
    )
    add_jd = sha is not None and target.jd_sha256 is None
    set_snapshot = (
        jd is not None
        and sha is None
        and (
            target.snapshot_path is None or (ei_row is not None and ei_row["snapshot_path"] is None)
        )
    )
    # A JD the application already has stands; a marker would change nothing shown.
    mark_unavailable = (
        entry.jd_unavailable is not None
        and target.jd_sha256 is None
        and target.jd_unavailable != entry.jd_unavailable
    )
    if not (set_url or add_jd or set_snapshot or mark_unavailable):
        return Planned(entry.number, UNCHANGED, entry, target=target)
    if (
        entry.url is not None
        and target.url is None
        and _url_owner(search, entry.url, target.posting_id) is not None
    ):
        return Planned(
            entry.number,
            CONFLICT,
            entry,
            target=target,
            reasons=("that link is recorded for another posting",),
        )
    refusal = _locked(record, target, ei_row)
    if refusal:
        return Planned(entry.number, REFUSED, entry, target=target, reasons=(refusal,))
    return Planned(
        entry.number,
        ATTACHING,
        entry,
        target=target,
        set_url=set_url,
        add_jd=add_jd,
        set_snapshot=set_snapshot,
        mark_unavailable=mark_unavailable,
    )


def _locked(record: Record, target: StoredApplication, ei_row: sqlite3.Row | None) -> str:
    """Why the target's record can't change, or ""."""
    if target.ei_claim is None:
        return ""
    if record.claim is None or record.claim.label != target.ei_claim:
        return (
            f"the application is in the record for claim {target.ei_claim}, which is not the "
            "active claim, so its record can't change"
        )
    if record.conn is None or ei_row is None:
        return (
            f"the application is missing from the record for claim {target.ei_claim}; "
            "run careerhunter doctor"
        )
    if int(ei_row["voided"]):
        return "the application was voided, so it can't change"
    return ""


def _classify(
    search: sqlite3.Connection,
    record: Record,
    entry: Entry,
    stored: list[StoredApplication],
    route: bool,
) -> Planned:
    if entry.resolve == "new":
        return _create(search, record, entry, route)
    if isinstance(entry.resolve, int):
        target = next((s for s in stored if s.application_id == entry.resolve), None)
        if target is None:
            return Planned(
                entry.number,
                REFUSED,
                entry,
                reasons=(f"resolve: there is no application {entry.resolve} in search.db",),
            )
        return _against(search, record, entry, target)
    employer = titles.employer_key(entry.employer)
    role = titles.canonical(entry.role)
    same_employer = [s for s in stored if titles.employer_key(s.company) == employer]
    candidates = [s for s in same_employer if titles.canonical(s.title) == role]
    if len(candidates) > 1:
        dated = [s for s in candidates if _same_dating(s.dating, entry.dating)]
        if len(dated) != 1:
            return Planned(
                entry.number,
                AMBIGUOUS,
                entry,
                candidates=tuple(candidates),
                reasons=("several applications match, and no single one has the same date",),
            )
        candidates = dated
    if candidates:
        return _against(search, record, entry, candidates[0])
    near = [s for s in same_employer if titles.equivalent(s.title, entry.role)]
    if near:
        return Planned(
            entry.number,
            AMBIGUOUS,
            entry,
            candidates=tuple(near),
            reasons=("the titles are only equivalent",),
        )
    return _create(search, record, entry, route)


_TARGETED = (ATTACHING, UNCHANGED, CONFLICT)


def _one_per_application(planned: list[Planned]) -> list[Planned]:
    """Two entries for one application are both AMBIGUOUS, and so are two CREATEs of
    the same employer, title and date."""
    by_target: dict[int, list[int]] = {}
    by_key: dict[tuple[str, str, str, str | None, str | None], list[int]] = {}
    for i, item in enumerate(planned):
        if item.kind in _TARGETED and item.target is not None:
            by_target.setdefault(item.target.application_id, []).append(i)
        elif item.kind == CREATE and item.entry is not None:
            e = item.entry
            key = (
                titles.employer_key(e.employer),
                titles.canonical(e.role),
                e.dating.precision,
                e.dating.day,
                e.dating.week_of,
            )
            by_key.setdefault(key, []).append(i)
    out = list(planned)
    for target_id, indexes in by_target.items():
        if len(indexes) > 1:
            numbers = ", ".join(str(planned[i].number) for i in indexes)
            for i in indexes:
                item = planned[i]
                out[i] = Planned(
                    item.number,
                    AMBIGUOUS,
                    item.entry,
                    candidates=() if item.target is None else (item.target,),
                    reasons=(f"entries {numbers} all match application {target_id}",),
                )
    for indexes in by_key.values():
        if len(indexes) > 1:
            numbers = ", ".join(str(planned[i].number) for i in indexes)
            for i in indexes:
                item = planned[i]
                out[i] = Planned(
                    item.number,
                    AMBIGUOUS,
                    item.entry,
                    reasons=(f"entries {numbers} are the same new application",),
                )
    return out


def plan(
    search: sqlite3.Connection,
    record: Record,
    parsed: list[Parsed],
    *,
    route_out_of_claim: bool = False,
) -> list[Planned]:
    """What the import would do with each entry. Reads only."""
    stored = search_store.stored_applications(search)
    planned: list[Planned] = []
    for item in parsed:
        if item.entry is None:
            planned.append(
                Planned(item.number, REFUSED, None, reasons=item.problems, label=item.label)
            )
            continue
        planned.append(_classify(search, record, item.entry, stored, route_out_of_claim))
    return _one_per_application(planned)


# --- Applying -------------------------------------------------------------------------


def refuse_fetch(url: str) -> Fetched:
    """The import's fetcher: it never fetches anything."""
    raise FetchRefused("the import never fetches")


def apply(
    data_dir: Path,
    search: sqlite3.Connection,
    active: ActiveClaim | None,
    planned: list[Planned],
    today: dt.date,
    lane_config: Mapping[str, Any] | None = None,
) -> list[Outcome]:
    """Write exactly what `planned` says, one entry at a time, each in its own ledger
    call. Only create and attach write anything. A created posting is checked against
    `search.lanes` in `lane_config` (config.yaml), as the watcher would."""
    outcomes: list[Outcome] = []
    for item in planned:
        entry = item.entry
        target = item.target
        if entry is None or not (item.kind == CREATE or (item.kind == ATTACHING and target)):
            outcomes.append(Outcome(item.number, item.kind))
            continue
        try:
            if item.kind == CREATE:
                logged = selfsourced.record(
                    data_dir,
                    search,
                    active if item.to_ei else None,
                    entry.posting,
                    entry.method,
                    entry.dating,
                    today,
                    refuse_fetch,
                    entry.jd,
                    notes=entry.notes,
                    lane_config=lane_config,
                )
                if entry.jd_unavailable is not None:
                    ledger.mark_jd_unavailable(
                        search,
                        active if item.to_ei else None,
                        logged.correlation_id,
                        entry.jd_unavailable,
                    )
                outcomes.append(Outcome(item.number, CREATE, logged.correlation_id))
            elif target is not None:
                saved = None
                if entry.jd is not None and (item.add_jd or item.set_snapshot):
                    saved = jobdesc.save(data_dir, entry.jd, today)
                if item.set_url or item.add_jd or item.set_snapshot:
                    ledger.attach_job_description(
                        search,
                        active,
                        target.correlation_id,
                        saved,
                        entry.url if item.set_url else None,
                    )
                if item.mark_unavailable and entry.jd_unavailable is not None:
                    ledger.mark_jd_unavailable(
                        search, active, target.correlation_id, entry.jd_unavailable
                    )
                outcomes.append(Outcome(item.number, ATTACHING, target.correlation_id))
        except (selfsourced.LogError, ledger.AttachRefused) as exc:
            outcomes.append(Outcome(item.number, REFUSED, str(exc)))
    return outcomes
