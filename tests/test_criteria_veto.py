"""Criteria vetoes and caps (ADR-028): a rule the posting meets, grounded by a quote
checked in code.

Synthetic postings, companies and rules only. The model is a fake transport, so these
tests check the code path; the model's own judgment is checked by hand with the real
local model. Cases 1 to 8 are the operator's regression cases.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from careerhunter import criteria
from careerhunter import score as scoring
from careerhunter.filters import PostingText
from careerhunter.score import Cap, Score, Veto, score_posting
from tests.test_score import RUBRIC

TEXT = (
    "1. veto: No French or bilingual mention, whether required, preferred or an asset.\n"
    "2. veto: No contract or term of any length unless the posting states a possible move "
    "to a permanent or full-time role, or a possible extension.\n"
    "3. cap medium: A contract or term whose only stated path is a possible extension, "
    "with no path to permanent."
)
RULES = criteria.Criteria(TEXT, len(TEXT), criteria.parse_rules(TEXT))
GAPS = [{"name": "Forklift licence", "kind": "minor"}]


def _posting(description: str, title: str = "Operations Coordinator") -> PostingText:
    return PostingText(
        "Northwind Analytics", title, "Example City", "hybrid", description=description
    )


def _score(
    posting: PostingText,
    veto: object | None = None,
    *,
    cap: object | None = None,
    stated: criteria.Criteria | None = RULES,
    gaps: list[dict[str, str]] | None = None,
    seen: list[dict[str, Any]] | None = None,
) -> Score | None:
    content: dict[str, Any] = {"gaps": GAPS if gaps is None else gaps, "requirements": []}
    if veto is not None:
        content["veto"] = veto
    if cap is not None:
        content["cap"] = cap

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        if seen is not None:
            seen.append(json.loads(payload))
        body = {"choices": [{"message": {"content": json.dumps(content)}}]}
        return 200, json.dumps(body).encode()

    return score_posting(posting, RUBRIC, transport=transport, criteria=stated)


# --- the operator's regression cases -----------------------------------------------------


def test_1_a_short_contract_with_no_path_is_vetoed() -> None:
    posting = _posting(
        "Northwind Analytics is hiring for a 3-month contract, full-time hours, "
        "to support a synthetic warehouse move."
    )
    result = _score(posting, {"rule": 2, "quote": "3-month contract, full-time hours"})
    assert result is not None and result.vetoed
    assert result.veto == Veto(2) and result.capped is None


def test_2_a_twelve_month_term_with_no_path_is_vetoed() -> None:
    posting = _posting("A 12-month term position at a synthetic depot. The term ends in 2027.")
    result = _score(posting, {"rule": 2, "quote": "A 12-month term position"})
    assert result is not None and result.veto == Veto(2)


def test_the_skip_reason_is_the_rules_first_words_marked_as_cut() -> None:
    posting = _posting("A 12-month term position at a synthetic depot.")
    result = _score(posting, {"rule": 2, "quote": "A 12-month term position"})
    assert result is not None and result.veto is not None
    assert result.veto.skip == "criteria: 2. No contract or term of any…"


def test_a_short_rule_is_named_whole_with_no_mark() -> None:
    text = "1. veto: No synthetic night shifts."
    stated = criteria.Criteria(text, len(text), criteria.parse_rules(text))
    posting = _posting("Synthetic night shifts at the example depot.")
    result = _score(posting, {"rule": 1, "quote": "Synthetic night shifts"}, stated=stated)
    assert result is not None and result.veto is not None
    assert result.veto.skip == "criteria: 1. No synthetic night shifts."


def test_a_labelled_rule_is_named_by_its_label() -> None:
    text = TEXT.replace("2. veto:", "2. veto [contract, no path]:")
    stated = criteria.Criteria(text, len(text), criteria.parse_rules(text))
    seen: list[dict[str, Any]] = []
    posting = _posting("A 12-month term position at a synthetic depot.")
    result = _score(
        posting, {"rule": 2, "quote": "A 12-month term position"}, stated=stated, seen=seen
    )
    assert result is not None and result.veto is not None
    assert result.veto.skip == "criteria: 2. contract, no path"
    assert "contract, no path" not in json.dumps(seen)  # the label is never sent


def test_3_a_contract_with_a_path_to_permanent_is_neither_vetoed_nor_capped() -> None:
    result = _score(
        _posting("A 6-month contract with a stated path to a permanent role at the depot.")
    )
    assert result is not None and not result.vetoed and result.capped is None
    assert result.tier == "strong"


def test_4_french_in_wording_the_gate_misses_is_vetoed() -> None:
    posting = _posting("Bilingue (FR/EN) requis. Coordinate synthetic shipments.")
    result = _score(posting, {"rule": 1, "quote": "Bilingue (FR/EN) requis"})
    assert result is not None and result.veto == Veto(1)


def test_5_french_as_an_asset_is_vetoed_with_its_quote_checked() -> None:
    posting = _posting("French is an asset. Coordinate synthetic shipments.")
    result = _score(posting, {"rule": 1, "quote": "French is an asset"})
    assert result is not None and result.veto == Veto(1)
    invented = _score(posting, {"rule": 1, "quote": "French is preferred"})
    assert invented is not None and invented.veto == Veto(1, "not-found")


def test_6_a_fabricated_quote_is_rejected_and_the_posting_is_scored() -> None:
    posting = _posting("A permanent, full-time role coordinating synthetic shipments.")
    result = _score(posting, {"rule": 2, "quote": "3-month contract, no extension"})
    assert result is not None and not result.vetoed
    assert result.veto == Veto(2, "not-found")
    assert result.tier == "strong"  # scored from the same reply's gaps
    assert json.loads(result.gaps) == GAPS


def test_7_a_contract_with_only_a_possible_extension_is_capped_at_medium() -> None:
    posting = _posting("A 6-month contract with the possibility of extension.")
    result = _score(posting, cap={"rule": 3, "quote": "possibility of extension"})
    assert result is not None and not result.vetoed
    assert result.cap == Cap(3, level="medium") and result.capped == "medium"
    assert result.tier == "strong"  # the tier stands; the watcher caps the fit


def test_8_a_fabricated_cap_quote_is_discarded_and_the_posting_is_scored() -> None:
    posting = _posting("A permanent, full-time role coordinating synthetic shipments.")
    result = _score(posting, cap={"rule": 3, "quote": "possibility of extension"})
    assert result is not None and result.capped is None
    assert result.cap == Cap(3, "not-found") and result.tier == "strong"


# --- caps beside vetoes -------------------------------------------------------------------


def test_a_cap_naming_a_veto_rule_is_rejected() -> None:
    posting = _posting("A 6-month contract with the possibility of extension.")
    result = _score(posting, cap={"rule": 2, "quote": "possibility of extension"})
    assert result is not None and result.cap == Cap(2, "unknown-rule")
    assert result.capped is None


def test_a_veto_wins_over_a_cap() -> None:
    posting = _posting("French is an asset. A 6-month contract with the possibility of extension.")
    result = _score(
        posting,
        {"rule": 1, "quote": "French is an asset"},
        cap={"rule": 3, "quote": "possibility of extension"},
    )
    assert result is not None and result.vetoed and result.capped is None


@pytest.mark.parametrize("quote", ["extend", "", None])
def test_a_short_or_missing_cap_quote_is_rejected(quote: object) -> None:
    posting = _posting("A 6-month contract with the possibility of extension.")
    result = _score(posting, cap={"rule": 3, "quote": quote})
    assert result is not None and result.cap == Cap(3, "too-short")


def test_a_cap_with_no_criteria_sent_is_ignored() -> None:
    posting = _posting("A 6-month contract with the possibility of extension.")
    result = _score(posting, cap={"rule": 3, "quote": "possibility of extension"}, stated=None)
    assert result is not None and result.cap is None


def test_a_cap_quote_is_never_kept() -> None:
    posting = _posting("A 6-month contract with the possibility of extension.")
    result = _score(posting, cap={"rule": 3, "quote": "possibility of extension"})
    assert result is not None
    assert "extension" not in result.llm_output and "extension" not in repr(result.cap)
    assert json.loads(result.llm_output) == {"gaps": GAPS}


# --- the checks around them --------------------------------------------------------------


@pytest.mark.parametrize("rule", [3, 4, 0, -1, "2", True, None, 2.0])
def test_a_rule_the_file_does_not_have_is_rejected(rule: object) -> None:
    posting = _posting("A 3-month contract, full-time hours.")
    result = _score(posting, {"rule": rule, "quote": "3-month contract, full-time hours"})
    assert result is not None and result.veto is not None
    assert result.veto.rejected == "unknown-rule" and not result.vetoed


@pytest.mark.parametrize("quote", ["3-month", "  3-month ", "", None, 42, "\x00\x01\x02"])
def test_a_quote_too_short_or_not_text_is_rejected(quote: object) -> None:
    result = _score(_posting("A 3-month contract."), {"rule": 2, "quote": quote})
    assert result is not None and result.veto == Veto(2, "too-short")


def test_a_veto_that_is_not_an_object_is_rejected() -> None:
    result = _score(_posting("A 3-month contract."), "rule 2")
    assert result is not None and result.veto == Veto(None, "unknown-rule")


def test_a_veto_with_no_criteria_sent_is_ignored() -> None:
    posting = _posting("A 3-month contract, full-time hours.")
    result = _score(posting, {"rule": 2, "quote": "3-month contract"}, stated=None)
    assert result is not None and result.veto is None and result.tier == "strong"


def test_the_quote_matches_across_whitespace_and_case() -> None:
    posting = _posting("Term:\n  a 3-MONTH   contract,\tfull-time hours.")
    result = _score(posting, {"rule": 2, "quote": "3-month Contract, FULL-TIME hours"})
    assert result is not None and result.veto == Veto(2)


def test_the_quote_may_come_from_the_title() -> None:
    posting = _posting("Coordinate synthetic shipments.", title="Coordinator (3-month contract)")
    result = _score(posting, {"rule": 2, "quote": "Coordinator (3-month contract)"})
    assert result is not None and result.veto == Veto(2)


def test_the_quote_is_not_matched_against_the_company_or_location() -> None:
    posting = _posting("Coordinate synthetic shipments.")
    for quote in ("Northwind Analytics", "Example City"):
        result = _score(posting, {"rule": 1, "quote": quote})
        assert result is not None and result.veto == Veto(1, "not-found")


def test_a_veto_stands_when_the_gaps_exclude_or_fit_no_tier() -> None:
    posting = _posting("A 3-month contract, full-time hours.")
    many = [{"name": f"Synthetic skill {n}", "kind": "core"} for n in range(5)]
    result = _score(posting, {"rule": 2, "quote": "3-month contract"}, gaps=many)
    assert result is not None and result.vetoed


def test_a_veto_in_an_unparseable_reply_is_not_honoured() -> None:
    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        content = json.dumps({"veto": {"rule": 2, "quote": "3-month contract"}})
        return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    posting = _posting("A 3-month contract.")
    assert score_posting(posting, RUBRIC, transport=transport, criteria=RULES) is None


@pytest.mark.parametrize(
    "quote", ["3-month contract, full-time hours", "invented: full-time hours only", "hours"]
)
def test_the_quote_is_never_kept_in_the_score(quote: str) -> None:
    posting = _posting("A 3-month contract, full-time hours.")
    result = _score(posting, {"rule": 2, "quote": quote})
    assert result is not None and result.veto is not None
    fields = (result.llm_output, result.gaps, repr(result.veto), result.key_requirements)
    assert not any("hours" in str(field) for field in fields)
    assert json.loads(result.llm_output) == {"gaps": GAPS}


def test_the_request_has_numbered_rules_and_no_gate_lists() -> None:
    seen: list[dict[str, Any]] = []
    config = dict(RUBRIC, hard_gates={"required_languages": ["synthetic-lang"]})
    posting = _posting("Coordinate synthetic shipments.")
    score_posting(
        posting,
        config,
        transport=lambda url, payload: (
            seen.append(json.loads(payload)),
            (200, json.dumps({"choices": [{"message": {"content": '{"gaps":[]}'}}]}).encode()),
        )[1],
        criteria=RULES,
    )
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["criteria"] == [
        {"rule": 1, "kind": "veto", "text": RULES.rules[0].text},
        {"rule": 2, "kind": "veto", "text": RULES.rules[1].text},
        {"rule": 3, "kind": "cap", "text": RULES.rules[2].text, "cap": "medium"},
    ]
    assert "hard_gates" not in view and "synthetic-lang" not in json.dumps(seen[0])


def test_the_instruction_applies_rules_as_written() -> None:
    text = scoring.CRITERIA_INSTRUCTION
    for words in (
        "exactly as written",
        "an asset included when the rule names it",
        "allowed side of a limit",
        "exception the rule itself allows",
        '"cap":{"rule":N,"quote":"..."}',
    ):
        assert words in text
    assert "is not a match: a skill wanted only as an asset" not in text  # the old wording
    assert f"at most {scoring.MAX_QUOTE} characters" in text
    assert "never list them as gaps or requirements" in text


def test_a_veto_naming_a_cap_rule_is_rejected() -> None:
    text = "1. cap medium: A synthetic contract whose only stated path is an extension."
    stated = criteria.Criteria(text, len(text), criteria.parse_rules(text))
    posting = _posting("A 3-month contract, full-time hours.")
    result = _score(posting, {"rule": 1, "quote": "3-month contract"}, stated=stated)
    assert result is not None and result.veto == Veto(1, "unknown-rule")
