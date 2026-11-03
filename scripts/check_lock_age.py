#!/usr/bin/env python3
"""Lock-age check: every version in uv.lock was two weeks old when it was locked (ADR-006).

`make check-lock-age` and CI run this from the repo root. For each package in the
working tree's `uv.lock`:

1. its upload time is the earliest `upload-time` of its sdist and wheels;
2. its locking time is the committer date of the first commit, oldest first, whose
   `uv.lock` holds that exact name and version. A version that no commit holds yet
   (an uncommitted re-lock) is judged against now;
3. it fails when the locking time is less than 14 days after the upload time, unless
   `scripts/lock_age_allowlist.toml` lists that package and version as a fix for known
   advisories.

The allowlist is exact: each entry names the package, the version and the advisory
IDs it fixes. An entry whose package and version are no longer in `uv.lock` is stale
and fails the check, so the list can't outlive the versions it excuses.

It fails closed, with a reason, when it cannot tell: a shallow clone (the history is
incomplete), a package with no upload time, a source other than the registry, or an
allowlist it can't read. The project's own editable or virtual entry is skipped.
Standard library only, so it runs before any project dependencies are installed.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_AGE = dt.timedelta(days=14)
LOCKFILE = "uv.lock"
ALLOWLIST = Path("scripts") / "lock_age_allowlist.toml"
ADVISORY = re.compile(
    r"CVE-\d{4}-\d{4,}|GHSA(-[23456789cfghjmpqrvwx]{4}){3}|PYSEC-\d{4}-\d+",
)


class LockAgeError(Exception):
    """A reason the lock's ages can't be checked at all. The message is user-facing."""


@dataclass(frozen=True)
class Violation:
    name: str
    version: str
    uploaded: dt.datetime
    locked: dt.datetime

    def line(self) -> str:
        days = (self.locked - self.uploaded).total_seconds() / 86400
        return (
            f"{self.name} {self.version}: uploaded {self.uploaded:%Y-%m-%d}, "
            f"locked {self.locked:%Y-%m-%d} ({days:.1f} days)"
        )


@dataclass(frozen=True)
class Allowed:
    """A version the allowlist excuses, because it fixes these advisories."""

    name: str
    version: str
    advisories: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    checked: int
    violations: list[Violation]
    allowed: list[Allowed]
    stale: list[Allowed]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise LockAgeError(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def _timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize(name: str) -> str:
    """A package name as the index compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def packages(lock: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Registry packages. The project's own entry is skipped; any other source fails."""
    found = []
    for package in lock.get("package", []):
        source = package.get("source", {})
        if "editable" in source or "virtual" in source:
            continue
        if "registry" not in source:
            raise LockAgeError(f"{package.get('name')} has no registry source to date")
        found.append(package)
    return found


def upload_time(package: Mapping[str, Any]) -> dt.datetime:
    """The earliest upload time of the package's sdist and wheels."""
    files = [package.get("sdist", {}), *package.get("wheels", [])]
    times = [_timestamp(f["upload-time"]) for f in files if f.get("upload-time")]
    if not times:
        raise LockAgeError(f"{package['name']} {package['version']} has no upload time")
    return min(times)


def allowlist(repo: Path) -> dict[tuple[str, str], Allowed]:
    """The excused (package, version) pairs. A missing file excuses nothing."""
    path = repo / ALLOWLIST
    if not path.is_file():
        return {}
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise LockAgeError(f"{ALLOWLIST} is not valid TOML: {exc}") from None
    if set(doc) - {"allow"}:
        raise LockAgeError(f"{ALLOWLIST} may only hold [[allow]] entries")
    entries: dict[tuple[str, str], Allowed] = {}
    for number, entry in enumerate(doc.get("allow", []), start=1):
        where = f"{ALLOWLIST} entry {number}"
        if not isinstance(entry, dict) or set(entry) != {"package", "version", "advisories"}:
            raise LockAgeError(f"{where} needs exactly package, version and advisories")
        name, version, advisories = entry["package"], entry["version"], entry["advisories"]
        if not isinstance(name, str) or not name.strip():
            raise LockAgeError(f"{where}: package must be a name")
        if not isinstance(version, str) or not version.strip():
            raise LockAgeError(f"{where}: version must be an exact version")
        if (
            not isinstance(advisories, list)
            or not advisories
            or not all(isinstance(a, str) and ADVISORY.fullmatch(a) for a in advisories)
        ):
            raise LockAgeError(f"{where}: advisories must list CVE, GHSA or PYSEC IDs")
        key = (normalize(name), version)
        if key in entries:
            raise LockAgeError(f"{where} repeats {name} {version}")
        entries[key] = Allowed(name, version, tuple(advisories))
    return entries


def first_locked(repo: Path) -> dict[tuple[str, str], dt.datetime]:
    """When each (name, version) first appeared in a committed uv.lock."""
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise LockAgeError("the clone is shallow, so when each version was locked is unknown")
    log = _git(repo, "log", "--reverse", "--format=%H %cI", "--", LOCKFILE)
    first: dict[tuple[str, str], dt.datetime] = {}
    for line in log.splitlines():
        commit, date = line.split(" ", 1)
        try:
            lock = tomllib.loads(_git(repo, "show", f"{commit}:{LOCKFILE}"))
        except (LockAgeError, tomllib.TOMLDecodeError):
            continue  # this commit deleted or broke the lockfile
        for package in lock.get("package", []):
            key = (package["name"], package["version"])
            first.setdefault(key, _timestamp(date))
    return first


def check(repo: Path, now: dt.datetime) -> Result:
    """Every registry package's age when locked, against the allowlist."""
    path = repo / LOCKFILE
    if not path.is_file():
        raise LockAgeError(f"{LOCKFILE} not found in {repo.name}")
    current = tomllib.loads(path.read_text(encoding="utf-8"))
    excused = allowlist(repo)
    first = first_locked(repo)
    checked = packages(current)
    locked_keys = {(normalize(p["name"]), p["version"]) for p in checked}
    violations, allowed = [], []
    for package in checked:
        uploaded = upload_time(package)
        locked = first.get((package["name"], package["version"]), now)
        if locked - uploaded >= MIN_AGE:
            continue
        exception = excused.get((normalize(package["name"]), package["version"]))
        if exception is not None:
            allowed.append(exception)
        else:
            violations.append(Violation(package["name"], package["version"], uploaded, locked))
    stale = [entry for key, entry in excused.items() if key not in locked_keys]
    return Result(len(checked), violations, allowed, stale)


def main(argv: Sequence[str], now: dt.datetime | None = None) -> int:
    repo = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    try:
        result = check(repo, now or dt.datetime.now(dt.UTC))
    except LockAgeError as exc:
        print(f"check-lock-age: cannot check: {exc}", file=sys.stderr)
        return 1
    failed = False
    if result.violations:
        failed = True
        print(
            f"check-lock-age: {len(result.violations)} of {result.checked} locked versions "
            "were less than 14 days old when locked (ADR-006):",
            file=sys.stderr,
        )
        for violation in result.violations:
            print(f"  {violation.line()}", file=sys.stderr)
    if result.stale:
        failed = True
        print(f"check-lock-age: stale entries in {ALLOWLIST}:", file=sys.stderr)
        for entry in result.stale:
            print(f"  {entry.name} {entry.version} is not in {LOCKFILE}", file=sys.stderr)
    if failed:
        return 1
    print(f"check-lock-age: OK, all {result.checked} locked versions pass")
    for entry in result.allowed:
        print(
            f"  allowed as a security fix: {entry.name} {entry.version} "
            f"({len(entry.advisories)} advisories)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
