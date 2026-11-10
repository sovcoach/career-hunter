"""The launchd service: plist contents, install/uninstall, and doctor's health checks."""

from __future__ import annotations

import datetime as dt
import plistlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, service
from tests.conftest import ACTIVE, DOCTOR_NOW, FakeProbe, set_claims

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
    paths = service.install(
        tmp_path / "Data", program_path=Path("/venv/bin/careerhunter"), runner=run
    )
    assert paths == [service.plist_path(), service.watch_plist_path()]
    assert not service.discover_plist_path().exists()
    assert all(path.is_file() and path.stat().st_mode & 0o777 == 0o644 for path in paths)
    assert [call[0] for call in run.calls] == ["bootstrap", "bootstrap"]
    assert service.status(run) == service.ServiceStatus(True, True, True, 4242, "(never exited)")
    with pytest.raises(service.ServiceError, match="already exist"):
        service.install(tmp_path / "Data", runner=run)
    assert service.uninstall(run) is True
    assert not service.plist_path().exists()
    assert not service.watch_plist_path().exists()
    assert [call[0] for call in run.calls[-2:]] == ["bootout", "bootout"]
    assert service.uninstall(run) is False
    assert service.status(run) == service.ServiceStatus(installed=False)


def test_a_failed_bootstrap_leaves_nothing_behind(tmp_path: Path) -> None:
    with pytest.raises(service.ServiceError, match="bootstrap failed"):
        service.install(tmp_path, program_path=Path("/x"), runner=fake_launchctl("denied", 5))
    assert not service.plist_path().exists()
    assert not service.watch_plist_path().exists()


def test_watch_plist_runs_once_per_interval(tmp_path: Path) -> None:
    doc = plistlib.loads(service.watch_plist(Path("/venv/bin/careerhunter"), tmp_path / "Data", 15))
    assert doc["Label"] == "org.careerhunter.watch"
    assert doc["ProgramArguments"] == ["/venv/bin/careerhunter", "watch"]
    assert doc["EnvironmentVariables"] == {"CAREERHUNTER_DATA_DIR": str(tmp_path / "Data")}
    assert doc["StartInterval"] == 900 and doc["RunAtLoad"] is True
    assert "KeepAlive" not in doc and "ThrottleInterval" not in doc
    assert doc["StandardOutPath"] == doc["StandardErrorPath"] == "/dev/null"
    assert "token" not in str(doc).lower()
    with pytest.raises(service.ServiceError, match="integer >= 15"):
        service.watch_plist(Path("/x"), tmp_path, 14)


def test_install_adds_the_watch_agent_when_the_bot_is_already_there(tmp_path: Path) -> None:
    service.install(
        tmp_path / "Data",
        program_path=Path("/venv/bin/careerhunter"),
        runner=fake_launchctl(),
    )
    service.watch_plist_path().unlink()
    run = fake_launchctl()
    made = service.install(
        tmp_path / "Data",
        program_path=Path("/venv/bin/careerhunter"),
        runner=run,
        watch_interval_minutes=15,
    )
    assert made == [service.watch_plist_path()]
    assert service.plist_path().is_file()
    assert [call[0] for call in run.calls] == ["bootstrap"]
    doc = plistlib.loads(service.watch_plist_path().read_bytes())
    assert doc["StartInterval"] == 900
    assert doc["ProgramArguments"] == ["/venv/bin/careerhunter", "watch"]


def test_a_failed_watch_bootstrap_keeps_the_bot(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        if args[0] == "bootstrap" and sum(call[0] == "bootstrap" for call in calls) == 2:
            return 5, "denied"
        return 0, ""

    with pytest.raises(service.ServiceError, match="bootstrap failed"):
        service.install(tmp_path, program_path=Path("/x"), runner=run)
    assert service.plist_path().is_file()
    assert not service.watch_plist_path().exists()


def test_service_command_reports_both_agents(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl())
    assert cli.main(["service", "install"], probe=probe) == 0
    installed = capsys.readouterr().out
    assert "org.careerhunter.bot.plist; the bot starts now and at every login" in installed
    assert "careerhunter watch runs now and every 60 minutes" in installed
    assert "org.careerhunter.watch.plist" in installed

    monkeypatch.setattr(service, "run_launchctl", _split_launchctl(STOPPED))
    assert cli.main(["service", "status"], probe=probe) == 0
    status = capsys.readouterr().out
    assert "bot: running (pid 4242)" in status
    assert "watch: loaded" in status
    assert "discovery: off" in status

    monkeypatch.setattr(service, "run_launchctl", fake_launchctl())
    assert cli.main(["service", "uninstall"], probe=probe) == 0
    assert capsys.readouterr().out == "uninstalled\n"
    assert cli.main(["service", "status"], probe=probe) == 0
    missing = capsys.readouterr().out
    assert "bot: not installed (run: careerhunter service install)" in missing
    assert "watch: not installed (run: careerhunter service install)" in missing
    assert cli.main(["service", "uninstall"], probe=probe) == 0
    assert capsys.readouterr().out == "not installed\n"


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
    code = cli.main(["doctor"], probe=probe, clock=lambda: DOCTOR_NOW)
    return code, capsys.readouterr().out


def test_doctor_without_the_service(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "telegram        --  telegram.chat_id not set" in out
    assert "bot service     --  not installed" in out
    assert "watch service   --  not installed" in out
    assert "watch           --  no successful run yet" in out
    assert "discovery       --  off" in out
    assert "discovery agent" not in out
    assert out.count("no successful run yet") == 1
    assert "logs/           OK  0 files, all owner-only" in out


def test_doctor_with_a_healthy_service(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl())
    (ready / "logs" / "bot.heartbeat").write_text(DOCTOR_NOW.isoformat())
    (ready / "logs" / "bot.heartbeat").chmod(0o600)
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "bot service     OK  running (pid 4242)" in out
    assert "bot heartbeat   OK  polled within the last 5 minutes" in out
    assert "watch service   OK  running (pid 4242)" in out
    assert "watch           --  no successful run yet" in out


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
        (ready / "logs" / "bot.heartbeat").write_text((DOCTOR_NOW - age).isoformat())
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


def _heartbeat(ready: Path) -> None:
    path = ready / "logs" / "bot.heartbeat"
    path.write_text(DOCTOR_NOW.isoformat())
    path.chmod(0o600)


def _stamp(ready: Path, age: dt.timedelta) -> None:
    moment = DOCTOR_NOW - age
    path = ready / "logs" / "watch.success"
    path.write_text(moment.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


def _split_launchctl(watch_output: str, watch_code: int = 0) -> Any:
    def run(args: list[str]) -> tuple[int, str]:
        if args and args[0] == "print" and args[-1].endswith(service.WATCH_LABEL):
            return watch_code, watch_output
        return 0, RUNNING

    return run


def test_doctor_reports_a_loaded_watch_and_a_fresh_success(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", _split_launchctl(STOPPED))
    _heartbeat(ready)
    _stamp(ready, dt.timedelta(minutes=12))
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "watch service   OK  loaded" in out
    assert "watch           OK  last success 12 min ago" in out
    assert "watch interval" not in out


def test_doctor_measures_every_age_against_its_clock(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same files read as stale once the injected clock moves on: no real clock."""
    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl())
    _heartbeat(ready)
    _stamp(ready, dt.timedelta(minutes=12))
    later = DOCTOR_NOW + dt.timedelta(hours=3)
    capsys.readouterr()
    code = cli.main(["doctor"], probe=probe, clock=lambda: later)
    out = capsys.readouterr().out
    assert code == cli.EXIT_ERROR
    assert "bot heartbeat   BAD last poll 180 min ago" in out
    assert "watch           BAD last success 192 min ago" in out


def test_doctor_flags_an_unreadable_success_stamp(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (ready / "logs" / "watch.success").write_text("not-a-time\n")
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "watch           BAD last success stamp is unreadable" in out


def test_doctor_flags_a_stale_watch(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", _split_launchctl(STOPPED))
    _heartbeat(ready)
    _stamp(ready, dt.timedelta(minutes=121))
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "watch           BAD last success 121 min ago" in out


def test_doctor_flags_a_watch_plist_that_is_not_loaded(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", _split_launchctl("", 1))
    _heartbeat(ready)
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "watch service   BAD installed but not loaded" in out


def test_doctor_warns_when_the_installed_interval_disagrees(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    service.install(ready, program_path=Path("/x"), runner=fake_launchctl())
    monkeypatch.setattr(service, "run_launchctl", _split_launchctl(STOPPED))
    _heartbeat(ready)
    _stamp(ready, dt.timedelta(0))
    path = ready / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["schedule"]["watcher_interval_minutes"] = 15
    path.write_text(yaml.safe_dump(doc))
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "watch interval  WARN plist has 3600s, config asks for 900s" in out


def _old_discovery_agent() -> Path:
    """The plist an earlier version installed for the separate discovery agent."""
    path = service.discover_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": service.DISCOVER_LABEL}))
    return path


def test_retiring_the_old_discovery_agent_boots_it_out_and_removes_it(tmp_path: Path) -> None:
    run = fake_launchctl()
    assert service.retire_discovery_agent(run) is None  # nothing to do
    assert run.calls == []
    path = _old_discovery_agent()
    assert service.retire_discovery_agent(run) == path
    assert not path.exists()
    assert run.calls == [["bootout", f"{service._domain()}/org.careerhunter.discover"]]
    failing = fake_launchctl(code=3)  # not loaded: the plist goes anyway
    _old_discovery_agent()
    assert service.retire_discovery_agent(failing) is not None
    assert not service.discover_plist_path().exists()


def test_install_never_adds_a_discovery_agent(tmp_path: Path) -> None:
    made = service.install(tmp_path, program_path=Path("/x"), runner=fake_launchctl())
    assert made == [service.plist_path(), service.watch_plist_path()]
    assert not service.discover_plist_path().exists()
    assert not hasattr(service, "discover_plist")


def _set_discovery(ready: Path, url: str | None, minutes: int | None = None) -> None:
    import yaml

    path = ready / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["discovery"]["searxng_url"] = url
    if minutes is not None:
        doc["discovery"]["interval_minutes"] = minutes
    path.write_text(yaml.safe_dump(doc))


def test_service_install_migrates_a_setup_with_the_old_discovery_agent(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_discovery(ready, "http://127.0.0.1:8888/search", 60)
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl())
    assert cli.main(["service", "install"], probe=probe) == 0
    old = _old_discovery_agent()
    capsys.readouterr()
    run = fake_launchctl()
    monkeypatch.setattr(service, "run_launchctl", run)
    assert cli.main(["service", "install"], probe=probe) == 0
    out = capsys.readouterr().out
    assert out == f"removed {old}; discovery now runs inside the watch agent\n"
    assert [call[0] for call in run.calls] == ["bootout"]  # the others are not touched
    assert not old.exists()
    assert service.plist_path().is_file() and service.watch_plist_path().is_file()
    assert cli.main(["service", "install"], probe=probe) != 0  # nothing left to migrate


def test_service_install_on_a_fresh_setup_with_the_old_agent(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _old_discovery_agent()
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl())
    assert cli.main(["service", "install"], probe=probe) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"removed {old}; discovery now runs inside the watch agent\n")
    assert "careerhunter watch runs now" in out


def test_service_status_says_where_discovery_runs(
    ready: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "run_launchctl", fake_launchctl(STOPPED))
    capsys.readouterr()
    assert cli.main(["service", "status"], probe=probe) == 0
    assert "discovery: off" in capsys.readouterr().out
    _set_discovery(ready, "http://127.0.0.1:8888/search")
    assert cli.main(["service", "status"], probe=probe) == 0
    assert "discovery: runs inside the watch agent" in capsys.readouterr().out
    _old_discovery_agent()
    assert cli.main(["service", "status"], probe=probe) == 0
    assert "discovery: the old separate agent is installed" in capsys.readouterr().out


def _discovery_stamp(ready: Path, age: dt.timedelta) -> None:
    moment = DOCTOR_NOW - age
    (ready / "logs" / "discover.success").write_text(moment.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


def test_doctor_says_discovery_is_off_until_the_url_is_set(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "discovery       --  off" in out
    _set_discovery(ready, "http://127.0.0.1:8888/search")
    code, out = doctor(probe, capsys)
    assert code == 0
    assert "discovery       --  no successful run yet" in out


def test_doctor_warns_when_the_old_discovery_agent_is_left_behind(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _old_discovery_agent()
    code, out = doctor(probe, capsys)
    assert code == 0
    assert (
        "discovery agent WARN the old separate agent is installed "
        "(run: careerhunter service install)"
    ) in out


def test_doctor_reports_a_fresh_discovery_stamp(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_discovery(ready, "http://127.0.0.1:8888/search")
    _discovery_stamp(ready, dt.timedelta(minutes=12))
    _code, out = doctor(probe, capsys)
    assert "discovery       OK  last success 12 min ago" in out


def test_doctor_flags_a_stale_discovery_stamp_by_the_longer_interval(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_discovery(ready, "http://127.0.0.1:8888/search", 60)  # the watch runs every 60
    _discovery_stamp(ready, dt.timedelta(minutes=119))
    _code, out = doctor(probe, capsys)
    assert "discovery       OK  last success 119 min ago" in out
    _discovery_stamp(ready, dt.timedelta(minutes=121))
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "discovery       BAD last success 121 min ago" in out
    _set_discovery(ready, "http://127.0.0.1:8888/search", 1440)
    _discovery_stamp(ready, dt.timedelta(minutes=2881))
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "discovery       BAD last success 2881 min ago" in out


def test_doctor_flags_an_unreadable_discovery_stamp(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_discovery(ready, "http://127.0.0.1:8888/search")
    (ready / "logs" / "discover.success").write_text("not-a-time\n")
    code, out = doctor(probe, capsys)
    assert code == cli.EXIT_ERROR
    assert "discovery       BAD last success stamp is unreadable" in out
