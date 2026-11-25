"""`met` criteria rules (batch R2, ADR-028 as amended): a fact about the candidate that the
model counts as met when a posting asks for exactly what it names. Never a veto or a cap.
Synthetic rules, postings and replies only."""

from __future__ import annotations

import json
from typing import Any

import pytest

from careerhunter import criteria
from careerhunter import score as scoring
from careerhunter.score import Cap, Veto, score_posting
from tests.fakes import fit_reply
from tests.test_criteria_veto import _posting
from tests.test_score import RUBRIC, reply

TEXT = (
    "1. veto: No synthetic night shifts.\n"
    "2. met: the candidate holds a forklift certificate (covers forklift and lift-truck "
    "requirements)\n"
    "3. cap medium: A synthetic contract with only an extension."
)
RULES = criteria.Criteria(TEXT, len(TEXT), criteria.parse_rules(TEXT))
FORKLIFT = (
    "the candidate holds a forklift certificate (covers forklift and lift-truck requirements)"
)


@pytest.mark.parametrize(
    ("line", "label"),
    [
        (f"1. met: {FORKLIFT}", None),
        (f"1. MET : {FORKLIFT}", None),
        (f"1. met [forklift]: {FORKLIFT}", "forklift"),
    ],
)
def test_a_met_rule_parses(line: str, label: str | None) -> None:
    (rule,) = criteria.parse_rules(line)
    assert rule == criteria.Rule(1, FORKLIFT, "met", None, label)


def test_met_rules_are_counted_apart_from_vetoes_and_caps() -> None:
    assert (RULES.count("veto"), RULES.count("cap"), RULES.count("met")) == (1, 1, 1)
    assert RULES.rule(2, "met") is not None
    assert RULES.rule(2, "veto") is None and RULES.rule(2, "cap") is None


@pytest.mark.parametrize("line", ["1. met:", "1. met low: x", "1. mets: x", "1. met x"])
def test_a_bad_met_line_is_refused(line: str) -> None:
    with pytest.raises(criteria.CriteriaError) as caught:
        criteria.parse_rules(line)
    assert caught.value.reason == "no-kind"


def _request() -> dict[str, Any]:
    body = scoring._request_body(_posting("Synthetic duties."), RUBRIC, criteria=RULES)
    return json.loads(body)


def test_the_met_rule_is_sent_with_its_kind() -> None:
    view = json.loads(_request()["messages"][1]["content"])
    assert {"rule": 2, "kind": "met", "text": FORKLIFT} in view["criteria"]


def test_the_instruction_keeps_a_met_rule_narrow() -> None:
    system = _request()["messages"][0]["content"]
    assert '"met" (a fact the candidate has, even when the resume doesn\'t show it)' in system
    assert "A requirement a met rule names is met: never list it as a gap" in system
    assert "covers only what it names, never anything similar or broader" in system
    assert "and is never a veto or a cap" in system


def _score(**extra: Any) -> Any:
    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        return reply(fit_reply(**extra))

    posting = _posting("Synthetic duties. A forklift certificate is required.")
    return score_posting(posting, RUBRIC, transport=transport, criteria=RULES)


def test_a_met_rule_named_as_a_veto_is_rejected_and_the_posting_scored() -> None:
    result = _score(veto={"rule": 2, "quote": "A forklift certificate is required"}, cap=None)
    assert result is not None and result.veto == Veto(2, "unknown-rule")
    assert not result.vetoed and result.tier == "high"


def test_a_met_rule_named_as_a_cap_is_rejected_and_never_lowers_the_fit() -> None:
    result = _score(veto=None, cap={"rule": 2, "quote": "A forklift certificate is required"})
    assert result is not None and result.cap == Cap(2, "unknown-rule")
    assert result.capped is None and result.tier == "high"
