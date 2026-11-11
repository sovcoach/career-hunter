"""`titles.py`: one title and employer normalization. Synthetic titles and names only."""

from __future__ import annotations

import pytest

from careerhunter import titles
from careerhunter.filters import title_key


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Client Experience Analyst", "client experience analyst"),
        ("Analyst, Client Experience", "client experience analyst"),
        ("Specialist, IT Support", "it support specialist"),
        ("IT Support Associate (R12345)", "it support associate"),
        ("Analyst, R12345", "analyst"),  # a requisition code is dropped, not inverted
        ("Business Analyst, IT Support", "it support business analyst"),
        ("Associate Director, IT Support", "associate director it support"),  # no role word
        ("Analyst, Data Analyst", "analyst data analyst"),  # the tail has a role word
        ("Analyst, Client, Experience", "analyst client experience"),  # two commas
        ("\uff29\uff34 Support Analyst", "it support analyst"),  # NFKC, fullwidth
    ],
)
def test_canonical(title: str, expected: str) -> None:
    assert titles.canonical(title) == expected


def test_canonical_starts_from_title_key() -> None:
    for title in ("Data Analyst - 104332", "Senior Analyst (Level 2)", "Analyst \u2013 2026"):
        assert titles.canonical(title) == title_key(title)


def test_canonical_is_idempotent() -> None:
    for title in ("Analyst, Client Experience", "IT Support Associate II", "Lead, Ops"):
        once = titles.canonical(title)
        assert titles.canonical(once) == once


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Example Robotics Inc.", "example robotics"),
        ("Example Robotics, Inc", "example robotics"),
        ("The Example Robotics Company", "example robotics company"),
        ("The Example Robotics Co., Ltd.", "example robotics"),
        ("Synthetic Widgets L.L.C.", "synthetic widgets"),
        ("Synthetic Widgets Corporation", "synthetic widgets"),
        ("Synthetic Widgets Limited", "synthetic widgets"),
        ("SYNTHETIC WIDGETS CORP", "synthetic widgets"),
        ("The Co", "co"),  # a name is never folded away entirely
        ("Inc", "inc"),
        ("Thesis Labs", "thesis labs"),  # only a whole leading "The"
    ],
)
def test_employer_key(name: str, expected: str) -> None:
    assert titles.employer_key(name) == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # The two examples from the lane-matching plan.
        ("IT Support Associate", "IT Support Specialist"),
        ("Analyst, Client Experience", "Client Experience Analyst"),
        ("IT Support Associate", "IT Support Analyst"),
        ("Senior IT Support Associate II", "IT Support Specialist"),
        ("Jr. Data Analyst", "Data Analyst"),
        ("Data Analyst (Level 2)", "Data Analyst"),
        ("Entry Level Data Analyst", "Data Analyst III"),
        ("Intermediate Data Specialist 3", "Data Associate"),
    ],
)
def test_equivalent(a: str, b: str) -> None:
    assert titles.equivalent(a, b)
    assert titles.equivalent(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("IT Support Manager", "IT Support Specialist"),
        ("IT Support Lead", "IT Support Specialist"),
        ("Principal Data Analyst", "Data Analyst"),
        ("Head of Data", "Data"),
        ("IT Support Associate Manager", "IT Support Specialist"),
        ("Client Experience Director", "Client Experience Analyst"),
        ("Data Analyst", "Data Engineer"),
        ("Senior", "Junior"),  # a title is never stripped to nothing
    ],
)
def test_not_equivalent(a: str, b: str) -> None:
    assert not titles.equivalent(a, b)


def test_role_words_are_the_built_in_group() -> None:
    assert frozenset({"specialist", "analyst", "associate"}) == titles.ROLE_WORDS
    assert not {"lead", "manager", "principal", "head"} & titles.LEVEL_WORDS
