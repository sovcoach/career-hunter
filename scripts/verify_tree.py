#!/usr/bin/env python3
"""Whole-tree verification: every hook, plus gitleaks, over every file. `make verify-tree`.

`pre-commit run --all-files` only covers tracked files, and the upstream gitleaks hook
only ever scans staged changes, whatever files it is handed (ADR-013). This script
covers the whole working tree, untracked files included:

1. It lists tracked files (`git ls-files`) and untracked, unignored ones
   (`git ls-files -o --exclude-standard`), NUL-separated, so any file name is safe and
   nothing depends on shell word-splitting.
2. It runs every pre-commit hook with `--files` over that list.
3. It runs the pinned gitleaks that pre-commit built (the `rev` in
   .pre-commit-config.yaml, found through pre-commit's own cache database) with
   `gitleaks dir`. That command takes a single path, so the listed files are copied
   into a temporary folder and the folder is scanned, with the repo's .gitleaks.toml.

It fails if either step fails, and also, loudly, if the file list is empty or that
gitleaks build can't be found, rather than passing on a scan that checked nothing.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

GITLEAKS_REPO = "https://github.com/gitleaks/gitleaks"
Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


class VerifyError(Exception):
    """A reason the tree can't be verified at all. The message is user-facing."""


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True).stdout


def tree_files(repo: Path) -> tuple[list[str], int]:
    """Tracked plus untracked-and-unignored paths that exist. Returns (paths, untracked)."""
    tracked = [p.decode() for p in _git(repo, "ls-files", "-z").split(b"\0") if p]
    untracked = [
        p.decode()
        for p in _git(repo, "ls-files", "-z", "-o", "--exclude-standard").split(b"\0")
        if p
    ]
    # A tracked file deleted in the working tree has nothing left to scan.
    paths = [p for p in dict.fromkeys([*tracked, *untracked]) if os.path.lexists(repo / p)]
    return paths, len([p for p in untracked if p in paths])


def pinned_gitleaks_rev(repo: Path) -> str:
    config = yaml.safe_load((repo / ".pre-commit-config.yaml").read_text())
    for entry in config.get("repos", []):
        if entry.get("repo") == GITLEAKS_REPO:
            return str(entry["rev"])
    raise VerifyError(f"{GITLEAKS_REPO} is not pinned in .pre-commit-config.yaml")


def find_gitleaks(rev: str) -> Path:
    """The gitleaks binary pre-commit built for `rev`, from pre-commit's cache database."""
    home = Path(os.environ.get("PRE_COMMIT_HOME") or Path.home() / ".cache" / "pre-commit")
    database = home / "db.db"
    hint = "run: uv run pre-commit install-hooks"
    if not database.is_file():
        raise VerifyError(f"pre-commit has no hook cache at {home}; {hint}")
    conn = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT path FROM repos WHERE repo = ? AND ref = ?", (GITLEAKS_REPO, rev)
        ).fetchall()
    finally:
        conn.close()
    for (path,) in rows:
        binary = Path(path) / "golangenv-default" / "bin" / "gitleaks"
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
    raise VerifyError(f"gitleaks {rev} hasn't been built by pre-commit yet; {hint}")


def run_hooks(repo: Path, files: Sequence[str], runner: Runner) -> int:
    result = runner([sys.executable, "-m", "pre_commit", "run", "--files", *files], cwd=repo)
    return int(result.returncode)


def run_gitleaks(repo: Path, files: Sequence[str], binary: Path, runner: Runner) -> int:
    with tempfile.TemporaryDirectory(prefix="verify-tree-") as tmp:
        copied = 0
        for rel in files:
            src = repo / rel
            if src.is_symlink() or not src.is_file():
                continue  # gitleaks doesn't follow symlinks; the data-path hook checks them
            dst = Path(tmp) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied += 1
        if not copied:
            raise VerifyError("no regular files to give gitleaks; refusing to pass an empty scan")
        print(f"verify-tree: gitleaks scanning {copied} files", flush=True)
        command = [
            str(binary),
            "dir",
            tmp,
            "--config",
            str(repo / ".gitleaks.toml"),
            "--no-banner",
            "--redact",
        ]
        return int(runner(command, cwd=repo).returncode)


def verify(
    repo: Path,
    runner: Runner = subprocess.run,
    gitleaks: Callable[[str], Path] = find_gitleaks,
) -> int:
    try:
        files, untracked = tree_files(repo)
        if not files:
            raise VerifyError("the file list is empty; refusing to pass a scan of nothing")
        binary = gitleaks(pinned_gitleaks_rev(repo))
        print(f"verify-tree: {len(files)} files ({untracked} untracked)", flush=True)
        hooks = run_hooks(repo, files, runner)
        leaks = run_gitleaks(repo, files, binary, runner)
    except VerifyError as exc:
        print(f"verify-tree: FAILED: {exc}", file=sys.stderr)
        return 2
    failed = [name for name, code in (("pre-commit hooks", hooks), ("gitleaks", leaks)) if code]
    if failed:
        print(f"verify-tree: FAILED: {' and '.join(failed)}", file=sys.stderr)
        return 1
    print(f"verify-tree: OK, {len(files)} files checked by every hook and by gitleaks")
    return 0


def main() -> int:
    top = _git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip()
    return verify(Path(top))


if __name__ == "__main__":
    sys.exit(main())
