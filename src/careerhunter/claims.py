"""EI claims: the claim config, the claim-period check, and one record file per claim.

The active claim is set in config.yaml, never computed:

    ei:
      active_claim: "2026-27"      # or null while not claiming
      claims:
        - label: "2025-26"
          start: 2025-06-01
          end: 2026-05-30          # required once a claim is not the active one
        - label: "2026-27"
          start: 2026-08-03        # end is optional for the active claim

Each claim's record lives in ei_record_<label>.db. A file is created complete or not at
all: it is built under a temporary name and linked into place only once its claim row
is written.

When a claim is over, its file is sealed with `careerhunter claim seal <label>`, and
only that way: the database then refuses every change, the WAL is folded into the main
file, and the file is made read-only (0400). Sealing is never automatic. Until every
earlier claim is sealed, commands that write refuse to run and name the command to use.
"""

from __future__ import annotations

import datetime as dt
import enum
import itertools
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from careerhunter import db
from careerhunter.models import Dating
from careerhunter.storage_guard import CLAIM_LABEL_SHAPE, EI_RECORD_PREFIX, DataDir

RETENTION_YEARS = 6
_CLAIM_KEYS = {"label", "start", "end"}


class ClaimError(Exception):
    """A claim problem the user has to fix. The message is user-facing."""


@dataclass(frozen=True)
class Claim:
    label: str
    start: dt.date
    end: dt.date | None = None

    @property
    def period(self) -> str:
        return f"{self.start} to {self.end}" if self.end else f"from {self.start}"


@dataclass(frozen=True)
class ClaimSettings:
    active: Claim | None = None
    claims: tuple[Claim, ...] = ()

    def get(self, label: str) -> Claim | None:
        return next((c for c in self.claims if c.label == label), None)


def label_problem(label: Any) -> str | None:
    """Why `label` is not a claim label, or None if it is one."""
    if not isinstance(label, str) or not CLAIM_LABEL_SHAPE.match(label):
        return f"{label!r} is not a claim label like 2026-27"
    first, second = int(label[:4]), int(label[5:])
    if second != (first + 1) % 100:
        return f"{label!r} must name two consecutive years, like {first}-{(first + 1) % 100:02d}"
    return None


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return None
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def validate_section(section: Any) -> list[str]:
    """Problems with the `ei` section of config.yaml. A missing section is fine."""
    if section is None:
        return []
    if not isinstance(section, dict):
        return ["ei must be a mapping with active_claim and claims"]
    problems: list[str] = []
    unknown = set(section) - {"active_claim", "claims"}
    if unknown:
        problems.append(f"ei has unknown keys: {sorted(unknown)}")
    entries = section.get("claims", [])
    if entries is None:
        entries = []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        problems.append("ei.claims must be a list of mappings with label, start and end")
        entries = []

    parsed: list[Claim] = []
    for i, entry in enumerate(entries):
        where = f"ei.claims[{i}]"
        extra = set(entry) - _CLAIM_KEYS
        if extra:
            problems.append(f"{where} has unknown keys: {sorted(extra)}")
        label = entry.get("label")
        bad_label = label_problem(label)
        if bad_label:
            problems.append(f"{where}.label: {bad_label}")
        start = _as_date(entry.get("start"))
        if start is None:
            problems.append(f"{where}.start must be a YYYY-MM-DD date")
        end = None
        if entry.get("end") is not None:
            end = _as_date(entry.get("end"))
            if end is None:
                problems.append(f"{where}.end must be a YYYY-MM-DD date")
        if start and end and end < start:
            problems.append(f"{where}: end {end} is before start {start}")
        if not bad_label and start and start.year != int(str(label)[:4]):
            problems.append(f"{where}: claim {label} must start in {str(label)[:4]}, not {start}")
        if not bad_label and start:
            parsed.append(Claim(str(label), start, end))

    labels = [c.label for c in parsed]
    for label in sorted({x for x in labels if labels.count(x) > 1}):
        problems.append(f"ei.claims lists {label} more than once")
    ordered = sorted(parsed, key=lambda c: c.start)
    for earlier, later in itertools.pairwise(ordered):
        if earlier.end is None or earlier.end >= later.start:
            problems.append(f"claims {earlier.label} and {later.label} overlap")

    active = section.get("active_claim")
    if active is not None and active not in labels:
        problems.append(f"ei.active_claim {active!r} is not one of the claims listed: {labels}")
    for claim in parsed:
        if claim.label != active and claim.end is None:
            problems.append(f"claim {claim.label} is not the active claim, so it needs an end date")
    return problems


def settings_from_config(doc: dict[str, Any]) -> ClaimSettings:
    """Build the claim settings from an already validated config document."""
    section = doc.get("ei") or {}
    claims = tuple(
        Claim(str(e["label"]), _as_date(e["start"]) or dt.date.min, _as_date(e.get("end")))
        for e in section.get("claims") or []
    )
    active_label = section.get("active_claim")
    active = next((c for c in claims if c.label == active_label), None)
    return ClaimSettings(active=active, claims=claims)


def warnings(settings: ClaimSettings, today: dt.date) -> list[str]:
    claim = settings.active
    if claim and claim.end and claim.end < today:
        return [
            f"active claim {claim.label} ended on {claim.end}. Start the next claim, or set "
            "ei.active_claim to null, in config.yaml"
        ]
    return []


def keep_until(end: dt.date) -> dt.date:
    try:
        return end.replace(year=end.year + RETENTION_YEARS)
    except ValueError:  # 29 February
        return end.replace(year=end.year + RETENTION_YEARS, day=28)


# --- The claim-period check ---------------------------------------------------------


class Verdict(enum.Enum):
    ACCEPT = "accept"
    WARN = "warn"  # accepted, with a warning
    REFUSE = "refuse"


def check_dating(claim: Claim, dating: Dating) -> tuple[Verdict, str]:
    """Does a record with this date belong to `claim`? The rule depends on precision.

    - exact: the day must fall within the claim;
    - week: the week (Monday to Sunday) must overlap the claim;
    - unknown: always accepted, with a warning, and shown under "Date unknown".
    """
    end = claim.end or dt.date.max
    if dating.precision == "exact" and dating.day:
        day = dt.date.fromisoformat(dating.day)
        if day < claim.start:
            return Verdict.REFUSE, f"{day} is before claim {claim.label} started ({claim.start})"
        if day > end:
            return Verdict.REFUSE, f"{day} is after claim {claim.label} ended ({claim.end})"
        return Verdict.ACCEPT, ""
    if dating.precision == "week" and dating.week_of:
        monday = dt.date.fromisoformat(dating.week_of)
        if monday + dt.timedelta(days=6) < claim.start:
            return (
                Verdict.REFUSE,
                f"the week of {monday} is entirely before claim {claim.label} started "
                f"({claim.start})",
            )
        if monday > end:
            return (
                Verdict.REFUSE,
                f"the week of {monday} is entirely after claim {claim.label} ended ({claim.end})",
            )
        return Verdict.ACCEPT, ""
    return (
        Verdict.WARN,
        f"date unknown: recorded under claim {claim.label} and listed under "
        '"Date unknown" in exports',
    )


# --- Claim record files ---------------------------------------------------------------


@dataclass(frozen=True)
class FileClaim:
    """The claim row stored inside an ei_record file."""

    label: str
    start: str
    end: str | None
    sealed_at: str | None


def read_file_claim(conn: sqlite3.Connection) -> FileClaim | None:
    if db.file_track(conn) != "ei_record":
        return None
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claim'"
    ).fetchone()
    if not has_table:
        return None
    row = conn.execute("SELECT label, start_date, end_date, sealed_at FROM claim").fetchone()
    if row is None:
        return None
    return FileClaim(row["label"], row["start_date"], row["end_date"], row["sealed_at"])


def read_claim_file(path: Path) -> FileClaim | None:
    conn = db.connect_read_only(path)
    try:
        return read_file_claim(conn)
    finally:
        conn.close()


def label_of(path: Path) -> str:
    return path.name.removeprefix(EI_RECORD_PREFIX).removesuffix(".db")


def mismatch(file_claim: FileClaim | None, claim: Claim, path: Path) -> str | None:
    """Why the file at `path` is not the record of `claim`, or None if it is."""
    if file_claim is None:
        return f"{path.name} is not a complete EI record file (no claim row)"
    if file_claim.label != claim.label:
        return f"{path.name} records claim {file_claim.label}, not {claim.label}"
    if file_claim.start != claim.start.isoformat():
        return (
            f"{path.name} records a start of {file_claim.start}, but config.yaml says "
            f"{claim.start}. A claim's start date can't change"
        )
    return None


def write_blockers(datadir: DataDir, settings: ClaimSettings) -> list[str]:
    """Reasons commands that write must not run yet: unsealed or unknown claim files."""
    problems: list[str] = []
    active = settings.active.label if settings.active else None
    for path in datadir.ei_record_files():
        label = label_of(path)
        if label_problem(label):
            problems.append(f"{path.name} is not a valid claim file name")
            continue
        if label == active:
            continue
        if settings.get(label) is None:
            problems.append(
                f"{path.name} is not listed under ei.claims in config.yaml. Add claim {label} "
                "with its start and end dates"
            )
            continue
        file_claim = read_claim_file(path)
        if file_claim is None or file_claim.sealed_at is None:
            problems.append(
                f"claim {label} is not the active claim, but its record is not sealed yet. "
                f"Run: careerhunter claim seal {label}"
            )
    return problems


def require_write_ready(datadir: DataDir, settings: ClaimSettings) -> None:
    problems = write_blockers(datadir, settings)
    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise ClaimError(f"refusing to write until these are fixed:\n{detail}")


@dataclass
class ActiveClaim:
    """An open, writable connection to the active claim's record file."""

    conn: sqlite3.Connection
    claim: Claim
    path: Path
    created: bool = False
    applied: list[int] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _create_file(path: Path, claim: Claim) -> None:
    tmp = path.with_name(path.name + ".tmp")
    for leftover in [*db.db_files(tmp), tmp.with_name(tmp.name + "-journal")]:
        # A crash while building: the file was never linked in, so it never held a record.
        if leftover.exists():
            leftover.unlink()
    conn = db.connect(tmp, wal=False)
    try:
        db.migrate(conn, "ei_record")
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO claim (id, label, start_date, end_date) VALUES (1, ?, ?, ?)",
                (
                    claim.label,
                    claim.start.isoformat(),
                    claim.end.isoformat() if claim.end else None,
                ),
            )
    finally:
        conn.close()
    os.link(tmp, path)  # fails if the record file appeared meanwhile; never overwrites
    tmp.unlink()


def open_active(datadir: DataDir, settings: ClaimSettings) -> ActiveClaim | None:
    """Open (creating if needed) the active claim's file for writing. None if no claim.

    The caller must already have run `require_write_ready`.
    """
    claim = settings.active
    if claim is None:
        return None
    path = datadir.ei_record_path(claim.label)
    created = not path.exists()
    if created:
        _create_file(path, claim)
    else:
        existing = read_claim_file(path)
        if existing is not None and existing.sealed_at is not None:
            raise ClaimError(
                f"claim {claim.label} was sealed on {existing.sealed_at}, so it can't be the "
                "active claim. Start a new claim in config.yaml"
            )
    conn = db.connect(path)
    try:
        applied = db.migrate(conn, "ei_record")
        file_claim = read_file_claim(conn)
        problem = mismatch(file_claim, claim, path)
        if problem:
            raise ClaimError(problem)
        assert file_claim is not None  # noqa: S101 - mismatch() covers None
        if file_claim.sealed_at is not None:
            raise ClaimError(
                f"claim {claim.label} was sealed on {file_claim.sealed_at}, so it can't be the "
                "active claim. Start a new claim in config.yaml"
            )
        active = ActiveClaim(conn, claim, path, created, applied)
        wanted = claim.end.isoformat() if claim.end else None
        if file_claim.end != wanted:
            if wanted is None:
                raise ClaimError(
                    f"{path.name} records an end of {file_claim.end} for claim {claim.label}, "
                    "but config.yaml gives none. Add end back to the claim in config.yaml"
                )
            try:
                with db.transaction(conn):
                    conn.execute("UPDATE claim SET end_date = ? WHERE id = 1", (wanted,))
            except sqlite3.IntegrityError as exc:
                raise ClaimError(f"can't set claim {claim.label}'s end to {wanted}: {exc}") from exc
            active.messages.append(f"claim {claim.label}: end date recorded as {wanted}")
    except BaseException:
        conn.close()
        raise
    return active


@contextmanager
def active_claim(datadir: DataDir, settings: ClaimSettings) -> Iterator[ActiveClaim | None]:
    active = open_active(datadir, settings)
    try:
        yield active
    finally:
        if active:
            active.conn.close()


# --- Sealing --------------------------------------------------------------------------


@dataclass(frozen=True)
class SealPlan:
    claim: Claim
    path: Path
    end: dt.date

    def describe(self) -> str:
        return (
            f"This will permanently close the record for EI claim {self.claim.label} "
            f"({self.claim.start} to {self.end}):\n"
            f"  - record {self.end} as the claim's end date;\n"
            f"  - mark {self.path.name} sealed, so the database refuses every further change;\n"
            "  - fold its write-ahead log into the main file;\n"
            "  - make the file read-only (0400).\n"
            f"It stays readable for `careerhunter export --claim {self.claim.label}`. Keep it "
            f"until at least {keep_until(self.end)}. This can't be undone."
        )


def plan_seal(datadir: DataDir, settings: ClaimSettings, label: str, today: dt.date) -> SealPlan:
    """Check that `label` can be sealed, and say what sealing will do. Changes nothing."""
    bad = label_problem(label)
    if bad:
        raise ClaimError(bad)
    claim = settings.get(label)
    if claim is None:
        raise ClaimError(f"claim {label} is not listed under ei.claims in config.yaml")
    if settings.active and settings.active.label == label:
        raise ClaimError(
            f"claim {label} is the active claim. Set ei.active_claim to the next claim "
            "(or null) in config.yaml before sealing it"
        )
    if claim.end is None:  # validation requires an end for every non-active claim
        raise ClaimError(f"claim {label} has no end date in config.yaml")
    if claim.end > today:
        raise ClaimError(f"claim {label} ends on {claim.end}, which hasn't happened yet")
    path = datadir.ei_record_path(label)
    if not path.is_file():
        raise ClaimError(f"there is no record file for claim {label} ({path.name})")
    file_claim = read_claim_file(path)
    problem = mismatch(file_claim, claim, path)
    if problem:
        raise ClaimError(problem)
    assert file_claim is not None  # noqa: S101 - mismatch() covers None
    if file_claim.sealed_at is not None:
        raise ClaimError(f"claim {label} was already sealed on {file_claim.sealed_at}")
    return SealPlan(claim, path, claim.end)


def seal(plan: SealPlan) -> None:
    conn = db.connect(plan.path)
    try:
        try:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE claim SET end_date = ?, "
                    "sealed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1",
                    (plan.end.isoformat(),),
                )
        except sqlite3.IntegrityError as exc:
            raise ClaimError(f"can't seal claim {plan.claim.label}: {exc}") from exc
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    finally:
        conn.close()
    if mode != "delete":
        raise ClaimError(f"{plan.path.name} is sealed but still in {mode} mode; run doctor")
    os.chmod(plan.path, 0o400)
    leftovers = [f.name for f in db.db_files(plan.path)[1:] if f.exists()]
    if leftovers:
        raise ClaimError(f"{plan.path.name} is sealed but {', '.join(leftovers)} remain")
