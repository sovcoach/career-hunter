"""Running career-hunter as user LaunchAgents on the always-on Mac (ADR-008, ADR-016).

A *user* LaunchAgent, not a system daemon: the bot reads its token from the login
Keychain, which needs the logged-in user's session.

- The bot's `RunAtLoad` starts it at login. `KeepAlive: {SuccessfulExit: false}`
  restarts it whenever it fails, including a fail-closed refusal because the data
  drive isn't mounted yet. `ThrottleInterval: 60` makes that a retry once a minute,
  not a loop.
- The watch agent runs `careerhunter watch` once per
  `schedule.watcher_interval_minutes` and exits. It has no `KeepAlive`.
- launchd's own stdout and stderr go to /dev/null. Each command writes its own log,
  into the data folder's logs/, only after the storage guard has passed, so launchd
  never creates a log file under /Volumes while the drive is missing.
- A plist holds the program path and CAREERHUNTER_DATA_DIR, and nothing secret.
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

from careerhunter.config import DEFAULT_WATCH_INTERVAL_MINUTES, MIN_WATCH_INTERVAL_MINUTES

LABEL = "org.careerhunter.bot"
WATCH_LABEL = "org.careerhunter.watch"
Runner = Callable[[list[str]], tuple[int, str]]


def run_launchctl(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}"
    return proc.returncode, proc.stdout + proc.stderr


def launch_agents_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents"


def agent_plist_path(label: str, home: Path | None = None) -> Path:
    return launch_agents_dir(home) / f"{label}.plist"


def plist_path(home: Path | None = None) -> Path:
    return agent_plist_path(LABEL, home)


def watch_plist_path(home: Path | None = None) -> Path:
    return agent_plist_path(WATCH_LABEL, home)


def program() -> Path:
    """The careerhunter entry point of this installation (the venv's script)."""
    found = shutil.which("careerhunter")
    return Path(found) if found else Path(sys.executable).with_name("careerhunter")


def _quiet_paths() -> dict[str, str]:
    # launchd must not create logs itself. A missing drive would otherwise get a file
    # under /Volumes from launchd, before the storage guard can refuse to start.
    return {"StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}


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
            **_quiet_paths(),
        }
    )


def watch_plist(program_path: Path, data_dir: Path, minutes: int) -> bytes:
    """One watch run per interval. No KeepAlive, so a finished run waits for the next."""
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int)
        or minutes < MIN_WATCH_INTERVAL_MINUTES
    ):
        raise ServiceError(
            f"watcher_interval_minutes must be an integer >= {MIN_WATCH_INTERVAL_MINUTES}"
        )
    return plistlib.dumps(
        {
            "Label": WATCH_LABEL,
            "ProgramArguments": [str(program_path), "watch"],
            "EnvironmentVariables": {"CAREERHUNTER_DATA_DIR": str(data_dir)},
            "RunAtLoad": True,
            "StartInterval": minutes * 60,
            "ProcessType": "Background",
            **_quiet_paths(),
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


def status(
    runner: Runner | None = None,
    home: Path | None = None,
    *,
    label: str = LABEL,
) -> ServiceStatus:
    runner = runner or run_launchctl
    if not agent_plist_path(label, home).is_file():
        return ServiceStatus(installed=False)
    code, out = runner(["print", f"{_domain()}/{label}"])
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


def _install_one(path: Path, body: bytes, runner: Runner) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.chmod(path, 0o644)  # launchd requires the plist not be group- or world-writable
    code, out = runner(["bootstrap", _domain(), str(path)])
    if code != 0:
        path.unlink()
        raise ServiceError(f"launchctl bootstrap failed ({code}): {out.strip()[:200]}")


def install(
    data_dir: Path,
    *,
    program_path: Path | None = None,
    runner: Runner | None = None,
    home: Path | None = None,
    watch_interval_minutes: int = DEFAULT_WATCH_INTERVAL_MINUTES,
) -> list[Path]:
    """Install whichever of the bot and watch agents is missing. Both, if neither is."""
    runner = runner or run_launchctl
    binary = program_path or program()
    bot_path = plist_path(home)
    watch_path = watch_plist_path(home)
    if bot_path.exists() and watch_path.exists():
        raise ServiceError(
            f"{bot_path.name} and {watch_path.name} already exist; "
            "run: careerhunter service uninstall"
        )
    # Build both before writing so a bad interval leaves nothing new behind.
    bot_body = plist(binary, data_dir)
    watch_body = watch_plist(binary, data_dir, watch_interval_minutes)
    made: list[Path] = []
    if not bot_path.exists():
        _install_one(bot_path, bot_body, runner)
        made.append(bot_path)
    if not watch_path.exists():
        _install_one(watch_path, watch_body, runner)
        made.append(watch_path)
    return made


def uninstall(runner: Runner | None = None, home: Path | None = None) -> bool:
    """Stop both agents and remove their plists. False if neither was installed."""
    runner = runner or run_launchctl
    removed = False
    for label in (LABEL, WATCH_LABEL):
        path = agent_plist_path(label, home)
        if not path.exists():
            continue
        runner(["bootout", f"{_domain()}/{label}"])  # fine if it wasn't loaded
        path.unlink()
        removed = True
    return removed
