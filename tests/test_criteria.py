"""search.criteria_file: free-text scoring criteria from a file in the data folder.

Synthetic criteria only. The model, the boards and Telegram are fakes; no network.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import cli, criteria, db, score, search_store, watch
from careerhunter.config import validate_config
from careerhunter.filters import PostingText
from careerhunter.platform_checks import SystemProbe
from careerhunter.score import Score
from careerhunter.storage_guard import DataDir
from tests import test_bot
from tests.fakes import use_example_watchlist
from tests.test_bot import Env
from tests.test_score import RUBRIC
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

env = test_bot.env  # the bot fixture, shared with test_bot
SYNTHETIC = "Prefers synthetic operations roles; a missing forklift licence is minor."


def _doc(value: object = "criteria.md") -> dict[str, Any]:
    doc = copy.deepcopy(RUBRIC)
    doc["search"]["criteria_file"] = value
    return doc


def _write(root: Path, text: str | bytes, name: str = "criteria.md", mode: int = 0o600) -> Path:
    path = root / name
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    path.chmod(mode)
    return path


# --- reading the file ----------------------------------------------------------------------


def test_unset_means_no_criteria(datadir: DataDir) -> None:
    assert criteria.load(datadir.root, RUBRIC) is None
    assert criteria.load(datadir.root, _doc(None)) is None


def test_a_file_in_the_data_folder_is_read(datadir: DataDir) -> None:
    _write(datadir.root, f"\n{SYNTHETIC}\n\n")
    found = criteria.load(datadir.root, _doc())
    assert found == criteria.Criteria(SYNTHETIC, len(SYNTHETIC))


def test_a_symlink_that_stays_inside_is_followed(datadir: DataDir) -> None:
    (datadir.root / "notes").mkdir(mode=0o700)
    _write(datadir.root / "notes", SYNTHETIC, "real.md")
    (datadir.root / "criteria.md").symlink_to(datadir.root / "notes" / "real.md")
    found = criteria.load(datadir.root, _doc())
    assert found is not None and found.text == SYNTHETIC


@pytest.mark.parametrize("how", ["symlink", "dotdot"])
def test_a_file_outside_the_data_folder_is_refused(
    datadir: DataDir, tmp_path_factory: pytest.TempPathFactory, how: str
) -> None:
    outside = _write(tmp_path_factory.mktemp("elsewhere"), SYNTHETIC, "outside.md")
    if how == "symlink":
        (datadir.root / "criteria.md").symlink_to(outside)
        value = "criteria.md"
    else:
        value = os.path.relpath(outside, datadir.root)
        assert value.startswith("..")
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc(value))
    assert caught.value.reason == "outside"


@pytest.mark.parametrize(
    ("content", "mode", "reason"),
    [
        (SYNTHETIC, 0o644, "loose"),
        (b"\xff\xfe synthetic", 0o600, "not-utf8"),
        ("   \n\n", 0o600, "empty"),
        ("x" * (criteria.MAX_CHARS + 1), 0o600, "too-long"),
        (b"x" * (criteria.MAX_BYTES + 1), 0o600, "too-long"),
    ],
)
def test_a_file_that_cannot_be_used_is_refused(
    datadir: DataDir, content: str | bytes, mode: int, reason: str
) -> None:
    _write(datadir.root, content, mode=mode)
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc())
    assert caught.value.reason == reason


def test_the_cap_is_inclusive_and_the_size_is_known(datadir: DataDir) -> None:
    _write(datadir.root, "y" * criteria.MAX_CHARS)
    found = criteria.load(datadir.root, _doc())
    assert found is not None and found.chars == criteria.MAX_CHARS
    _write(datadir.root, "y" * (criteria.MAX_CHARS + 5))
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc())
    assert caught.value.chars == criteria.MAX_CHARS + 5


def test_a_missing_file_or_a_folder_is_refused(datadir: DataDir) -> None:
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc())
    assert caught.value.reason == "missing"
    (datadir.root / "folder").mkdir(mode=0o700)
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc("folder"))
    assert caught.value.reason == "unreadable"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("/Volumes/ExampleDrive/criteria.md", "must be relative to the data folder"),
        ("~/criteria.md", "must be relative to the data folder"),
        ("", "must be a path of 1 to 200 characters"),
        ("x" * 201, "must be a path of 1 to 200 characters"),
        (" criteria.md", "no spaces at either end"),
        ("criteria\n.md", "no control characters"),
        (7, "must be a path of 1 to 200 characters"),
    ],
)
def test_config_refuses_a_bad_value(value: object, message: str) -> None:
    problems = validate_config(_doc(value))
    assert [p for p in problems if p.startswith("search.criteria_file")] == [
        p for p in problems if message in p
    ]
    assert any(message in p for p in problems)


def test_config_accepts_a_relative_path_or_null() -> None:
    assert not [p for p in validate_config(_doc()) if "criteria_file" in p]
    assert not [p for p in validate_config(_doc(None)) if "criteria_file" in p]


# --- the scoring request -------------------------------------------------------------------


def _request(stated: str | None) -> dict[str, Any]:
    seen: list[bytes] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen.append(payload)
        reply = {"choices": [{"message": {"content": '{"gaps": [], "requirements": []}'}}]}
        return 200, json.dumps(reply).encode()

    posting = PostingText("Example Robotics", "Operations Coordinator", "Example City", None)
    score.score_posting(posting, RUBRIC, transport=transport, criteria=stated)
    return dict(json.loads(seen[0]))


def test_unset_criteria_add_nothing_to_the_request() -> None:
    body = _request(None)
    assert "criteria" not in json.loads(body["messages"][1]["content"])
    assert "criteria" not in body["messages"][0]["content"]


def test_criteria_go_in_the_request_with_the_instruction() -> None:
    body = _request(SYNTHETIC)
    assert json.loads(body["messages"][1]["content"])["criteria"] == SYNTHETIC
    system = body["messages"][0]["content"]
    assert system.endswith(score.CRITERIA_INSTRUCTION)
    assert "minor or core" in score.CRITERIA_INSTRUCTION


# --- the watcher ---------------------------------------------------------------------------


def _run(search: sqlite3.Connection, problem: str | None, calls: list[str]) -> list[str]:
    def scorer(posting: PostingText) -> Score:
        calls.append(posting.title)
        return Score(llm_output="model text", gaps="[]", tier="strong")

    board = url("greenhouse", "examplerobotics")
    return watch.run(
        search,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today=DAY,
        fetcher=Scripted({board: fetched(one_greenhouse("examplerobotics"))}),
        config=copy.deepcopy(RUBRIC),
        scorer=scorer,
        criteria_problem=problem,
    )


def _criteria_health(search: sqlite3.Connection) -> search_store.Health:
    [health] = [h for h in search_store.component_health(search) if h.component == "criteria"]
    return health


def test_a_bad_criteria_file_holds_scoring_and_is_recorded(search: sqlite3.Connection) -> None:
    calls: list[str] = []
    lines = _run(search, "too-long", calls)
    assert calls == []
    assert (
        "watch: scoring: criteria file not usable (it is over the 4,000-character cap); "
        "left for the next run"
    ) in lines
    health = _criteria_health(search)
    assert (health.failures, health.last_failure, health.held) == (1, "too-long", 1)
    _run(search, None, calls)
    assert calls == ["Operations Coordinator"]
    assert _criteria_health(search).failures == 0


# --- the bot -------------------------------------------------------------------------------


def _record(env: Env, failed: str | None) -> None:
    conn = db.connect(env.datadir.search_db_path)
    try:
        db.migrate(conn, "search")
        with db.transaction(conn):
            search_store.record_component(
                conn, "criteria", failed=failed, stamp="2026-10-28T15:00:00Z"
            )
    finally:
        conn.close()


def test_the_bot_says_why_scoring_is_held_after_one_run(env: Env) -> None:
    _record(env, "loose")
    env.bot.send_health()
    env.bot.send_health()
    _record(env, None)
    env.bot.send_health()
    assert [t for t in env.api.texts() if t.startswith("Health:")] == [
        "Health: scoring is held because the criteria file can't be used: other users can "
        "read it (run: chmod 600 on it) (search.criteria_file; see careerhunter doctor). "
        "0 postings are waiting to be scored.",
        "Health: the criteria file can be used again; scoring resumes.",
    ]


# --- doctor --------------------------------------------------------------------------------


def _doctor(datadir: DataDir, doc: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> str:
    cli._doctor_criteria(cli._Report(), datadir, doc)
    return capsys.readouterr().out.strip()


def test_doctor_reports_unset_set_and_over_the_cap(
    datadir: DataDir, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _doctor(datadir, RUBRIC, capsys) == (
        "criteria        --  not set (optional: search.criteria_file)"
    )
    _write(datadir.root, SYNTHETIC)
    assert _doctor(datadir, _doc(), capsys) == (
        f"criteria        OK  {len(SYNTHETIC)} of 4,000 characters; sent with each scoring call"
    )
    _write(datadir.root, "z" * 4_100)
    assert _doctor(datadir, _doc(), capsys) == (
        "criteria        WARN over the 4,000-character cap (4,100 characters); scoring is held"
    )
    _write(datadir.root, SYNTHETIC, mode=0o640)
    assert _doctor(datadir, _doc(), capsys) == (
        "criteria        BAD other users can read it (run: chmod 600 on it); scoring is held"
    )
    assert str(datadir.root) not in capsys.readouterr().out


# --- edges of reading ----------------------------------------------------------------------


def test_an_absolute_value_that_skipped_validation_is_refused(datadir: DataDir) -> None:
    _write(datadir.root, SYNTHETIC)
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc(str(datadir.root / "criteria.md")))
    assert caught.value.reason == "outside"


def test_a_symlink_loop_is_unreadable(datadir: DataDir) -> None:
    (datadir.root / "criteria.md").symlink_to(datadir.root / "criteria.md")
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc())
    assert caught.value.reason == "unreadable"


@pytest.mark.parametrize(
    ("error", "reason"), [(FileNotFoundError(), "missing"), (PermissionError(), "unreadable")]
)
def test_a_file_that_goes_away_or_cannot_be_opened(
    datadir: DataDir, monkeypatch: pytest.MonkeyPatch, error: OSError, reason: str
) -> None:
    _write(datadir.root, SYNTHETIC)

    def refuse(*_args: object) -> int:
        raise error

    monkeypatch.setattr(criteria.os, "open", refuse)
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc())
    assert caught.value.reason == reason


def test_a_read_that_fails_is_unreadable(datadir: DataDir, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(datadir.root, SYNTHETIC)

    def broken(fd: int) -> bytes:
        raise OSError("synthetic")

    monkeypatch.setattr(criteria, "_read", broken)
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.load(datadir.root, _doc())
    assert caught.value.reason == "unreadable"


# --- careerhunter watch reads the file once and hands it on --------------------------------


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    use_example_watchlist(data_env / "config.yaml")
    capsys.readouterr()
    return data_env


def _set_criteria(root: Path) -> None:
    path = root / "config.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["search"]["criteria_file"] = "criteria.md"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("mode", "problem", "sent"), [(0o600, None, SYNTHETIC), (0o644, "loose", None)]
)
def test_watch_sends_the_criteria_or_holds_scoring(
    inited: Path,
    probe: SystemProbe,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    problem: str | None,
    sent: str | None,
) -> None:
    _set_criteria(inited)
    _write(inited, SYNTHETIC, mode=mode)
    seen: dict[str, Any] = {}

    def run(*_args: object, **kwargs: Any) -> list[str]:
        seen["problem"] = kwargs["criteria_problem"]
        kwargs["scorer"](object())
        return []

    def local_score(posting: object, config: object, **kwargs: Any) -> Score:
        seen["criteria"] = kwargs["criteria"]
        return Score(llm_output="{}", gaps="[]", tier="strong")

    monkeypatch.setattr(cli.watch, "run", run)
    monkeypatch.setattr(cli, "score_posting", local_score)
    assert cli.main(["watch"], probe=probe) == cli.EXIT_OK
    assert seen == {"problem": problem, "criteria": sent}
    assert SYNTHETIC not in (inited / "logs" / "watch.log").read_text()
