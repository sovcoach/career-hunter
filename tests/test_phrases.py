"""Requirement text is cut at a clause break or a word, never mid-phrase, and never
longer than the limit (Batch R). Synthetic phrases only."""

from __future__ import annotations

import pytest

from careerhunter import bot as botmod
from careerhunter import phrases
from careerhunter import score as scoring

DASH = chr(0x2013)  # an en dash, spaced, as a clause break


@pytest.mark.parametrize(
    ("text", "cut"),
    [
        (
            "2-3 years of experience in customer service, preferably in a busy contact team",
            "2-3 years of experience in customer service…",
        ),
        (
            "Experience with ticketing systems and remote support tools such as SampleDesk",
            "Experience with ticketing systems and remote support tools…",
        ),
        (
            "Advanced spreadsheet skills (pivot tables, lookups, advanced formulas)",
            "Advanced spreadsheet skills…",
        ),
        (
            "Experience coordinating inbound and outbound freight across several sites",
            "Experience coordinating inbound and outbound freight…",
        ),
        (
            f"Strong communication skills {DASH} written and verbal {DASH} "
            "with clients and the team",
            f"Strong communication skills {DASH} written and verbal…",
        ),
        ("x" * 70, "x" * 59 + "…"),
        ("A short requirement", "A short requirement"),
        ("  spaced\n  out  ", "spaced out"),
    ],
)
def test_a_long_phrase_is_cut_where_a_phrase_ends(text: str, cut: str) -> None:
    found = phrases.cut(text, 60)
    assert found == cut
    assert len(found) <= 60


@pytest.mark.parametrize("limit", [20, 40, 60])
def test_a_cut_is_never_longer_than_its_limit(limit: int) -> None:
    text = "Plan synthetic routes, schedules and loads for the yard (day and night shifts)"
    assert len(phrases.cut(text, limit)) <= limit


def test_the_stored_and_the_shown_limits_are_unchanged() -> None:
    assert scoring.MAX_REQUIREMENT == 60
    assert botmod._CAPS["requirement"] == 60
