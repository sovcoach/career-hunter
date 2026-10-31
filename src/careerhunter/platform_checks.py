"""Thin wrappers around macOS system tools so the guards can be tested with fakes.

Every function here only reads system state. None of them change anything.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any


class SystemProbe:
    """Reads the facts the storage guard needs. Tests substitute a fake."""

    def is_macos(self) -> bool:
        return sys.platform == "darwin"

    def ismount(self, path: Path) -> bool:
        return os.path.ismount(path)

    def st_dev(self, path: Path) -> int | None:
        try:
            return os.stat(path).st_dev
        except OSError:
            return None

    def diskutil_info(self, target: str) -> dict[str, Any] | None:
        """Return `diskutil info -plist <target>` as a dict, or None if it can't be read."""
        try:
            out = subprocess.run(
                ["/usr/sbin/diskutil", "info", "-plist", target],
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout
            data = plistlib.loads(out)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
            return None
        return data if isinstance(data, dict) else None

    def filevault_on(self) -> bool | None:
        """True/False from `fdesetup status`, None if it can't be determined."""
        try:
            out = subprocess.run(
                ["/usr/bin/fdesetup", "status"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if "FileVault is On" in out:
            return True
        if "FileVault is Off" in out:
            return False
        return None

    def git_toplevel(self, path: Path) -> Path | None:
        """The git work tree containing `path`, or None if it isn't in one."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return Path(proc.stdout.strip()).resolve()
