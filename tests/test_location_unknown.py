"""Locations that name no place the rules can check (Batch R): only a country, or only
a building. Such a posting is scored and alerted, never dropped by `location_rule`.
Synthetic postings only; the countries are well-known examples."""

from __future__ import annotations

from pathlib import Path

import pytest

from careerhunter import countries, db, filters, locations, watch
from careerhunter.filters import PostingText
from careerhunter.score import Score
from tests.test_watch import Scripted, company, fetched, url

RULES = {"onsite_hybrid": ["Example City"], "remote": ["Exampleland"]}


@pytest.mark.parametrize(
    "text",
    [
        "Japan",
        "japan.",
        "U.S.A.",
        "USA",
        "United Kingdom",
        "The Netherlands",
        "Côte d'Ivoire",
        "Bosnia & Herzegovina",
        "  Germany  ",
    ],
)
def test_country_names_and_common_spellings_are_countries(text: str) -> None:
    assert countries.is_country(text)


@pytest.mark.parametrize(
    "text", ["Example City, Japan", "Remote - Japan", "Example City", "", "Japanese", "Remote"]
)
def test_a_place_in_a_country_is_not_only_a_country(text: str) -> None:
    assert not countries.is_country(text)


@pytest.mark.parametrize(
    "text",
    [
        "Example Tower",
        "EXAMPLE CENTRE",
        "Sample Building 4",
        "Example Plaza",
        "Sample Campus",
        "Example Financial Center",
        "Example HQ",
    ],
)
def test_a_building_name_alone_is_building_only(text: str) -> None:
    assert locations.building_only(text)


@pytest.mark.parametrize(
    "text",
    [
        "Example Tower, Example City",
        "Example Tower - Example City",
        "Example Tower / Remote",
        "Sample Park",
        "Example Place",
        "Example Square",
        "Example City",
        "Japan",
        "",
    ],
)
def test_a_building_with_a_place_or_a_town_is_not_building_only(text: str) -> None:
    assert not locations.building_only(text)


def _decide(location: str, title: str = "Analyst", work_model: str | None = None) -> str | None:
    return filters.decide(
        PostingText(
            company="Example Software", title=title, location=location, work_model=work_model
        ),
        config={"search": {"location_rules": RULES}},
        exclusions={},
        role_filters=None,
        applied=False,
    ).reason


def test_a_country_only_location_is_kept_like_a_missing_one() -> None:
    assert _decide("Japan") is None
    assert _decide("Japan", work_model="onsite") is None  # on-site, but where?
    assert _decide("Example Town, Japan") == "location_rule"  # a place: the rules apply


def test_a_country_with_remote_or_hybrid_stated_still_follows_the_rules() -> None:
    assert _decide("Japan", work_model="remote") == "location_rule"
    assert _decide("Japan", work_model="hybrid") == "location_rule"
    assert _decide("Japan", title="Analyst - Remote") == "location_rule"
    assert _decide("Exampleland", work_model="remote") is None


def test_a_building_only_location_is_kept_like_a_missing_one() -> None:
    assert _decide("Example Tower") is None
    assert _decide("Example Tower, Example Town") == "location_rule"


def test_role_filter_locations_never_drop_an_unknown_location() -> None:
    for place in ("Japan", "Example Tower", ""):
        verdict = filters.decide(
            PostingText(company="Example Software", title="Analyst", location=place),
            config={},
            exclusions={},
            role_filters={"locations": ["Example City"]},
            applied=False,
        )
        assert verdict.reason is None
    verdict = filters.decide(
        PostingText(company="Example Software", title="Analyst", location="Example Town"),
        config={},
        exclusions={},
        role_filters={"locations": ["Example City"]},
        applied=False,
    )
    assert verdict.reason == "role_filter"


SHAPED_LIKE_THE_CASE = (
    "Example Software is hiring an FP&A Financial Analyst to join its finance team. "
    "You will build budgets and forecasts, run variance analysis and report on orders "
    "and margin. 2-3 years as an FP&A analyst. Advanced Excel."
)


def test_a_watched_posting_whose_location_is_only_a_country_is_scored_and_shown_as_given(
    tmp_path: Path,
) -> None:
    """The shape of the posting that showed the bug: a corporate finance posting whose
    board gives only a country as its location, and no work model. It used to be
    skipped as `location_rule`; now it is scored, and its location is kept as given."""
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    scored: list[str] = []

    def scorer(posting: PostingText) -> Score:
        scored.append(posting.location)
        return Score(llm_output="model text", gaps="[]", tier="low")

    job = {
        "id": 9,
        "title": "FP&A Financial Analyst",
        "absolute_url": "https://boards.greenhouse.io/examplesoftware/jobs/9",
        "location": {"name": "Japan"},
        "content": SHAPED_LIKE_THE_CASE,
    }
    board = url("greenhouse", "examplesoftware")
    watch.run(
        conn,
        [company("Example Software", "greenhouse", "examplesoftware")],
        today="2026-11-02",
        fetcher=Scripted({board: fetched({"jobs": [job]})}, on_board=True),
        config={"search": {"location_rules": RULES}},
        scorer=scorer,
    )
    (row,) = conn.execute(
        "SELECT p.location, (SELECT reason FROM skips s WHERE s.posting_id = p.id) FROM postings p"
    ).fetchall()
    assert tuple(row) == ("Japan", None)
    assert scored == ["Japan"]
    conn.close()
