"""Main-function gaps on junior or entry postings (batch R2): an ordinary core gap there,
so one is Medium, not Low. Other postings keep the Low rule. Synthetic postings and
replies only."""

from __future__ import annotations

import pytest

from careerhunter.filters import PostingText
from careerhunter.score import Score, score_posting
from tests.fakes import fit_reply, requirement
from tests.test_score import DEFAULTS, reply

ENTRY = "Recent graduates are welcome to apply."
MAIN = requirement("Hands-on network monitoring", type="skill", core=True, main_function=True)
OTHER = requirement("Ticketing system experience", type="experience", core=True)


def _tier(title: str, *items: dict[str, object], entry_level: str = "") -> Score:
    text = PostingText(
        "Example Networks",
        title,
        "Example City",
        "remote",
        description=f"Synthetic duties. {ENTRY}",
    )
    content = fit_reply(items=items, entry_level=entry_level)
    result = score_posting(text, DEFAULTS, transport=lambda url, body: reply(content))
    assert result is not None
    return result


@pytest.mark.parametrize(
    "title", ["Junior NOC Analyst", "NOC Analyst I", "Associate Network Analyst"]
)
def test_a_main_function_gap_on_a_junior_title_is_medium(title: str) -> None:
    assert _tier(title, MAIN).tier == "medium"


def test_a_main_function_gap_on_a_grounded_entry_level_posting_is_medium() -> None:
    assert _tier("NOC Analyst", MAIN, entry_level=ENTRY).tier == "medium"
    # an entry-level quote not in the posting is not grounded: the Low rule stays
    assert _tier("NOC Analyst", MAIN, entry_level="We train everyone from day one").tier == "low"


def test_other_postings_keep_the_low_rule() -> None:
    assert _tier("NOC Analyst", MAIN).tier == "low"
    assert _tier("Senior NOC Analyst", MAIN).tier == "low"  # the title is a seniority gap too


def test_on_a_junior_posting_two_core_gaps_are_still_low_and_three_exclude() -> None:
    assert _tier("Junior NOC Analyst", MAIN, OTHER).tier == "low"
    third = requirement("On-call rotation experience", type="experience", core=True)
    result = _tier("Junior NOC Analyst", MAIN, OTHER, third)
    assert result.tier is None and result.excluded
