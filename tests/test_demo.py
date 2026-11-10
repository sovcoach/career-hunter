"""`careerhunter demo` runs on synthetic data in memory: no files, no socket, no guard (#44)."""

from __future__ import annotations

import builtins
import os
import socket
import sqlite3
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, demo, storage_guard
from careerhunter.config import validate_config

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


class Watched(MutableMapping[str, str]):
    """os.environ, recording every key that is read."""

    def __init__(self, inner: MutableMapping[str, str]) -> None:
        self.inner = inner
        self.read: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.read.append(key)
        return self.inner[key]

    def __setitem__(self, key: str, value: str) -> None:
        self.inner[key] = value

    def __delitem__(self, key: str) -> None:
        del self.inner[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.inner)

    def __len__(self) -> int:
        return len(self.inner)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.read.append(key)
        return key in self.inner


@pytest.fixture
def sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[Any]]]:
    """Every way the demo could touch the outside world, recorded."""
    seen: dict[str, list[Any]] = {"writes": [], "connects": [], "sockets": [], "guard": []}
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(storage_guard.ENV_VAR, str(tmp_path / "Volumes" / "Missing" / "Data"))
    environ = Watched(os.environ)
    monkeypatch.setattr(os, "environ", environ)

    real_open = builtins.open
    real_os_open = os.open
    real_connect = sqlite3.connect

    def spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wax+"):
            seen["writes"].append(("open", file, mode))
        return real_open(file, mode, *args, **kwargs)

    def spy_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & WRITE_FLAGS:
            seen["writes"].append(("os.open", path, flags))
        return real_os_open(path, flags, *args, **kwargs)

    def spy_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen["connects"].append(database)
        return real_connect(database, *args, **kwargs)

    def no_socket(*args: Any, **kwargs: Any) -> Any:
        seen["sockets"].append(args)
        raise AssertionError("the demo opened a socket")

    def no_guard(*args: Any, **kwargs: Any) -> Any:
        seen["guard"].append(args)
        raise AssertionError("the demo called the storage guard")

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(storage_guard, "check", no_guard)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    yield seen
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert after == before, "the demo created or removed a file"
    assert storage_guard.ENV_VAR not in environ.read


def test_demo_writes_nothing_and_opens_no_socket(
    sealed: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
    no_network: list[str],
) -> None:
    assert cli.main(["demo"]) == cli.EXIT_OK
    out = capsys.readouterr()
    assert out.err == ""
    assert sealed["writes"] == []
    assert sealed["sockets"] == []
    assert sealed["guard"] == []
    assert sealed["connects"] and set(sealed["connects"]) == {":memory:"}
    assert no_network == []
    assert "REFUSING TO START" not in out.out


def test_demo_runs_without_the_data_folder_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(storage_guard.ENV_VAR, raising=False)
    assert cli.main(["demo"]) == cli.EXIT_OK
    assert "== watch run ==" in capsys.readouterr().out


def test_demo_output_is_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    lines: list[str] = []
    assert demo.run(lines.append) == 0
    again: list[str] = []
    assert demo.run(again.append) == 0
    assert lines == again
    text = "\n".join(lines)
    assert "watch: Example Staffing: no ats_type; skipped" in text
    assert "'Senior Director, Operations' skipped (seniority)" in text
    assert "'Night Dispatcher' skipped (shift_pattern)" in text
    assert "'Customer Success Specialist' skipped (gap_tier)" in text
    assert "watch: 2 boards fetched, 0 failed, 0 left" in text
    assert "New postings: 3" in text
    assert "1. Example Robotics — Operations Coordinator" in text
    assert "Example City | Lane: operations | Fit: High | 1 gap" in text
    assert "Remote - Example Region | Lane: operations | Fit: Medium | 2 gaps" in text
    assert "Example City | Off-lane | Fit: High | 0 gaps" in text
    assert "forklift certificate" not in text.split("== Applied ==")[0].split("digest")[1]
    assert "  employer: Example Robotics" in text
    assert "  method: Company site" in text
    assert "  current_status: applied" in text
    assert all(line == line.rstrip() for line in lines)
    assert capsys.readouterr().out == ""


def test_demo_config_is_valid_and_synthetic() -> None:
    assert validate_config(demo.CONFIG) == []
    flat = repr(demo.CONFIG) + repr(demo.BOARDS)
    for word in ("Example", "Sample", "synthetic"):
        assert word in flat
    assert "/Volumes/" not in flat
    assert "/Users/" not in flat


def test_unknown_boards_answer_404() -> None:
    assert demo.board_fetcher("https://example.com/other").status == 404
