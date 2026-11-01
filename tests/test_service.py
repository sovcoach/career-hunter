"""The launchd service: plist contents, install/uninstall, and doctor's health checks."""

from __future__ import annotations

import datetime as dt
import plistlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, service
from tests.conftest import ACTIVE, FakeProbe, set_claims

RUNNING = """gui/501/org.careerhunter.bot = {
\tstate = running
\tpid = 4242
\tlast exit code = (never exited)
}"""
STOPPED = """gui/501/org.careerhunter.bot = {
\tstate = not running
\tlast exit code = 1
}"""


def fake_launchctl(output: str = RUNNING, code: int = 0) -> Any:
    calls: list[list[str]] = []

    def run(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        return code, output

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_plist_contents(tmp_path: Path) -> None:
    doc = plistlib.loads(service.plist(Path("/venv/bin/careerhunter"), tmp_path / "Data"))
    assert doc["Label"] == "org.careerhunter.bot"
    assert doc["ProgramArguments"] == ["/venv/bin/careerhunter", "bot", "run"]
    assert doc["EnvironmentVariables"] == {"CAREERHUNTER_DATA_DIR": str(tmp_path / "Data")}
    assert doc["KeepAlive"] == {"SuccessfulExit": False}
    assert doc["ThrottleInterval"] == 60 and doc["RunAtLoad"] is True
    # launchd never writes logs itself, so it can't create files under an unmounted /Volumes.
    assert doc["StandardOutPath"] == doc["StandardErrorPath"] == "/dev/null"
    assert "token" not in str(doc).lower()


def test_install_status_uninstall(tmp_path: Path, launch_agents: Path) -> None:
    run = fake_launchctl()
    path = service.install(
        tmp_path / "Data", program_path=Path("/venv/bin/careerhunter"), runner=run
    )
    assert path == service.plist_path() and path.is_file()
    assert path.stat().st_mode & 0o777 == 0o644
    assert run.calls[0][0] == "bootstrap" and run.calls[0][-1] == str(path)
    assert service.status(run) == service.ServiceStatus(True, True, True, 4242, "(never exited)")
    with pytest.raises(service.ServiceError, match="already exists"):
        service.install(tmp_path / "Data", runner=run)
    assert service.uninstall(run) is True
    assert not path.exists() and run.calls[-1][0] == "bootout"
    assert service.uninstall(run) is False
    assert service.status(run) == service.ServiceStatus(installed=False)


def test_a_failed_bootstrap_leaves_nothing_behind(tmp_path: Path) -> None:
    with pytest.raises(service.ServiceError, match="bootstrap failed"):
        service.install(tmp_path, program_path=Path("/x"), runner=fake_launchctl("denied", 5))
    assert not service.plist_path().exists()


def test_status_of_a_stopped_service(tmp_path: Path) -> None:
    service.install(tmp_path, program_path=Path("/x"), runner=fake_launchctl())
    assert service.status(fake_launchctl(STOPPED)) == service.ServiceStatus(
        True, True, False, None, "1"
    )
    assert service.status(fake_launchctl("", 113)) == service.ServiceStatus(installed=True)


# --- doctor ------------------------------------------------------------------------------


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    assert cli.main(["migrate"], probe=probe) == 0
    return data_env


def doctor(probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    code = cli.main(["doctor"], probe=probe)
    return code, capsys.readouterr().out


def test_doctor_without_the_service(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "telegram        --  telegram.chat_id not set" in out
    assert "bot service     --  not installed" in out
    assert "logs/           OK  0 files, all owner-only" in out


def test_doctor_with_a_healthy_service(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl())
    (ready / "logs" / "bot.heartbeat").write_text(dt.datetime.now(dt.UTC).isoformat())
    (ready / "logs" / "bot.heartbeat").chmod(0o600)
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "bot service     OK  running (pid 4242)" in out
    assert "bot heartbeat   OK  polled within the last 5 minutes" in out


@pytest.mark.parametrize(
    ("launchctl", "heartbeat", "expected"),
    [
        (STOPPED, "fresh", "bot service     BAD installed but not running (last exit 1)"),
        (RUNNING, "stale", "bot heartbeat   BAD last poll 30 min ago"),
        (RUNNING, None, "bot heartbeat   BAD none yet"),
    ],
)
def test_doctor_flags_an_unhealthy_service(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    launchctl: str,
    heartbeat: str | None,
    expected: str,
) -> None:
    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl(launchctl))
    if heartbeat:
        age = dt.timedelta(minutes=30 if heartbeat == "stale" else 0)
        (ready / "logs" / "bot.heartbeat").write_text((dt.datetime.now(dt.UTC) - age).isoformat())
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert expected in out


def test_doctor_with_the_chat_set(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    import yaml

    path = ready / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["telegram"] = {"chat_id": 424242}
    path.write_text(yaml.safe_dump(doc))
    _, out = doctor(probe, capsys)
    assert "telegram        OK  chat set" in out
    assert "424242" not in out  # doctor doesn't print the chat id
    shutil.rmtree(ready / "logs")
    _, out = doctor(probe, capsys)
    assert "logs/           --  missing" in out
