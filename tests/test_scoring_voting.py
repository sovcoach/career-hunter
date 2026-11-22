"""scoring.voting (ADR-031): up to three draws vote on the alert decision, and a failed draw
is replaced once. Off, scoring is one draw, as before. A fake transport stands in for the
model; synthetic postings and replies only."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from careerhunter import score
from careerhunter.config import (
    DEFAULT_SEED,
    SPARE_SEEDS,
    VOTE_SEEDS,
    scoring_voting,
    validate_config,
)
from careerhunter.deadline import Deadline
from careerhunter.score import Cap, Score, ScoreError, Veto, alerts, score_posting
from tests.fakes import FakeClock, fit_reply, requirement
from tests.test_criteria_veto_watch import SLUG, _boards, _logged
from tests.test_rescore import OLD, Recorder, row, run
from tests.test_score import RUBRIC, posting, reply

HIGH = fit_reply()  # no gaps: high, alerts
# A core gap in the role's main function: low, no alert.
LOW = fit_reply(items=(requirement("Example function", core=True, main_function=True),))
BAD = "not json"  # does not parse


def _voting(on: object = True, **sampling: Any) -> dict[str, Any]:
    doc = copy.deepcopy(RUBRIC)
    doc["scoring"] = {"voting": on}
    if sampling:
        doc["scoring"]["sampling"] = sampling
    return doc


Step = str | ScoreError


def _scripted(*steps: Step) -> tuple[Callable[[str, bytes], tuple[int, bytes]], list[int]]:
    """A transport that answers each call with the next step, and records each seed."""
    seeds: list[int] = []
    left = list(steps)

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seeds.append(json.loads(payload)["seed"])
        step = left.pop(0)
        if isinstance(step, ScoreError):
            raise step
        return reply(step)

    return transport, seeds


def test_voting_is_off_by_default_and_only_true_turns_it_on() -> None:
    assert scoring_voting(RUBRIC) is False
    assert scoring_voting(_voting(False)) is False
    assert scoring_voting(_voting(True)) is True
    assert scoring_voting(_voting("yes")) is False
    assert scoring_voting(None) is False


@pytest.mark.parametrize("value", ["yes", 1, 0, "true", [], {}])
def test_a_voting_value_other_than_true_or_false_is_refused(value: object) -> None:
    assert "scoring.voting must be true or false" in validate_config(_voting(value))


@pytest.mark.parametrize("value", [True, False])
def test_true_and_false_are_accepted(value: bool) -> None:
    assert not [p for p in validate_config(_voting(value)) if "voting" in p]


@pytest.mark.parametrize("doc", [RUBRIC, _voting(False)])
def test_off_is_one_draw_with_the_same_bytes(doc: dict[str, Any]) -> None:
    sent: list[bytes] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        sent.append(payload)
        return reply(LOW)

    result = score_posting(posting(), doc, transport=transport)
    plain: list[bytes] = []

    def before(url: str, payload: bytes) -> tuple[int, bytes]:
        plain.append(payload)
        return reply(LOW)

    score_posting(posting(), RUBRIC, transport=before)
    assert sent == plain and len(sent) == 1
    assert json.loads(sent[0])["seed"] == DEFAULT_SEED
    assert result is not None and (result.draws, result.replaced) == (1, ())


def test_off_a_failed_draw_is_not_replaced() -> None:
    transport, seeds = _scripted(BAD)
    assert score_posting(posting(), RUBRIC, transport=transport) is None
    assert seeds == [DEFAULT_SEED]
    transport, seeds = _scripted(ScoreError("x", kind="timeout"))
    with pytest.raises(ScoreError):
        score_posting(posting(), RUBRIC, transport=transport)
    assert seeds == [DEFAULT_SEED]


@pytest.mark.parametrize("both", [HIGH, LOW])
def test_two_draws_that_agree_settle_it(both: str) -> None:
    transport, seeds = _scripted(both, both)
    result = score_posting(posting(), _voting(), transport=transport)
    assert seeds == [DEFAULT_SEED, VOTE_SEEDS[0]]
    assert result is not None and result.draws == 2 and result.replaced == ()
    assert alerts(result) is (both == HIGH)


@pytest.mark.parametrize(
    ("steps", "alerted", "tier"),
    [
        ((HIGH, LOW, HIGH), True, "high"),
        ((HIGH, LOW, LOW), False, "low"),
        ((LOW, HIGH, HIGH), True, "high"),
        ((LOW, HIGH, LOW), False, "low"),
    ],
)
def test_a_disagreement_takes_a_third_draw_and_the_majority_decides(
    steps: tuple[str, ...], alerted: bool, tier: str
) -> None:
    transport, seeds = _scripted(*steps)
    result = score_posting(posting(), _voting(), transport=transport)
    assert seeds == [DEFAULT_SEED, *VOTE_SEEDS]
    assert result is not None and result.draws == 3
    assert alerts(result) is alerted and result.tier == tier


def test_the_first_draw_that_matches_the_majority_is_the_one_stored() -> None:
    first = fit_reply(("Example tool A", "minor"))  # high, one minor gap
    third = fit_reply()  # high, no gaps
    transport, _ = _scripted(first, LOW, third)
    result = score_posting(posting(), _voting(), transport=transport)
    assert result is not None
    assert [g["name"] for g in json.loads(result.gaps)] == ["Example tool A"]


def test_the_first_draw_uses_the_configured_seed() -> None:
    transport, seeds = _scripted(HIGH, LOW, HIGH)
    score_posting(posting(), _voting(seed=7), transport=transport)
    assert seeds == [7, *VOTE_SEEDS]


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (BAD, "unparseable"),
        (ScoreError("x", kind="timeout"), "timeout"),
    ],
)
def test_a_failed_draw_is_replaced_once_by_the_next_spare_seed(failure: Step, kind: str) -> None:
    transport, seeds = _scripted(HIGH, failure, HIGH)
    result = score_posting(posting(), _voting(), transport=transport)
    assert seeds == [DEFAULT_SEED, VOTE_SEEDS[0], SPARE_SEEDS[0]]
    assert result is not None and alerts(result)
    assert result.draws == 2 and result.replaced == (kind,)


def test_each_replacement_takes_the_next_unused_spare_seed() -> None:
    timeout = ScoreError("x", kind="timeout")
    transport, seeds = _scripted(BAD, HIGH, timeout, LOW, LOW)
    result = score_posting(posting(), _voting(), transport=transport)
    # draw 1 fails, replaced; draw 2 fails, replaced; they disagree, so draw 3.
    assert seeds == [DEFAULT_SEED, SPARE_SEEDS[0], VOTE_SEEDS[0], SPARE_SEEDS[1], VOTE_SEEDS[1]]
    assert result is not None and not alerts(result) and result.draws == 3
    assert result.replaced == ("unparseable", "timeout")


def test_a_failed_replacement_fails_the_posting_as_one_draw_would() -> None:
    transport, seeds = _scripted(HIGH, BAD, BAD)
    assert score_posting(posting(), _voting(), transport=transport) is None
    assert seeds == [DEFAULT_SEED, VOTE_SEEDS[0], SPARE_SEEDS[0]]
    transport, seeds = _scripted(BAD, ScoreError("x", kind="timeout"))
    with pytest.raises(ScoreError) as raised:
        score_posting(posting(), _voting(), transport=transport)
    assert raised.value.kind == "timeout"
    assert seeds == [DEFAULT_SEED, SPARE_SEEDS[0]]


def test_an_unreachable_model_makes_one_call_and_fails_at_once() -> None:
    transport, seeds = _scripted(ScoreError("x"), HIGH, HIGH)
    with pytest.raises(ScoreError) as raised:
        score_posting(posting(), _voting(), transport=transport)
    assert raised.value.kind == "unreachable"
    assert seeds == [DEFAULT_SEED]


def test_a_refused_token_makes_one_call_and_fails_at_once() -> None:
    calls: list[bytes] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        calls.append(payload)
        return reply("", status=401)

    with pytest.raises(ScoreError) as raised:
        score_posting(posting(), _voting(), transport=transport)
    assert raised.value.kind == "unauthorized"
    assert len(calls) == 1


def test_an_unreachable_second_draw_is_not_replaced() -> None:
    transport, seeds = _scripted(HIGH, ScoreError("x"), HIGH)
    with pytest.raises(ScoreError) as raised:
        score_posting(posting(), _voting(), transport=transport)
    assert raised.value.kind == "unreachable"
    assert seeds == [DEFAULT_SEED, VOTE_SEEDS[0]]


def _timed(clock: FakeClock, cost: float, *steps: Step) -> tuple[Any, list[int]]:
    """A scripted transport whose every call takes `cost` seconds on `clock`."""
    transport, seeds = _scripted(*steps)

    def slow(url: str, payload: bytes) -> tuple[int, bytes]:
        clock.advance(cost)
        return transport(url, payload)

    return slow, seeds


# scoring.timeout_seconds is 300 by default: a draw needs that much of the run left.
@pytest.mark.parametrize(
    ("steps", "seeds_sent"),
    [
        ((HIGH, LOW, HIGH), [DEFAULT_SEED]),  # no second vote
        ((BAD, HIGH, HIGH), [DEFAULT_SEED]),  # no replacement
    ],
)
def test_no_extra_draw_starts_once_the_run_budget_is_spent(
    steps: tuple[Step, ...], seeds_sent: list[int]
) -> None:
    clock = FakeClock()
    transport, seeds = _timed(clock, 150.0, *steps)
    budget = Deadline(400.0, clock)  # 250 s left after the first draw: under 300
    with pytest.raises(ScoreError) as raised:
        score_posting(posting(), _voting(), transport=transport, budget=budget)
    assert raised.value.kind == "budget"
    assert seeds == seeds_sent


def test_a_third_draw_needs_the_budget_too() -> None:
    clock = FakeClock()
    transport, seeds = _timed(clock, 150.0, HIGH, LOW, HIGH)
    budget = Deadline(550.0, clock)  # 400 s left after one draw, 250 after two
    with pytest.raises(ScoreError) as raised:
        score_posting(posting(), _voting(), transport=transport, budget=budget)
    assert raised.value.kind == "budget"
    assert seeds == [DEFAULT_SEED, VOTE_SEEDS[0]]


def test_with_enough_budget_the_vote_runs_as_usual() -> None:
    clock = FakeClock()
    transport, seeds = _timed(clock, 150.0, HIGH, LOW, HIGH)
    budget = Deadline(1000.0, clock)
    result = score_posting(posting(), _voting(), transport=transport, budget=budget)
    assert result is not None and result.draws == 3
    assert seeds == [DEFAULT_SEED, *VOTE_SEEDS]


def test_one_draw_ignores_the_budget() -> None:
    clock = FakeClock()
    transport, seeds = _timed(clock, 150.0, HIGH)
    budget = Deadline(10.0, clock)
    assert score_posting(posting(), RUBRIC, transport=transport, budget=budget) is not None
    assert seeds == [DEFAULT_SEED]


def test_a_vote_out_of_budget_leaves_the_posting_for_the_next_run(
    search: sqlite3.Connection,
) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = ScoreError(score.OUT_OF_BUDGET, kind="budget")
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    assert _logged(lines, ": 'Coordinator' left for the next run (run budget reached)")
    text = "\n".join(lines)
    assert "model unreachable" not in text and "given up" not in text
    assert row(search, "Coordinator")["score_attempted_at"] is None  # its place is kept
    assert row(search, "Coordinator")["llm_output"] is None
    again = Recorder()
    run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=again, resume=OLD)
    assert [c.title for c in again.calls] == ["Coordinator"]  # scored on the next run


def _score(tier: str | None = "high", **fields: Any) -> Score:
    return Score(llm_output="{}", gaps="[]", tier=tier, **fields)


@pytest.mark.parametrize(
    ("given", "decision"),
    [
        (_score("high"), True),
        (_score("medium"), True),
        (_score("low"), False),
        (_score(None, excluded=True), False),
        (_score("high", veto=Veto(1, skip="criteria: 1. Example")), False),
        (_score("high", veto=Veto(1, "not-found")), True),  # a rejected veto doesn't count
        (_score("high", cap=Cap(2, level="low")), False),
        (_score("high", cap=Cap(2, level="medium")), True),
        (_score("high", cap=Cap(2, "not-found")), True),
    ],
)
def test_the_alert_decision_of_one_draw(given: Score, decision: bool) -> None:
    assert alerts(given) is decision


def test_a_replaced_draw_is_logged_in_the_run(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(
        llm_output="{}", gaps="[]", tier="high", draws=2, replaced=("timeout",)
    )
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    assert _logged(lines, ": 'Coordinator' draw replaced (timeout); 2 draws voted")
    assert row(search, "Coordinator")["gaps"] == "[]"


def test_no_replaced_line_without_a_replaced_draw(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(llm_output="{}", gaps="[]", tier="high", draws=2)
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    assert not any("replaced" in line for line in lines)


def test_two_replaced_draws_are_named_in_order(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(
        llm_output="{}", gaps="[]", tier="low", draws=3, replaced=("unparseable", "timeout")
    )
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    assert _logged(lines, ": 'Coordinator' draws replaced (unparseable, timeout); 3 draws voted")


def test_the_module_names_the_seeds() -> None:
    assert score.VOTE_SEEDS == VOTE_SEEDS == (20261122, 20261123)
    assert SPARE_SEEDS[0] == 20261124 and DEFAULT_SEED == 20261119
