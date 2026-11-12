"""scoring.timeout_seconds: how long one scoring call may take, and when one may start."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import cli, loopback, score
from careerhunter import watch as w
from careerhunter.config import scoring_timeout_seconds, validate_config
from careerhunter.platform_checks import SystemProbe
from careerhunter.score import Score
from tests.fakes import FakeClock, FakeServer, Reply, use_example_watchlist
from tests.test_watch import DAY, NAMES, Timed, entries, minutes, postings, three_boards

ROOT = Path(__file__).parents[1]


def _example() -> dict[str, Any]:
    doc = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert isinstance(doc, dict)
    return doc


@pytest.mark.parametrize("value", [None, 60, 300, 540])
def test_a_timeout_from_60_to_540_is_valid(value: int | None) -> None:
    doc = _example()
    doc["scoring"]["timeout_seconds"] = value
    assert validate_config(doc) == []
    assert scoring_timeout_seconds(doc) == (300 if value is None else value)


@pytest.mark.parametrize("value", [59, 541, 0, -1, True, 300.0, "300"])
def test_another_timeout_is_refused(value: object) -> None:
    doc = _example()
    doc["scoring"]["timeout_seconds"] = value
    assert validate_config(doc) == ["scoring.timeout_seconds must be an integer from 60 to 540"]


def test_the_default_is_300_seconds() -> None:
    assert scoring_timeout_seconds(None) == scoring_timeout_seconds({}) == 300


def test_the_transport_waits_as_long_as_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(Reply(200, b"{}"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    score.default_transport(score.DEFAULT_URL, b"{}", timeout=300.0)
    assert 299.0 < server.opened[0][3] <= 300.0
    score.default_transport(score.DEFAULT_URL, b"{}")
    assert score.TOTAL - 1 < server.opened[1][3] <= score.TOTAL


def test_a_call_starts_only_while_the_timeout_fits_in_the_budget(
    search: sqlite3.Connection,
) -> None:
    w.run(search, entries(*NAMES), today=DAY, fetcher=three_boards())  # three unscored rows
    clock = FakeClock()

    def scorer(text: Any) -> Score:
        clock.advance(100)
        return Score(llm_output="kept", gaps="[]", tier="strong")

    lines = w.run(
        search,
        entries(*NAMES),
        today=DAY,
        fetcher=Timed(three_boards().mapping, clock, cost=2),  # 6 minutes of fetching
        scorer=scorer,
        budget=minutes(clock, 10),
        config={"scoring": {"timeout_seconds": 200}},
    )
    # 240 s are left: one call (240 >= 200), then 140 s, too few for the next.
    assert [row["llm_output"] for row in postings(search)].count("kept") == 1
    assert "watch: scoring: run budget reached; 2 postings left for the next run" in lines


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    use_example_watchlist(data_env / "config.yaml")
    capsys.readouterr()
    return data_env


def test_watch_gives_the_scorer_the_configured_timeout(
    inited: Path, probe: SystemProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = inited / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.setdefault("scoring", {})["timeout_seconds"] = 420
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    seen: dict[str, Any] = {}

    def run(*_args: object, **kwargs: Any) -> list[str]:
        kwargs["scorer"](object())
        return []

    def local_score(posting: object, config: object, **kwargs: Any) -> Score:
        seen["timeout"] = kwargs["transport"].keywords["timeout"]
        return Score(llm_output="{}", gaps="[]", tier="strong")

    monkeypatch.setattr(cli.watch, "run", run)
    monkeypatch.setattr(cli, "score_posting", local_score)
    assert cli.main(["watch"], probe=probe) == cli.EXIT_OK
    assert seen == {"timeout": 420}
