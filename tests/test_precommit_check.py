"""Runs the real pre-commit check script against a throwaway git repo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_no_datadir_paths.py"


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


def run_check(
    repo: Path, data: Path | None, files: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the hook. `files` are passed as arguments, the way pre-commit passes them."""
    env = {k: v for k, v in os.environ.items() if k != "CAREERHUNTER_DATA_DIR"}
    if data is not None:
        env["CAREERHUNTER_DATA_DIR"] = str(data)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(files or [])],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
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


@pytest.mark.parametrize("files", [None, ["clean.md"]], ids=["staged", "passed"])
def test_unset_env_fails_with_a_clear_message(
    setup: tuple[Path, Path], files: list[str] | None
) -> None:
    """No path is built into the hook, so without the variable it refuses, not guesses."""
    repo, _ = setup
    stage(repo, "clean.md", "nothing private here\n")
    result = run_check(repo, None, files)
    assert result.returncode == 1
    assert "CAREERHUNTER_DATA_DIR isn't set" in result.stderr
    assert "/Volumes/ExampleDrive/ExampleFolder" in result.stderr  # only a placeholder


def test_the_hook_holds_no_real_path() -> None:
    source = SCRIPT.read_text()
    assert "DEFAULT_DATA_DIR" not in source
    assert [line for line in source.splitlines() if "/Volumes/" in line] == [
        "  export CAREERHUNTER_DATA_DIR=/Volumes/ExampleDrive/ExampleFolder   (see docs/SETUP.md)"
    ]


def test_reported_path_stops_at_backslash(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    # Source-code text with an escape right after the path: the literal two characters
    # backslash + n follow ".pdf" in the staged file.
    stage(repo, "app.py", 'MSG = "wrote ' + str(data) + '/exports/a.pdf\\nnext"\n')
    result = run_check(repo, data)
    assert result.returncode == 1
    assert f"({data}/exports/a.pdf)" in result.stderr


# --- Two modes: no arguments = staged files (commit time); arguments = those files. ---


def leak(data: Path) -> str:
    return f"see {data}/exports/a.pdf\n"


def test_staged_mode_ignores_unstaged_files(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    (repo / "draft.md").write_text(leak(data))  # untracked, not staged
    stage(repo, "README.md")
    assert run_check(repo, data).returncode == 0


def test_passed_untracked_file_is_scanned(setup: tuple[Path, Path]) -> None:
    """The gap this mode closes: `pre-commit run --files` / `--all-files` really scan."""
    repo, data = setup
    (repo / "draft.md").write_text(leak(data))
    result = run_check(repo, data, ["draft.md"])
    assert result.returncode == 1
    assert "draft.md: mentions a file inside the data directory" in result.stderr


def test_passed_tracked_file_uses_working_tree(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    stage(repo, "notes.md")
    git(repo, "commit", "-q", "-m", "synthetic", "--no-verify")
    (repo / "notes.md").write_text(leak(data))  # modified, unstaged
    assert run_check(repo, data, ["notes.md"]).returncode == 1


def test_passed_staged_file_is_checked_as_staged(setup: tuple[Path, Path]) -> None:
    """At commit time the index is what gets recorded, so a staged file uses its blob."""
    repo, data = setup
    stage(repo, "notes.md", leak(data))
    (repo / "notes.md").write_text("cleaned up, but not re-staged\n")
    assert run_check(repo, data, ["notes.md"]).returncode == 1

    stage(repo, "other.md", "clean\n")
    (repo / "other.md").write_text(leak(data))  # unstaged leak; the staged blob is clean
    assert run_check(repo, data, ["other.md"]).returncode == 0


@pytest.mark.parametrize("name", ["config.yaml", "careerhunter.db", ".env"])
def test_passed_private_filenames_blocked(setup: tuple[Path, Path], name: str) -> None:
    repo, data = setup
    (repo / name).write_text("synthetic\n")
    assert run_check(repo, data, [name]).returncode == 1


def test_passed_symlink_into_data_dir_blocked(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    (repo / "db-link").symlink_to(data / "careerhunter.db")
    result = run_check(repo, data, ["db-link"])
    assert result.returncode == 1
    assert "resolves into the data directory" in result.stderr


def test_passed_paths_outside_repo_root_and_dirs(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    (repo / "sub").mkdir()
    (repo / "sub" / "leak.md").write_text(leak(data))
    # pre-commit passes repo-relative paths; absolute ones resolve the same way.
    assert run_check(repo, data, [str(repo / "sub" / "leak.md")]).returncode == 1
    # Directories and missing paths are ignored rather than crashing.
    assert run_check(repo, data, ["sub", "missing.md"]).returncode == 0


def test_clean_passed_files_pass(setup: tuple[Path, Path]) -> None:
    repo, data = setup
    (repo / "a.md").write_text(f"The data folder is {data} (naming the folder is fine).\n")
    (repo / "b.md").write_text("hello\n")
    assert run_check(repo, data, ["a.md", "b.md"]).returncode == 0


def test_git_is_hermetic(setup: tuple[Path, Path]) -> None:
    """The autouse hermetic_git fixture hides real git config and fixes the identity."""
    repo, _ = setup
    origins = subprocess.run(
        ["git", "config", "--show-origin", "--list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "global" not in origins
    assert "user.name" not in origins
    ident = subprocess.run(
        ["git", "var", "GIT_COMMITTER_IDENT"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ident.startswith("Synthetic Test <test@example.invalid>")
    stage(repo, "a.md")
    git(repo, "commit", "-q", "-m", "synthetic", "--no-verify")  # works with no real config
