"""Canonical posting URLs and the hosts that are never fetched."""

from __future__ import annotations

import pytest

from careerhunter.urls import canonical, no_fetch

CASES = [
    # Scheme and host are lowercased; the fragment goes; the path keeps its case.
    ("HTTPS://Jobs.Example.COM/Ad/1#apply", "https://jobs.example.com/Ad/1"),
    # utm_* in any case.
    (
        "https://jobs.example.com/ad?utm_source=x&UTM_Medium=y&utm_campaign=z",
        "https://jobs.example.com/ad",
    ),
    # LinkedIn-style tracking on a non-LinkedIn page.
    (
        "https://jobs.example.com/ad?trk=a&trkInfo=b&refId=c&trackingId=d&lipi=e",
        "https://jobs.example.com/ad",
    ),
    # Ad-click and mail-campaign ids.
    (
        "https://jobs.example.com/ad?gclid=1&fbclid=2&msclkid=3&mc_cid=4&mc_eid=5",
        "https://jobs.example.com/ad",
    ),
    # gh_src goes, gh_jid stays.
    (
        "https://careers.example.com/jobs?gh_jid=4012345&gh_src=abc",
        "https://careers.example.com/jobs?gh_jid=4012345",
    ),
    # Identifying parameters keep their order and their spelling.
    (
        "https://jobs.example.com/view?b=2&utm_source=x&a=hello%20world&id=7",
        "https://jobs.example.com/view?b=2&a=hello%20world&id=7",
    ),
    # LinkedIn view links, with a slug, a trailing path or tracking.
    (
        "https://www.linkedin.com/jobs/view/4000000001/?refId=abc123&trk=public_jobs_topcard",
        "https://www.linkedin.com/jobs/view/4000000001/",
    ),
    (
        "https://ca.linkedin.com/jobs/view/support-analyst-at-example-robotics-4000000002",
        "https://www.linkedin.com/jobs/view/4000000002/",
    ),
    (
        "http://LinkedIn.com/jobs/view/it-2-support-4000000003/apply?x=1",
        "https://www.linkedin.com/jobs/view/4000000003/",
    ),
    # currentJobId on a search or collections page.
    (
        "https://www.linkedin.com/jobs/search/?currentJobId=4000000004&keywords=analyst",
        "https://www.linkedin.com/jobs/view/4000000004/",
    ),
    (
        "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4000000005",
        "https://www.linkedin.com/jobs/view/4000000005/",
    ),
    # A LinkedIn page with no job id only loses its tracking.
    (
        "https://www.linkedin.com/company/example-robotics/?trk=x",
        "https://www.linkedin.com/company/example-robotics/",
    ),
    # Nothing to change.
    ("https://jobs.example.com/ad/1", "https://jobs.example.com/ad/1"),
]


@pytest.mark.parametrize(("url", "expected"), CASES)
def test_canonical(url: str, expected: str) -> None:
    assert canonical(url) == expected


@pytest.mark.parametrize(("url", "expected"), CASES)
def test_canonical_is_idempotent(url: str, expected: str) -> None:
    assert canonical(canonical(url)) == canonical(url)


def test_a_host_that_only_looks_like_linkedin_is_not_rewritten() -> None:
    url = "https://notlinkedin.com/jobs/view/4000000001/"
    assert canonical(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/jobs/view/1/",
        "https://www.linkedin.com/jobs/view/1/",
        "https://ca.linkedin.com/jobs/view/1/",
        "https://WWW.LinkedIn.COM./x",
        "https://lnkd.in/abc",
    ],
)
def test_linkedin_is_never_fetched(url: str) -> None:
    assert no_fetch(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://notlinkedin.com/x",
        "https://linkedin.com.example.org/x",
        "https://jobs.example.com/linkedin.com",
        "https://sub.lnkd.in.example.net/x",
        "not a url",
        "",
    ],
)
def test_other_hosts_may_be_fetched(url: str) -> None:
    assert not no_fetch(url)
