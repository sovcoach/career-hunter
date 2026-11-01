"""The pre-push guard, run by real git against a throwaway repo and a local bare remote."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from careerhunter import cli

SCRIPT = Path(__file__).parents[1] / "scripts" / "pre_push_guard.py"
OVERRIDE = "CAREERHUNTER_ALLOW_PUSH_MAIN"


def load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pre_push_guard", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env)


def ok(repo: Path, *args: str) -> str:
    result = git(repo, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OVERRIDE, raising=False)


@pytest.fixture
def work(tmp_path: Path, clean_env: None) -> Path:
    """A clone whose `main` is on a bare remote, with the real guard as its pre-push hook."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    ok(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    ok(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "README").write_text("synthetic\n")
    ok(work, "add", "README")
    ok(work, "commit", "-q", "-m", "initial")
    ok(work, "remote", "add", "origin", str(bare))
    ok(work, "push", "-q", "origin", "main")  # before the guard is installed
    ok(work, "fetch", "-q", "origin")
    hook = work / ".git" / "hooks" / "pre-push"
    shutil.copyfile(SCRIPT, hook)
    hook.chmod(0o755)
    return work


def remote_ref(work: Path, ref: str) -> str:
    result = git(work.parent / "remote.git", "rev-parse", "--verify", "--quiet", ref)
    return result.stdout.strip()


def new_commit(work: Path, branch: str, *, track_main: bool = False) -> str:
    args = ["switch", "-q", "-c", branch] + (["origin/main"] if track_main else [])
    ok(work, *args)
    (work / f"{branch}.txt").write_text("synthetic\n")
    ok(work, "add", ".")
    ok(work, "commit", "-q", "-m", branch)
    return ok(work, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "push",
    [
        ["-c", "push.default=upstream", "push"],  # bare push from a branch tracking main
        ["push", "origin", "HEAD:main"],
        ["push", "origin", "feature:refs/heads/main"],
        ["push", "origin", ":main"],  # deleting main
        ["push", "origin", "feature", "HEAD:main"],  # main hidden among other refs
    ],
    ids=["bare push tracking main", "HEAD:main", "refs/heads/main", "delete main", "multi-ref"],
)
def test_pushes_to_main_are_refused(work: Path, push: list[str]) -> None:
    before = remote_ref(work, "main")
    new_commit(work, "feature", track_main=True)
    result = git(work, *push)
    assert result.returncode != 0
    assert "pre-push: refusing to push to main" in result.stderr
    assert remote_ref(work, "main") == before
    assert remote_ref(work, "feature") == ""  # a refused push sends nothing at all


def test_without_the_guard_that_bare_push_really_reaches_main(work: Path) -> None:
    """The control for the first case above: the gap the guard closes is real."""
    (work / ".git" / "hooks" / "pre-push").unlink()
    head = new_commit(work, "feature", track_main=True)
    ok(work, "-c", "push.default=upstream", "push", "-q")
    assert remote_ref(work, "main") == head


def test_the_override_allows_a_deliberate_push_to_main(work: Path) -> None:
    head = new_commit(work, "feature")
    result = git(work, "push", "origin", "HEAD:main", env={**os.environ, OVERRIDE: "1"})
    assert result.returncode == 0, result.stderr
    assert "is allowed" in result.stdout
    assert remote_ref(work, "main") == head


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_the_exact_override_value_counts(work: Path, value: str) -> None:
    new_commit(work, "feature")
    result = git(work, "push", "origin", "HEAD:main", env={**os.environ, OVERRIDE: value})
    assert result.returncode != 0


def test_feature_branches_and_tags_are_allowed(work: Path) -> None:
    head = new_commit(work, "feature")
    ok(work, "push", "-q", "origin", "feature")
    ok(work, "tag", "-a", "v9.9.9", "-m", "synthetic")
    ok(work, "push", "-q", "origin", "v9.9.9")
    assert remote_ref(work, "feature") == head
    assert remote_ref(work, "refs/tags/v9.9.9")


def test_push_default_current_sends_a_bare_push_to_its_own_branch(work: Path) -> None:
    """With push.default=current (set by make hooks), a branch tracking main is safe."""
    before = remote_ref(work, "main")
    head = new_commit(work, "feature", track_main=True)
    ok(work, "-c", "push.default=current", "push", "-q")
    assert remote_ref(work, "feature") == head
    assert remote_ref(work, "main") == before


def test_the_guard_reads_refs_not_command_text() -> None:
    guard = load_guard()
    zero = "0" * 40
    lines = [
        f"refs/heads/feature {'a' * 40} refs/heads/feature {zero}",
        f"refs/heads/feature {'a' * 40} refs/heads/main {'b' * 40}",
    ]
    assert guard.protected_refs(lines) == ["refs/heads/main"]
    assert guard.protected_refs(["refs/heads/main x refs/heads/maintenance y"]) == []
    assert guard.main(["pre-push", "origin"], lines, {}) == 1
    assert guard.main(["pre-push", "origin"], lines, {OVERRIDE: "1"}) == 0
    assert guard.main(["pre-push", "origin"], lines[:1], {}) == 0


# --- doctor's view of the checkout ----------------------------------------------------


def test_doctor_checks_push_default_and_the_installed_guard(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    ok(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "scripts").mkdir()
    shutil.copyfile(SCRIPT, repo / "scripts" / "pre_push_guard.py")
    assert cli.repo_push_checks(repo) == [
        ("WARN", "push.default is unset, not current; run: make hooks"),
        ("WARN", "the pre-push guard isn't installed; run: make hooks"),
    ]
    # What `make hooks` does.
    ok(repo, "config", "push.default", "current")
    hooks = repo / ok(repo, "rev-parse", "--git-path", "hooks")
    (hooks / "pre-push").symlink_to(repo / "scripts" / "pre_push_guard.py")
    assert cli.repo_push_checks(repo) == [
        ("OK", "push.default is current"),
        ("OK", "pre-push guard installed"),
    ]
    # Some other pre-push hook doesn't count.
    (hooks / "pre-push").unlink()
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    assert cli.repo_push_checks(repo)[1][0] == "WARN"
