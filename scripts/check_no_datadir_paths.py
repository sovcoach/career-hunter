#!/usr/bin/env python3
"""Pre-commit hook: block anything from the private data directory reaching git.

Which files it checks:

- Filenames passed on the command line (how pre-commit calls it). A file that is
  staged is checked as staged, from the index, which is what a commit would record.
  Any other file is checked as it is in the working tree. This makes
  `pre-commit run --all-files` and `pre-commit run --files ...` actually scan what
  they were given.
- With no filenames, every staged file, from the index.

A file is blocked if:

1. its path, or the target of a symlink, resolves into the data directory;
2. its content mentions a file *inside* the data directory (an absolute path that
   starts with the data dir followed by "/"), which usually means a log, export or
   snapshot path was pasted in;
3. it is a database, an env file, or one of the real (non-example) config filenames,
   which only ever belong in the data directory.

The data directory is $CAREERHUNTER_DATA_DIR, or the documented default if unset, so
the hook still protects a shell where the variable isn't exported.

Standard library only, so it runs before any project dependencies are installed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

DEFAULT_DATA_DIR = "/Volumes/X10-Pro/CareerHunterData"
REAL_CONFIG_NAMES = frozenset({"config.yaml", "exclusions.yaml", "seed.yaml"})
BLOCKED_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


class Entry(NamedTuple):
    path: str  # relative to the repo root
    is_symlink: bool
    content: bytes  # file bytes, or the symlink target


def staged_entries() -> dict[str, Entry]:
    """Every added/copied/modified/renamed file in the index, with its staged content."""
    names = _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").split(b"\0")
    entries: dict[str, Entry] = {}
    for raw in filter(None, names):
        path = raw.decode()
        info = _git("ls-files", "-s", "--", path).decode().split()
        mode = info[0] if info else "100644"
        entries[path] = Entry(path, mode == "120000", _git("show", f":{path}"))
    return entries


def worktree_entry(repo: Path, path: str) -> Entry | None:
    """A file as it is on disk, or None for something that isn't a file or symlink."""
    full = repo / path
    if full.is_symlink():
        return Entry(path, True, os.fsencode(os.readlink(full)))
    if full.is_file():
        return Entry(path, False, full.read_bytes())
    return None


def select_entries(repo: Path, argv: list[str], staged: dict[str, Entry]) -> list[Entry]:
    if not argv:
        return list(staged.values())
    entries: list[Entry] = []
    for arg in argv:
        rel = Path(os.path.relpath(Path(arg).absolute(), repo)).as_posix()
        entry = staged.get(rel) or worktree_entry(repo, rel)
        if entry is not None:
            entries.append(entry)
    return entries


def data_dirs() -> list[Path]:
    raw = os.environ.get("CAREERHUNTER_DATA_DIR", "").strip() or DEFAULT_DATA_DIR
    given = Path(raw).expanduser()
    return sorted({given, given.resolve()})


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def check(repo: Path, entries: list[Entry], dirs: list[Path]) -> list[str]:
    problems: list[str] = []
    # A path ends at whitespace, a quote, a closing bracket, or a backslash (so an escape
    # like the "\n" in source code isn't reported as part of the path).
    patterns = [re.compile(re.escape(str(d)).encode() + rb"/[^\s'\"`)>\\]+") for d in dirs]
    for entry in entries:
        rel = entry.path
        name = Path(rel).name
        if name in REAL_CONFIG_NAMES or name == ".env" or name.startswith(".env."):
            problems.append(f"{rel}: real config/env files live only in the data directory")
        if name.lower().endswith(BLOCKED_SUFFIXES):
            problems.append(f"{rel}: database files are never committed")

        candidates = [(repo / rel).resolve()]
        blob = entry.content
        if entry.is_symlink:
            target = Path(os.fsdecode(blob))
            candidates.append(
                (target if target.is_absolute() else repo / Path(rel).parent / target).resolve()
            )
        for candidate in candidates:
            if any(_within(candidate, d) for d in dirs):
                problems.append(f"{rel}: resolves into the data directory ({candidate})")
                break

        for pattern in patterns:
            match = pattern.search(blob)
            if match:
                problems.append(
                    f"{rel}: mentions a file inside the data directory "
                    f"({match.group().decode(errors='replace')})"
                )
                break
    return problems


def main(argv: list[str]) -> int:
    repo = Path(_git("rev-parse", "--show-toplevel").decode().strip())
    problems = check(repo, select_entries(repo, argv, staged_entries()), data_dirs())
    if problems:
        print("BLOCKED: this commit would expose private career-hunter data:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "Remove these from the repo (git restore --staged <file> if staged) and keep "
            "private data in the data dir.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
