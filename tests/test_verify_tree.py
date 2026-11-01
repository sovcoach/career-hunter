"""`make verify-tree`: whole-tree coverage, untracked files included, and no silent passes."""

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import sqlite3
import string
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
FAKE_GITLEAKS = Path("/nonexistent/fake-gitleaks")


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load("verify_tree")


def ok(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo whose only hook is the real data-path check (no network needed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ok(repo, "init", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts" / "check_no_datadir_paths.py", repo / "scripts" / "check.py")
    shutil.copyfile(ROOT / ".gitleaks.toml", repo / ".gitleaks.toml")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v0.0.0-synthetic\n"
        "    hooks: []\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: check-no-datadir-paths\n"
        "        name: Block private career-hunter data\n"
        "        entry: python3 scripts/check.py\n"
        "        language: system\n"
    )
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "tracked.txt").write_text("synthetic\n")
    ok(repo, "add", ".")
    ok(repo, "commit", "-q", "-m", "initial")
    return repo


def hooks_only(env: dict[str, str]) -> Any:
    """Run pre-commit for real; pretend gitleaks passed."""

    def runner(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if cmd[0] == str(FAKE_GITLEAKS):
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.run(cmd, cwd=cwd, env=env, check=False)

    return runner


def fake(hooks: int = 0, leaks: int = 0) -> Any:
    calls: list[list[str]] = []

    def runner(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, leaks if cmd[0] == str(FAKE_GITLEAKS) else hooks)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def no_gitleaks_lookup(rev: str) -> Path:
    return FAKE_GITLEAKS


# --- The file list ------------------------------------------------------------------------


def test_lists_tracked_and_untracked_files_safely(repo: Path) -> None:
    (repo / "with space.txt").write_text("x\n")
    (repo / "new\nline.txt").write_text("x\n")
    (repo / "ignored.log").write_text("x\n")
    files, untracked = vt.tree_files(repo)
    assert {"tracked.txt", "with space.txt", "new\nline.txt", ".pre-commit-config.yaml"} <= set(
        files
    )
    assert "ignored.log" not in files
    assert untracked == 2


def test_a_deleted_tracked_file_is_skipped(repo: Path) -> None:
    (repo / "tracked.txt").unlink()
    files, _ = vt.tree_files(repo)
    assert "tracked.txt" not in files


# --- Pass and fail ------------------------------------------------------------------------


def test_passes_only_when_both_steps_pass(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = fake()
    assert vt.verify(repo, runner, no_gitleaks_lookup) == 0
    out = capsys.readouterr().out
    assert "files checked by every hook and by gitleaks" in out
    hooks_cmd, leaks_cmd = runner.calls
    assert hooks_cmd[1:5] == ["-m", "pre_commit", "run", "--files"]
    assert "tracked.txt" in hooks_cmd
    assert leaks_cmd[1] == "dir" and "--redact" in leaks_cmd


@pytest.mark.parametrize(
    ("hooks", "leaks", "message"),
    [(1, 0, "pre-commit hooks"), (0, 1, "gitleaks"), (1, 1, "pre-commit hooks and gitleaks")],
)
def test_fails_when_either_step_fails(
    repo: Path, capsys: pytest.CaptureFixture[str], hooks: int, leaks: int, message: str
) -> None:
    assert vt.verify(repo, fake(hooks, leaks), no_gitleaks_lookup) == 1
    assert f"FAILED: {message}" in capsys.readouterr().err


def test_an_empty_tree_fails_loudly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    ok(empty, "init", "-q")
    assert vt.verify(empty, fake(), no_gitleaks_lookup) == 2
    assert "file list is empty" in capsys.readouterr().err


def test_a_missing_gitleaks_build_fails_loudly(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "pre-commit-home"
    home.mkdir()
    monkeypatch.setenv("PRE_COMMIT_HOME", str(home))
    assert vt.verify(repo, fake()) == 2
    assert "no hook cache" in capsys.readouterr().err

    db = sqlite3.connect(home / "db.db")
    db.execute("CREATE TABLE repos (repo TEXT, ref TEXT, path TEXT)")
    db.execute("INSERT INTO repos VALUES (?, 'v0.0.0-synthetic', ?)", (vt.GITLEAKS_REPO, str(home)))
    db.commit()
    db.close()
    assert vt.verify(repo, fake()) == 2
    assert "hasn't been built" in capsys.readouterr().err


def test_the_repo_pins_the_gitleaks_rev() -> None:
    assert vt.pinned_gitleaks_rev(ROOT) == "v8.30.1"


# --- End to end: planted problems in untracked files --------------------------------------


def test_a_planted_data_folder_path_in_an_untracked_file_fails(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = load("check_no_datadir_paths").DEFAULT_DATA_DIR
    (repo / "notes.md").write_text(f"see {data_dir}/seed.yaml\n")  # built here, never in source
    env = {**os.environ, "PRE_COMMIT_HOME": str(tmp_path / "pc-home")}
    assert vt.verify(repo, hooks_only(env), no_gitleaks_lookup) == 1
    assert "FAILED: pre-commit hooks" in capsys.readouterr().err


def test_a_planted_fake_secret_in_an_untracked_file_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        gitleaks = vt.find_gitleaks(vt.pinned_gitleaks_rev(ROOT))
    except vt.VerifyError as exc:
        pytest.skip(f"needs the gitleaks build pre-commit makes locally: {exc}")
    # An AWS-access-key-shaped string, assembled at runtime so no source file contains one.
    fake_key = "AKIA" + "".join(
        secrets.choice(string.ascii_uppercase + "234567") for _ in range(16)
    )
    (repo / "scratch.env.txt").write_text(f"aws_access_key_id = {fake_key}\n")

    def leaks_only(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if cmd[0] == str(gitleaks):
            return subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True)
        return subprocess.CompletedProcess(cmd, 0)

    assert vt.verify(repo, leaks_only, lambda rev: gitleaks) == 1
    captured = capsys.readouterr()
    assert "FAILED: gitleaks" in captured.err
    assert fake_key not in captured.out + captured.err
