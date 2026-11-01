"""Running the bot as a launchd LaunchAgent on the always-on Mac (ADR-008, ADR-016).

A *user* LaunchAgent, not a system daemon: the bot reads its token from the login
Keychain, which needs the logged-in user's session.

- `RunAtLoad` starts it at login. `KeepAlive: {SuccessfulExit: false}` restarts it
  whenever it fails, including a fail-closed refusal because the data drive isn't
  mounted yet. `ThrottleInterval: 60` makes that a retry once a minute, not a loop.
- launchd's own stdout and stderr go to /dev/null. The bot writes its own log, into
  the data folder's logs/, only after the storage guard has passed, so launchd never
  creates a log file under /Volumes while the drive is missing.
- The plist holds the program path and CAREERHUNTER_DATA_DIR, and nothing secret.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LABEL = "org.careerhunter.bot"
Runner = Callable[[list[str]], tuple[int, str]]


def run_launchctl(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}"
    return proc.returncode, proc.stdout + proc.stderr


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def program() -> Path:
    """The careerhunter entry point of this installation (the venv's script)."""
    found = shutil.which("careerhunter")
    return Path(found) if found else Path(sys.executable).with_name("careerhunter")


def plist(program_path: Path, data_dir: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(program_path), "bot", "run"],
            "EnvironmentVariables": {"CAREERHUNTER_DATA_DIR": str(data_dir)},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 60,
            "ProcessType": "Background",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        }
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


@dataclass(frozen=True)
class ServiceStatus:
    installed: bool
    loaded: bool = False
    running: bool = False
    pid: int | None = None
    last_exit: str | None = None


def status(runner: Runner | None = None, home: Path | None = None) -> ServiceStatus:
    runner = runner or run_launchctl
    if not plist_path(home).is_file():
        return ServiceStatus(installed=False)
    code, out = runner(["print", f"{_domain()}/{LABEL}"])
    if code != 0:
        return ServiceStatus(installed=True)
    pid = re.search(r"^\s*pid = (\d+)", out, re.MULTILINE)
    exit_code = re.search(r"^\s*last exit code = (.+)$", out, re.MULTILINE)
    return ServiceStatus(
        installed=True,
        loaded=True,
        running=bool(re.search(r"^\s*state = running", out, re.MULTILINE)),
        pid=int(pid.group(1)) if pid else None,
        last_exit=exit_code.group(1).strip() if exit_code else None,
    )


class ServiceError(Exception):
    """A launchd step failed. The message is user-facing."""


def install(
    data_dir: Path,
    *,
    program_path: Path | None = None,
    runner: Runner | None = None,
    home: Path | None = None,
) -> Path:
    runner = runner or run_launchctl
    path = plist_path(home)
    if path.exists():
        raise ServiceError(f"{path} already exists; run: careerhunter service uninstall")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plist(program_path or program(), data_dir))
    os.chmod(path, 0o644)  # launchd requires the plist not be group- or world-writable
    code, out = runner(["bootstrap", _domain(), str(path)])
    if code != 0:
        path.unlink()
        raise ServiceError(f"launchctl bootstrap failed ({code}): {out.strip()[:200]}")
    return path


def uninstall(runner: Runner | None = None, home: Path | None = None) -> bool:
    """Stop the service and remove its plist. False if it wasn't installed."""
    runner = runner or run_launchctl
    path = plist_path(home)
    if not path.exists():
        return False
    runner(["bootout", f"{_domain()}/{LABEL}"])  # fine if it wasn't loaded
    path.unlink()
    return True
