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
        # Level words anywhere in the head are set aside, kept at the start or the end.
        ("Senior Analyst, Client Experience", "senior client experience analyst"),
        ("Analyst II, Client Experience", "client experience analyst ii"),
        ("Sr. Analyst (Level 2), Client Experience", "sr client experience analyst level 2"),
        ("Senior Manager, Client Experience", "senior manager client experience"),
        ("Senior, Client Experience", "senior client experience"),  # no role word left
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
        ("Senior Analyst, Client Experience", "Client Experience Analyst"),
        ("Analyst II, Client Experience", "Client Experience Specialist"),
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
        # Near misses for a level word in the head.
        ("Senior Analyst, Client Experience Operations", "Client Experience Analyst"),
        ("Senior Manager, Client Experience", "Client Experience Analyst"),
    ],
)
def test_not_equivalent(a: str, b: str) -> None:
    assert not titles.equivalent(a, b)


def test_a_level_word_in_the_head_stays_only_equivalent() -> None:
    """The level is kept in the canonical form, so the import lists it, not attaches."""
    senior = "Senior Analyst, Client Experience"
    assert titles.canonical(senior) != titles.canonical("Client Experience Analyst")
    assert titles.canonical(senior) == titles.canonical("Senior Client Experience Analyst")


@pytest.mark.parametrize(
    ("title", "variant"),
    [
        ("IT Support Associate", "IT Support Specialist"),
        ("IT Support Associate", "IT Support Analyst"),
        ("Analyst, Client Experience", "Client Experience Analyst"),
        ("Senior Analyst, Client Experience", "Client Experience Analyst"),
        ("Analyst II, Client Experience", "Client Experience Specialist"),
        ("Senior IT Support Associate II", "IT Support Specialist"),
        ("IT Support Associate - Night Shift", "IT Support Specialist"),
        ("IT Support Associate (Level 2)", "IT Support Specialist"),
        ("IT Support Associate \u2013 Contract", "IT Support Specialist"),
        ("Regional IT Support Associate", "IT Support Specialist"),
        ("IT Support Associate", "Senior IT Support Specialist"),  # levels in the lane too
    ],
)
def test_loose_match(title: str, variant: str) -> None:
    assert titles.loose_match(title, variant)


@pytest.mark.parametrize(
    ("title", "variant"),
    [
        ("IT Support Manager", "IT Support Specialist"),
        ("IT Support Associate Manager", "IT Support Specialist"),
        ("Data Associate", "Data Analyst"),  # a one-word core is never loose
        ("Business Analyst, IT Support", "IT Support Specialist"),
        ("Analyst, Client Experience Operations", "Client Experience Analyst"),
        ("Senior Analyst, Client Experience Operations", "Client Experience Analyst"),
        ("Senior Manager, Client Experience", "Client Experience Analyst"),
        ("Associate Director, IT Support", "IT Support Specialist"),
        ("Client Experience Director", "Client Experience Analyst"),
        ("IT Support Lead", "IT Support Specialist"),
        ("IT Support Associate/Analyst Lead", "IT Support Specialist"),
        ("IT Support Coordinator", "IT Support Coordinator Lead"),  # no role word
        ("", "IT Support Specialist"),
    ],
)
def test_no_loose_match(title: str, variant: str) -> None:
    assert not titles.loose_match(title, variant)


def test_lane_core() -> None:
    assert titles.lane_core("IT Support Specialist") == ("it", "support")
    assert titles.lane_core("Senior IT Support Analyst II") == ("it", "support")
    assert titles.lane_core("Analyst, Client Experience") == ("client", "experience")
    assert titles.lane_core("Data Analyst") is None
    assert titles.lane_core("IT Support Manager") is None


def test_role_words_are_the_built_in_group() -> None:
    assert frozenset({"specialist", "analyst", "associate"}) == titles.ROLE_WORDS
    assert not {"lead", "manager", "principal", "head"} & titles.LEVEL_WORDS
