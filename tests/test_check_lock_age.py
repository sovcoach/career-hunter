"""The lock-age check (ADR-006), run against throwaway repos with synthetic lockfiles."""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_lock_age.py"
NOW = dt.datetime(2026, 6, 30, tzinfo=dt.UTC)


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_lock_age", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


check_lock_age = load_check()


def lock_text(packages: Sequence[tuple[str, str, str | None]]) -> str:
    """A uv.lock with one registry package per (name, version, upload time)."""
    parts = [
        'version = 1\nrequires-python = ">=3.12"\n',
        '[[package]]\nname = "example-project"\nversion = "0.1.0"\nsource = { editable = "." }\n',
    ]
    for name, version, uploaded in packages:
        stamp = f', upload-time = "{uploaded}"' if uploaded else ""
        parts.append(
            f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
            'source = { registry = "https://pypi.example.invalid/simple" }\n'
            f'sdist = {{ url = "https://files.example.invalid/{name}-{version}.tar.gz", '
            f'hash = "sha256:00", size = 1{stamp} }}\n'
        )
    return "\n".join(parts)


def git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout


def commit_lock(repo: Path, text: str, date: str) -> None:
    (repo / "uv.lock").write_text(text, encoding="utf-8")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-q", "-m", "lock", date=date)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def run(repo: Path) -> tuple[int, list[str]]:
    count, violations = check_lock_age.check(repo, NOW)
    return count, [v.line() for v in violations]


def test_a_version_two_weeks_old_when_locked_passes(repo: Path) -> None:
    commit_lock(
        repo,
        lock_text([("example-widget", "1.0.0", "2026-01-01T00:00:00Z")]),
        "2026-01-15T00:00:00Z",
    )
    assert run(repo) == (1, [])


def test_a_version_locked_too_soon_fails(repo: Path) -> None:
    commit_lock(
        repo,
        lock_text([("example-widget", "1.0.0", "2026-01-01T00:00:00Z")]),
        "2026-01-14T21:36:00Z",
    )
    count, lines = run(repo)
    assert count == 1
    assert lines == ["example-widget 1.0.0: uploaded 2026-01-01, locked 2026-01-14 (13.9 days)"]


def test_the_first_commit_that_locked_a_version_decides(repo: Path) -> None:
    text = lock_text([("example-widget", "1.0.0", "2026-01-01T00:00:00Z")])
    commit_lock(repo, text, "2026-01-05T00:00:00Z")
    (repo / "README").write_text("synthetic\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "later", date="2026-03-01T00:00:00Z")
    commit_lock(repo, text + "\n", "2026-03-02T00:00:00Z")  # rewritten, same version
    assert run(repo)[1] == [
        "example-widget 1.0.0: uploaded 2026-01-01, locked 2026-01-05 (4.0 days)"
    ]


def test_a_version_changed_later_is_judged_by_its_later_commit(repo: Path) -> None:
    commit_lock(
        repo,
        lock_text([("example-widget", "1.0.0", "2025-12-01T00:00:00Z")]),
        "2026-01-01T00:00:00Z",
    )
    commit_lock(
        repo,
        lock_text([("example-widget", "1.1.0", "2026-02-01T00:00:00Z")]),
        "2026-02-20T00:00:00Z",
    )
    assert run(repo) == (1, [])


def test_an_uncommitted_version_is_judged_against_now(repo: Path) -> None:
    commit_lock(
        repo,
        lock_text([("example-widget", "1.0.0", "2026-01-01T00:00:00Z")]),
        "2026-01-20T00:00:00Z",
    )
    (repo / "uv.lock").write_text(
        lock_text([("example-widget", "1.1.0", "2026-06-25T00:00:00Z")]), encoding="utf-8"
    )
    assert run(repo)[1] == [
        "example-widget 1.1.0: uploaded 2026-06-25, locked 2026-06-30 (5.0 days)"
    ]


def test_the_earliest_upload_of_a_version_counts(repo: Path) -> None:
    text = (
        lock_text([("example-widget", "1.0.0", "2026-01-10T00:00:00Z")])
        .replace(
            "size = 1, upload-time",
            'size = 1, upload-time = "2026-01-01T00:00:00Z" }\nwheels = [{ url = '
            '"https://files.example.invalid/w.whl", hash = "sha256:00", size = 1, upload-time',
        )
        .replace('"2026-01-10T00:00:00Z" }\n', '"2026-01-10T00:00:00Z" }]\n')
    )
    commit_lock(repo, text, "2026-01-16T00:00:00Z")
    assert run(repo) == (1, [])


def test_a_missing_upload_time_fails_closed(repo: Path) -> None:
    commit_lock(repo, lock_text([("example-widget", "1.0.0", None)]), "2026-01-20T00:00:00Z")
    with pytest.raises(check_lock_age.LockAgeError, match="no upload time"):
        run(repo)


def test_a_non_registry_source_fails_closed(repo: Path) -> None:
    text = lock_text([]) + (
        '\n[[package]]\nname = "example-gadget"\nversion = "2.0.0"\n'
        'source = { git = "https://git.example.invalid/gadget" }\n'
    )
    commit_lock(repo, text, "2026-01-20T00:00:00Z")
    with pytest.raises(check_lock_age.LockAgeError, match="no registry source"):
        run(repo)


def test_the_project_itself_is_skipped(repo: Path) -> None:
    commit_lock(repo, lock_text([]), "2026-01-20T00:00:00Z")
    assert run(repo) == (0, [])


def test_a_shallow_clone_fails_closed(repo: Path, tmp_path: Path) -> None:
    commit_lock(
        repo,
        lock_text([("example-widget", "1.0.0", "2026-01-01T00:00:00Z")]),
        "2026-01-20T00:00:00Z",
    )
    commit_lock(
        repo,
        lock_text([("example-widget", "1.1.0", "2026-01-01T00:00:00Z")]),
        "2026-01-21T00:00:00Z",
    )
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    with pytest.raises(check_lock_age.LockAgeError, match="shallow"):
        run(shallow)


def test_main_reports_and_exits_non_zero(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit_lock(
        repo,
        lock_text([("example-widget", "1.0.0", "2026-01-01T00:00:00Z")]),
        "2026-01-03T00:00:00Z",
    )
    assert check_lock_age.main(["check_lock_age.py", str(repo)], now=NOW) == 1
    err = capsys.readouterr().err
    assert "1 of 1 locked versions were less than 14 days old" in err
    assert "example-widget 1.0.0" in err


def test_main_passes_and_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit_lock(
        repo,
        lock_text([("example-widget", "1.0.0", "2026-01-01T00:00:00Z")]),
        "2026-02-01T00:00:00Z",
    )
    assert check_lock_age.main(["check_lock_age.py", str(repo)], now=NOW) == 0
    assert "OK, all 1 locked versions" in capsys.readouterr().out


def test_main_explains_why_it_cannot_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_lock_age.main(["check_lock_age.py", str(tmp_path)], now=NOW) == 1
    assert "cannot check: uv.lock not found" in capsys.readouterr().err
