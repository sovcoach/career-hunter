#!/usr/bin/env python3
"""Lock-age check: every version in uv.lock was two weeks old when it was locked (ADR-006).

`make check-lock-age` and CI run this from the repo root. For each package in the
working tree's `uv.lock`:

1. its upload time is the earliest `upload-time` of its sdist and wheels;
2. its locking time is the committer date of the first commit, oldest first, whose
   `uv.lock` holds that exact name and version. A version that no commit holds yet
   (an uncommitted re-lock) is judged against now;
3. it fails when the locking time is less than 14 days after the upload time.

It fails closed, with a reason, when it cannot tell: a shallow clone (the history is
incomplete), a package with no upload time, or a source other than the registry. The
project's own editable or virtual entry is skipped. Standard library only, so it runs
before any project dependencies are installed.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_AGE = dt.timedelta(days=14)
LOCKFILE = "uv.lock"


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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise LockAgeError(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def _timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def check(repo: Path, now: dt.datetime) -> tuple[int, list[Violation]]:
    """The number of packages checked, and those locked before they were two weeks old."""
    path = repo / LOCKFILE
    if not path.is_file():
        raise LockAgeError(f"{LOCKFILE} not found in {repo.name}")
    current = tomllib.loads(path.read_text(encoding="utf-8"))
    first = first_locked(repo)
    checked = packages(current)
    violations = []
    for package in checked:
        uploaded = upload_time(package)
        locked = first.get((package["name"], package["version"]), now)
        if locked - uploaded < MIN_AGE:
            violations.append(Violation(package["name"], package["version"], uploaded, locked))
    return len(checked), violations


def main(argv: Sequence[str], now: dt.datetime | None = None) -> int:
    repo = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    try:
        count, violations = check(repo, now or dt.datetime.now(dt.UTC))
    except LockAgeError as exc:
        print(f"check-lock-age: cannot check: {exc}", file=sys.stderr)
        return 1
    if violations:
        print(
            f"check-lock-age: {len(violations)} of {count} locked versions were less than "
            "14 days old when locked (ADR-006):",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation.line()}", file=sys.stderr)
        return 1
    print(f"check-lock-age: OK, all {count} locked versions were at least 14 days old")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
