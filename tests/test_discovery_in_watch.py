"""Discovery runs inside the watch agent (#40). Synthetic config; no sockets, no sleeps."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import cli, db, discover
from careerhunter.platform_checks import SystemProbe


class Talk:
    """A SearXNG that answers every query with no results."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, **kw: object) -> tuple[int, bytes]:
        self.calls.append(url)
        assert kw.get("budget") is not None  # the pass's share of the run budget
        return 200, json.dumps({"results": []}).encode()


@pytest.fixture
def home(data_env: Path, probe: SystemProbe, monkeypatch: pytest.MonkeyPatch) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    path = data_env / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["watchlist"] = []
    doc["discovery"]["searxng_url"] = "http://127.0.0.1:8888/search"
    doc["discovery"]["interval_minutes"] = 60
    doc["discovery"]["queries_per_run"] = 2
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(cli, "config_today", lambda doc, now=None: dt.date(2026, 11, 2))
    return data_env


def _watch(probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    assert cli.main(["watch"], probe=probe) == 0
    return capsys.readouterr().err


def test_a_due_pass_runs_first_in_the_watch_run(
    home: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    no_discovery_sleep: list[float],
) -> None:
    talk = Talk()
    monkeypatch.setattr(discover, "default_transport", talk)
    err = _watch(probe, capsys)
    assert len(talk.calls) == 2
    assert no_discovery_sleep == [5]
    assert err.splitlines()[0] == "discover: 0 queued, 0 postings found"
    assert (home / "logs" / "discover.success").is_file()
    assert "discover: 0 queued, 0 postings found" in (home / "logs" / "watch.log").read_text()


def test_a_pass_is_not_due_again_inside_the_interval(
    home: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    talk = Talk()
    monkeypatch.setattr(discover, "default_transport", talk)
    _watch(probe, capsys)
    stamp = (home / "logs" / "discover.success").read_text()
    moment = dt.datetime(2026, 11, 2, 9, 59, tzinfo=dt.UTC)  # 59 minutes later
    monkeypatch.setattr(discover, "_utc_now", lambda: moment)
    err = _watch(probe, capsys)
    assert len(talk.calls) == 2
    assert "discover:" not in err
    assert (home / "logs" / "discover.success").read_text() == stamp
    moment = dt.datetime(2026, 11, 2, 10, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(discover, "_utc_now", lambda: moment)
    _watch(probe, capsys)
    assert len(talk.calls) == 4


def test_discovery_off_sends_nothing(
    home: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = home / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["discovery"]["searxng_url"] = None
    path.write_text(yaml.safe_dump(doc))

    def refuse(url: str, **kw: object) -> Any:
        raise AssertionError(url)

    monkeypatch.setattr(discover, "default_transport", refuse)
    err = _watch(probe, capsys)
    assert "discover" not in err
    assert not (home / "logs" / "discover.success").exists()


def test_a_discovery_crash_is_isolated_and_the_boards_still_run(
    home: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kw: object) -> Any:
        raise RuntimeError("synthetic discovery bug")

    monkeypatch.setattr(discover, "run", broken)
    err = _watch(probe, capsys)
    assert "watch: discovery: RuntimeError; continuing" in err
    assert err.strip().endswith("watch: 0 boards fetched, 0 failed, 0 left")
    assert not (home / "logs" / "discover.success").exists()
    log = (home / "logs" / "watch.log").read_text()
    assert "RuntimeError: synthetic discovery bug" in log  # the traceback, in the log only
    assert "Traceback" not in err


def test_a_miss_inside_the_watch_run_still_counts_as_a_pass(
    home: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def down(url: str, **kw: object) -> Any:
        raise OSError("refused")

    monkeypatch.setattr(discover, "default_transport", down)
    err = _watch(probe, capsys)
    assert err.splitlines()[:2] == [discover.MISS, "discover: 0 queued, 0 postings found"]
    assert (home / "logs" / "discover.success").is_file()
    conn = db.connect(home / "search.db")
    failures = conn.execute("SELECT failures FROM discovery_state").fetchone()[0]
    conn.close()
    assert failures == 1
