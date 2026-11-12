"""An Ashby board name may contain a dot, such as a company's domain. Synthetic names."""

from __future__ import annotations

import pytest

from careerhunter import ats, discover


@pytest.mark.parametrize("slug", ["example.com", "example.co.uk", "Example-Org_2.io", "exampleorg"])
def test_a_dotted_ashby_board_name_is_valid(slug: str) -> None:
    assert ats.board_url("ashby", slug) == (
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    )
    found = discover.classify(f"https://jobs.ashbyhq.com/{slug}/role-9", ())
    assert found is not None and (found.ats_type, found.ats_slug) == ("ashby", slug)


@pytest.mark.parametrize(
    "slug", [".", "..", ".example", "example.", "example..com", "example/com", "exa mple.com"]
)
def test_a_dot_never_makes_a_path_segment_unsafe(slug: str) -> None:
    assert ats.board_url("ashby", slug) is None


@pytest.mark.parametrize(
    "url", ["https://jobs.ashbyhq.com/../role-9", "https://jobs.ashbyhq.com/.example"]
)
def test_discovery_refuses_a_dot_segment(url: str) -> None:
    found = discover.classify(url, ())
    assert found is None or found.ats_type != "ashby"


@pytest.mark.parametrize("ats_type", ["greenhouse", "lever", "smartrecruiters", "bamboohr"])
def test_other_boards_still_refuse_a_dot(ats_type: str) -> None:
    assert ats.board_url(ats_type, "example.com") is None
