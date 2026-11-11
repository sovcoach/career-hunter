"""The template watchlist is inert: `watch` makes no network call until it is edited (#44)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import cli, discover, embed
from careerhunter.config import discovery_enabled, validate_config
from careerhunter.platform_checks import SystemProbe
from tests.fakes import template_watchlist

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "config.example.yaml"


class Recorder:
    """Stands in for one client. Any call is recorded and fails the test."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(self.name)
        raise AssertionError(f"{self.name} was called")


def test_the_template_watchlist_is_empty_and_valid() -> None:
    doc = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert doc["watchlist"] == []
    assert validate_config(doc) == []
    assert discovery_enabled(doc) is False


def test_the_commented_examples_are_valid_once_uncommented() -> None:
    entries = template_watchlist()
    assert [e["name"] for e in entries] == [
        "Example Robotics",
        "Sample Logistics",
        "Example Staffing",
    ]
    assert {e.get("ats_type") for e in entries} == {"greenhouse", "lever", None}
    assert any("role_filters" in e for e in entries)
    assert any(e.get("agency") for e in entries)
    doc = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    doc["watchlist"] = entries
    assert validate_config(doc) == []


def test_the_workday_example_is_valid_once_uncommented() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    lines = [line[6:] for line in text.splitlines() if line.startswith("#   # ")]
    block = yaml.safe_load("\n".join(line for line in lines if not line.startswith("A ")))
    assert block == [
        {
            "name": "Example Freight",
            "ats_type": "workday",
            "ats_slug": "examplefreight",
            "ats_host": "wd5",
            "ats_site": "External",
        }
    ]
    doc = yaml.safe_load(text)
    doc["watchlist"] = block
    assert validate_config(doc) == []


def test_watch_on_the_template_triggers_no_fetch(
    data_env: Path,
    probe: SystemProbe,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_network: list[str],
) -> None:
    assert cli.main(["init"], probe=probe) == cli.EXIT_OK
    copied = (data_env / "config.yaml").read_text(encoding="utf-8")
    assert copied == TEMPLATE.read_text(encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(cli, "fetch_url", Recorder("fetch", calls))
    monkeypatch.setattr(cli, "post_json", Recorder("post_json", calls))
    monkeypatch.setattr(cli, "score_transport", Recorder("score", calls))
    monkeypatch.setattr(embed, "default_transport", Recorder("embed", calls))
    monkeypatch.setattr(discover, "default_transport", Recorder("discover", calls))
    monkeypatch.setattr(cli, "config_today", lambda doc, now=None: dt.date(2026, 11, 2))
    capsys.readouterr()
    assert cli.main(["watch"], probe=probe) == cli.EXIT_OK
    err = capsys.readouterr().err
    assert calls == []
    assert no_network == []
    assert "watch: 0 boards fetched, 0 failed, 0 left" in err
