"""scoring.sampling sets the sampling and seed of the scoring request; unset, nothing changes."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from careerhunter import score
from careerhunter.config import (
    DEFAULT_SAMPLING,
    DEFAULT_SEED,
    SAMPLING_RANGES,
    scoring_sampling,
    validate_config,
)
from careerhunter.filters import PostingText
from tests.test_score import RUBRIC

# The request's sampling as 0.11.1 sent it, byte for byte and in this order.
FIXED_BYTES = (
    b'"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, '
    b'"presence_penalty": 1.5, "repeat_penalty": 1.0, "seed": 20261119, '
)


def _posting() -> PostingText:
    return PostingText(
        company="Example Robotics",
        title="Operations Coordinator",
        location="Example City",
        work_model=None,
        description="A synthetic posting.",
    )


def _with(sampling: object) -> dict[str, Any]:
    doc = copy.deepcopy(RUBRIC)
    doc["scoring"] = {**(doc.get("scoring") or {}), "sampling": sampling}
    return doc


def _sent(doc: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(score._request_body(_posting(), doc))
    assert isinstance(body, dict)
    return body


def test_the_defaults_are_the_values_sent_before() -> None:
    assert DEFAULT_SAMPLING == {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repeat_penalty": 1.0,
    }
    assert DEFAULT_SEED == 20261119
    assert score.SAMPLING == DEFAULT_SAMPLING and score.SEED == DEFAULT_SEED
    assert set(SAMPLING_RANGES) == {*DEFAULT_SAMPLING, "seed"}


@pytest.mark.parametrize("sampling", [None, {}, {"seed": None, "top_k": None}])
def test_unset_sampling_sends_the_same_bytes(sampling: object) -> None:
    plain = score._request_body(_posting(), RUBRIC)
    assert FIXED_BYTES in plain
    doc = _with(sampling)
    assert validate_config(doc) == validate_config(RUBRIC)
    assert score._request_body(_posting(), doc) == plain


def test_explicit_defaults_send_the_same_bytes() -> None:
    doc = _with({**DEFAULT_SAMPLING, "seed": DEFAULT_SEED})
    assert score._request_body(_posting(), doc) == score._request_body(_posting(), RUBRIC)


def test_set_values_are_sent_and_the_rest_keep_their_defaults() -> None:
    body = _sent(_with({"seed": 7, "temperature": 0.7, "top_k": 40}))
    assert (body["seed"], body["temperature"], body["top_k"]) == (7, 0.7, 40)
    assert (body["top_p"], body["min_p"]) == (0.95, 0.0)
    assert (body["presence_penalty"], body["repeat_penalty"]) == (1.5, 1.0)
    keys = list(body)
    assert keys[1 : 1 + len(SAMPLING_RANGES)] == [*DEFAULT_SAMPLING, "seed"]


def test_only_the_seed_changes_between_seeded_runs() -> None:
    one, two = _sent(_with({"seed": 1})), _sent(_with({"seed": 2}))
    assert one.pop("seed") == 1 and two.pop("seed") == 2
    assert one == two


@pytest.mark.parametrize(
    ("sampling", "message"),
    [
        ([0.6], "scoring.sampling must be a mapping"),
        ({"temp": 0.6}, "scoring.sampling has unknown keys: ['temp']"),
        ({"temperature": 0}, "scoring.sampling.temperature must be a number above 0.0 to 2.0"),
        ({"temperature": 2.5}, "scoring.sampling.temperature must be a number above 0.0"),
        ({"temperature": "0.6"}, "scoring.sampling.temperature must be a number"),
        ({"top_p": 0}, "scoring.sampling.top_p must be a number above 0.0 to 1.0"),
        ({"top_k": 0}, "scoring.sampling.top_k must be an integer from 1 to 1000"),
        ({"top_k": 20.0}, "scoring.sampling.top_k must be an integer"),
        ({"min_p": -0.1}, "scoring.sampling.min_p must be a number from 0.0 to 1.0"),
        ({"presence_penalty": 3}, "scoring.sampling.presence_penalty must be a number from"),
        ({"repeat_penalty": 0.9}, "scoring.sampling.repeat_penalty must be a number from 1.0"),
        ({"seed": -1}, "scoring.sampling.seed must be an integer from 0 to 2147483647"),
        ({"seed": 2**31}, "scoring.sampling.seed must be an integer"),
        ({"seed": True}, "scoring.sampling.seed must be an integer"),
        ({"seed": 1.5}, "scoring.sampling.seed must be an integer"),
    ],
)
def test_bad_sampling_is_refused(sampling: object, message: str) -> None:
    problems = validate_config(_with(sampling))
    assert any(problem.startswith(message) for problem in problems), problems


@pytest.mark.parametrize(
    "sampling",
    [
        {"temperature": 2.0, "top_p": 1.0, "top_k": 1000, "min_p": 1.0},
        {"temperature": 0.01, "top_k": 1, "min_p": 0, "presence_penalty": -2},
        {"presence_penalty": 2, "repeat_penalty": 2, "seed": 0},
        {"repeat_penalty": 1, "seed": 2**31 - 1},
    ],
)
def test_values_at_the_ends_of_each_range_are_accepted(sampling: dict[str, Any]) -> None:
    assert validate_config(_with(sampling)) == validate_config(RUBRIC)
    sent = scoring_sampling(_with(sampling))
    assert all(sent[key] == value for key, value in sampling.items())


@pytest.mark.parametrize(
    "sampling",
    [{"temperature": 0, "seed": "x"}, {"top_k": True, "seed": 2**31}, ["seed"], "seed: 7"],
)
def test_an_invalid_value_never_reaches_a_request(sampling: object) -> None:
    assert scoring_sampling(_with(sampling)) == {**DEFAULT_SAMPLING, "seed": DEFAULT_SEED}


def test_a_config_with_no_scoring_section_uses_the_defaults() -> None:
    assert scoring_sampling(None) == {**DEFAULT_SAMPLING, "seed": DEFAULT_SEED}
    assert scoring_sampling({"scoring": None}) == {**DEFAULT_SAMPLING, "seed": DEFAULT_SEED}
