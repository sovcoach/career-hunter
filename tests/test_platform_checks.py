"""SystemProbe parsing, with subprocess mocked. No real system tools are called."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from careerhunter import platform_checks
from careerhunter.platform_checks import SystemProbe


def fake_run(stdout: bytes | str = b"", returncode: int = 0, exc: Exception | None = None):  # type: ignore[no-untyped-def]
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if exc:
            raise exc
        if kwargs.get("check") and returncode:
            raise subprocess.CalledProcessError(returncode, argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, b"")

    return run


@pytest.mark.parametrize(
    ("output", "expected"),
    [("FileVault is On.\n", True), ("FileVault is Off.\n", False), ("weird", None)],
)
def test_filevault_parsing(monkeypatch: pytest.MonkeyPatch, output: str, expected: bool) -> None:
    monkeypatch.setattr(platform_checks.subprocess, "run", fake_run(output))
    assert SystemProbe().filevault_on() is expected


def test_filevault_tool_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_checks.subprocess, "run", fake_run(exc=FileNotFoundError()))
    assert SystemProbe().filevault_on() is None


def test_diskutil_plist_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = plistlib.dumps({"MountPoint": "/Volumes/Fake", "Encryption": True})
    monkeypatch.setattr(platform_checks.subprocess, "run", fake_run(payload))
    assert SystemProbe().diskutil_info("/Volumes/Fake") == {
        "MountPoint": "/Volumes/Fake",
        "Encryption": True,
    }


@pytest.mark.parametrize(
    "run",
    [fake_run(b"not a plist"), fake_run(returncode=1), fake_run(plistlib.dumps([1, 2]))],
)
def test_diskutil_failures_return_none(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    monkeypatch.setattr(platform_checks.subprocess, "run", run)
    assert SystemProbe().diskutil_info("/Volumes/Fake") is None


def test_git_toplevel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(platform_checks.subprocess, "run", fake_run(f"{tmp_path}\n"))
    assert SystemProbe().git_toplevel(tmp_path) == tmp_path.resolve()
    monkeypatch.setattr(platform_checks.subprocess, "run", fake_run("", returncode=128))
    assert SystemProbe().git_toplevel(tmp_path) is None
    monkeypatch.setattr(platform_checks.subprocess, "run", fake_run(exc=FileNotFoundError()))
    assert SystemProbe().git_toplevel(tmp_path) is None


def test_real_filesystem_helpers(tmp_path: Path) -> None:
    probe = SystemProbe()
    assert probe.ismount(Path("/"))
    assert not probe.ismount(tmp_path)
    assert probe.st_dev(tmp_path) is not None
    assert probe.st_dev(tmp_path / "missing") is None
    assert isinstance(probe.is_macos(), bool)
