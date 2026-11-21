"""The "Junior/Entry ... Developer" title rule in the lane matcher (Batch R).
Synthetic titles and lanes only."""

from __future__ import annotations

from typing import Any

import pytest

from careerhunter import lanes, titles

CONFIG: dict[str, Any] = {
    "search": {
        "lanes": [
            {"name": "support", "priority": 1, "titles": ["IT Support Specialist"]},
            {
                "name": "developer",
                "priority": 2,
                "titles": [["Junior Developer", "Entry Level Developer"]],
            },
            {"name": "data", "priority": 3, "titles": ["Data Analyst"]},
        ]
    }
}


def _lane(title: str) -> str | None:
    found = lanes.match(title, CONFIG)
    return None if found is None else found.name


@pytest.mark.parametrize(
    "title",
    [
        "Junior Java Developer",
        "Junior Full Stack Developer",
        "Entry-Level Software Developer",
        "Entry Level .NET Developer II",
        "Jr. Python Developer (Remote)",
        "Junior Developer",
    ],
)
def test_a_junior_or_entry_developer_title_is_on_the_developer_lane(title: str) -> None:
    assert _lane(title) == "developer"


@pytest.mark.parametrize(
    "title",
    [
        "Senior Java Developer",
        "Java Developer",
        "Junior Front End React TypeScript Developer",  # more than three words between
        "Junior Software Development Manager",
        "Junior Data Analyst, Developer Tools",  # not across a separator
    ],
)
def test_other_developer_titles_are_not(title: str) -> None:
    assert _lane(title) != "developer"


def test_a_literal_match_on_a_better_lane_still_wins() -> None:
    assert _lane("Junior IT Support Specialist / Junior Developer") == "support"


def test_only_a_junior_developer_variant_takes_the_rule() -> None:
    assert titles.junior_developer_match("Junior Java Developer", "Junior Developer")
    assert titles.junior_developer_match("Junior Java Developer", "Entry Level Developer")
    assert not titles.junior_developer_match("Junior Java Developer", "Developer")
    assert not titles.junior_developer_match("Junior Java Developer", "Senior Developer")
    assert not titles.junior_developer_match("Junior Java Developer", "Junior Analyst")
