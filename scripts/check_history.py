#!/usr/bin/env python3
"""History path check: a pull request adds or removes no local paths (#29).

CI runs this on every pull request over its commit range, `BASE..HEAD`. It reads the
full `git log -p` of that range: every commit message, and every added and removed
line. Any `/Volumes/...` or `/Users/...` path that is not covered by
`scripts/history_path_allowlist.txt` fails the check. An allowlist entry also allows
anything below it. A bare `/Volumes/` or `/Users/` with no path after it, and a path
inside a longer one such as `/System/Volumes/Data`, are not local paths.

A finding names the commit and where the path is, and masks the path itself: CI logs
on a public repository are public, so the check must not repeat what it found.

It fails closed when it cannot tell: a shallow clone, a range git cannot read, or a
missing allowlist. Standard library only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST = Path("scripts") / "history_path_allowlist.txt"
# A path segment starts with a letter, digit or one of _@+-, so `/Users/...` and
# `/Users/<name>` are not paths. It ends at whitespace, a quote, a bracket, a separator,
# or a backslash, so an escape such as `\n` in source code isn't read as part of it.
PATH = re.compile(r"(?<![\w.~-])/(?:Volumes|Users)/[A-Za-z0-9_@+-][^\s'\"`)\]>,;:*\\]*")
MARK = "\x00commit "


class HistoryError(Exception):
    """A reason the range can't be checked at all. The message is user-facing."""


@dataclass(frozen=True)
class Finding:
    commit: str
    where: str
    masked: str

    def line(self) -> str:
        return f"{self.commit[:7]} {self.where}: {self.masked}"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise HistoryError(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def allowlist(repo: Path) -> list[str]:
    path = repo / ALLOWLIST
    if not path.is_file():
        raise HistoryError(f"{ALLOWLIST} is missing")
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.append(line.rstrip("/"))
    return entries


def allowed(path: str, entries: Sequence[str]) -> bool:
    clean = path.rstrip("/.")
    return any(clean == entry or clean.startswith(entry + "/") for entry in entries)


def mask(path: str) -> str:
    prefix = "/Volumes/" if path.startswith("/Volumes/") else "/Users/"
    return f"{prefix}… ({len(path)} characters)"


def scan(repo: Path, base: str, head: str) -> tuple[int, list[Finding]]:
    """The number of commits in BASE..HEAD, and every disallowed path in them."""
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise HistoryError("the clone is shallow, so the range may be incomplete")
    entries = allowlist(repo)
    count = len(_git(repo, "rev-list", f"{base}..{head}").split())
    log = _git(repo, "log", "-p", "--no-color", "--format=%x00commit %H%n%B", f"{base}..{head}")
    findings: list[Finding] = []
    commit, where = "", "commit message"
    for line in log.splitlines():
        if line.startswith(MARK):
            commit, where = line[len(MARK) :], "commit message"
            continue
        if line.startswith("diff --git "):
            where = line.split(" b/", 1)[-1]
            continue
        for match in PATH.finditer(line):
            if not allowed(match.group(0), entries):
                kind = (
                    "added line in "
                    if line.startswith("+")
                    else "removed line in "
                    if line.startswith("-")
                    else ""
                )
                place = where if where == "commit message" else f"{kind}{where}"
                findings.append(Finding(commit, place, mask(match.group(0))))
    return count, findings


def main(argv: Sequence[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: check_history.py BASE [HEAD]", file=sys.stderr)
        return 1
    base, head = argv[1], argv[2] if len(argv) == 3 else "HEAD"
    try:
        count, findings = scan(Path.cwd(), base, head)
    except HistoryError as exc:
        print(f"check-history: cannot check: {exc}", file=sys.stderr)
        return 1
    if findings:
        print(
            f"check-history: {len(findings)} local path(s) not in {ALLOWLIST} "
            f"in {count} commit(s):",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  {finding.line()}", file=sys.stderr)
        return 1
    print(f"check-history: OK, no local paths in {count} commit(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
