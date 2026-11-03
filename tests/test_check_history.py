"""The history path check (#29), run against throwaway repos.

Paths that must fail are assembled at runtime from pieces, so no such path appears in
this file or in its commit: the check scans its own pull request too.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_history.py"
ALLOWLIST = "/Volumes/ExampleDrive\n/Users/example\n/Volumes/Fake\n"
HOME = "/" + "Users" + "/" + "someone" + "/project"
DRIVE = "/" + "Volumes" + "/" + "RealDrive" + "/data"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_history", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


check_history = load_check()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(repo: Path, name: str, text: str, message: str = "change") -> str:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "scripts").mkdir()
    commit(path, "scripts/history_path_allowlist.txt", ALLOWLIST, "allowlist")
    return path


def findings(repo: Path, base: str) -> list[str]:
    return [f.line() for f in check_history.scan(repo, base, "HEAD")[1]]


@pytest.mark.parametrize(
    "text",
    [
        "data in /Volumes/ExampleDrive/ExampleFolder/seed.yaml\n",
        "repo at /Users/example/ExampleFolder/career-hunter\n",
        'assert "/Volumes/" not in text and "/Users/" not in text\n',
        "boot volume /System/Volumes/Data\n",
        "a fake mount /Volumes/Fake\n",
        "placeholders like /Users/... or /Users/<name> in prose\n",
        'source with an escape: "/Volumes/Fake\\n/Users/example\\n"\n',
    ],
)
def test_placeholders_and_non_paths_pass(repo: Path, text: str) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "notes.md", text)
    assert findings(repo, base) == []


def test_a_local_path_in_an_added_line_fails(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    sha = commit(repo, "notes.md", f"see {HOME}/notes\n")
    length = len(f"{HOME}/notes")
    assert findings(repo, base) == [
        f"{sha[:7]} added line in notes.md: /Users/… ({length} characters)"
    ]


def test_a_local_path_in_a_removed_line_fails(repo: Path) -> None:
    commit(repo, "notes.md", f"see {DRIVE}\n")
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "notes.md", "see the data folder\n")
    assert [line.split(" ", 1)[1] for line in findings(repo, base)] == [
        f"removed line in notes.md: /Volumes/… ({len(DRIVE)} characters)"
    ]


def test_a_local_path_in_a_commit_message_fails(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "notes.md", "synthetic\n", f"docs: mention {HOME}")
    assert [line.split(" ", 1)[1] for line in findings(repo, base)] == [
        f"commit message: /Users/… ({len(HOME)} characters)"
    ]


def test_a_lookalike_of_a_placeholder_fails(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "notes.md", "/Volumes/ExampleDrive" + "Copy/data\n")
    assert len(findings(repo, base)) == 1


def test_a_file_url_is_still_a_local_path(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "notes.md", f"file://{HOME}\n")
    assert len(findings(repo, base)) == 1


def test_an_empty_range_passes(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    assert check_history.scan(repo, head, "HEAD") == (0, [])


def test_main_masks_what_it_found(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "notes.md", f"see {HOME}\n")
    monkeypatch.chdir(repo)
    assert check_history.main(["check_history.py", base]) == 1
    captured = capsys.readouterr()
    assert (
        "1 local path(s) not in scripts/history_path_allowlist.txt in 1 commit(s)" in captured.err
    )
    assert "someone" not in captured.err + captured.out


def test_main_passes_and_says_so(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "notes.md", "synthetic\n")
    monkeypatch.chdir(repo)
    assert check_history.main(["check_history.py", base, "HEAD"]) == 0
    assert "OK, no local paths in 1 commit(s)" in capsys.readouterr().out


def test_a_missing_allowlist_fails_closed(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "scripts" / "history_path_allowlist.txt").unlink()
    with pytest.raises(check_history.HistoryError, match="is missing"):
        check_history.scan(repo, base, "HEAD")


def test_an_unknown_base_fails_closed(repo: Path) -> None:
    with pytest.raises(check_history.HistoryError, match="git rev-list failed"):
        check_history.scan(repo, "0" * 40, "HEAD")


def test_a_shallow_clone_fails_closed(repo: Path, tmp_path: Path) -> None:
    commit(repo, "notes.md", "synthetic\n")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    with pytest.raises(check_history.HistoryError, match="shallow"):
        check_history.scan(shallow, "HEAD~0", "HEAD")


def test_main_explains_its_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_history.main(["check_history.py"]) == 1
    assert "usage: check_history.py BASE [HEAD]" in capsys.readouterr().err


def test_the_repo_allowlist_holds_the_documented_placeholders() -> None:
    entries = check_history.allowlist(Path(__file__).parents[1])
    assert "/Volumes/ExampleDrive" in entries
    assert "/Users/example" in entries
