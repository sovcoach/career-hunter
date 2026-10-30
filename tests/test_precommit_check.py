"""Runs the real pre-commit check script against a throwaway git repo."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_no_datadir_paths.py"


def _default_data_dir() -> str:
    """Read DEFAULT_DATA_DIR from the hook itself.

    Paths to files inside the data directory are built at runtime, never written out in
    full here: the hook scans this file too, and it should keep catching them.
    """
    spec = importlib.util.spec_from_file_location("check_no_datadir_paths", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.DEFAULT_DATA_DIR)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path.resolve() / "repo"
    data = tmp_path.resolve() / "SyntheticData"
    repo.mkdir()
    data.mkdir()
    (data / "careerhunter.db").write_text("synthetic")
    git(repo, "init", "-q")
    return repo, data


def run_check(repo: Path, data: Path | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CAREERHUNTER_DATA_DIR"}
    if data is not None:
        env["CAREERHUNTER_DATA_DIR"] = str(data)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True
    )


def stage(repo: Path, name: str, content: str = "hello\n") -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    git(repo, "add", name)


def test_clean_commit_passes(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    stage(repo, "README.md", f"Data lives in {data} (the folder itself is fine to name).\n")
    stage(repo, "config.example.yaml")
    assert run_check(repo, data).returncode == 0


@pytest.mark.parametrize(
    "name",
    [
        "config.yaml",
        "sub/seed.yaml",
        "exclusions.yaml",
        ".env",
        ".env.local",
        "careerhunter.db",
        "x.sqlite3",
        "careerhunter.db-wal",
    ],
)
def test_private_filenames_blocked(setup: tuple[Path, Path], name: str) -> None:
    repo, data = setup
    stage(repo, name)
    result = run_check(repo, data)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr


def test_symlink_into_data_dir_blocked(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    (repo / "db-link").symlink_to(data / "careerhunter.db")
    git(repo, "add", "db-link")
    result = run_check(repo, data)
    assert result.returncode == 1
    assert "resolves into the data directory" in result.stderr


def test_relative_symlink_into_data_dir_blocked(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    (repo / "notes").symlink_to(Path("..") / data.name)
    git(repo, "add", "notes")
    assert run_check(repo, data).returncode == 1


def test_content_mentioning_data_file_blocked(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    stage(repo, "log.txt", f"wrote {data}/exports/ei_2026.pdf\n")
    result = run_check(repo, data)
    assert result.returncode == 1
    assert "mentions a file inside the data directory" in result.stderr


def test_unset_env_falls_back_to_default(setup: tuple[Path, Path]) -> None:
    repo, _ = setup
    stage(repo, "notes.md", "see " + _default_data_dir() + "/seed.yaml\n")
    assert run_check(repo, None).returncode == 1


def test_reported_path_stops_at_backslash(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    # Source-code text with an escape right after the path: the literal two characters
    # backslash + n follow ".pdf" in the staged file.
    stage(repo, "app.py", 'MSG = "wrote ' + str(data) + '/exports/a.pdf\\nnext"\n')
    result = run_check(repo, data)
    assert result.returncode == 1
    assert f"({data}/exports/a.pdf)" in result.stderr
