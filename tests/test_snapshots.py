"""Job-ad snapshots: reading the ad, the ATS fallback, and owner-only files."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from careerhunter import snapshots as s
from careerhunter.fetch import Fetched, FetchError

JSON_LD = json.dumps(
    {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage"},
            {
                "@type": "JobPosting",
                "title": "Synthetic Analyst &amp; Coordinator",
                "hiringOrganization": {"@type": "Organization", "name": "Example Robotics"},
                "jobLocation": [{"address": {"addressLocality": "Example City"}}],
            },
        ],
    }
)
AD = (
    "<html><title>Ignored</title>"
    f'<script type="application/ld+json">{JSON_LD}</script><p>Short.</p></html>'
).encode()
SHELL = b"<html><title>Careers</title><div id='root'></div><script>app()</script></html>"
LONG = (
    b"<html><title>Clerk - Sample Logistics</title><p>"
    + b"Synthetic duties. " * 120
    + b"</p></html>"
)


def page(body: bytes, url: str = "https://jobs.example.com/1", status: int = 200) -> Fetched:
    return Fetched(url, status, "text/html", body)


def test_posting_info_from_json_ld() -> None:
    info = s.posting_info(AD)
    assert info == s.PostingInfo(
        "Synthetic Analyst & Coordinator", "Example Robotics", "Example City"
    )


def test_posting_info_falls_back_to_the_title() -> None:
    assert s.posting_info(LONG) == s.PostingInfo(title="Clerk - Sample Logistics")
    assert (
        s.posting_info(b'<script type="application/ld+json">{not json</script>') == s.PostingInfo()
    )


def test_readability() -> None:
    assert s.is_readable(AD)
    assert s.is_readable(LONG)
    assert not s.is_readable(SHELL)


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        (
            "https://example.wd3.myworkdayjobs.com/en-US/External/job/Example-City/Synthetic-Analyst_R-123",
            "https://example.wd3.myworkdayjobs.com/wday/cxs/example/External/job/Example-City/Synthetic-Analyst_R-123",
        ),
        (
            "https://boards.greenhouse.io/examplerobotics/jobs/4567",
            "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/4567",
        ),
        (
            "https://jobs.lever.co/samplelogistics/abc-123",
            "https://api.lever.co/v0/postings/samplelogistics/abc-123",
        ),
        (
            "https://jobs.ashbyhq.com/exampleorg/uuid-1",
            "https://api.ashbyhq.com/posting-api/job-board/exampleorg#uuid-1",
        ),
        ("https://careers.example.com/jobs/1", None),
        ("https://boards.greenhouse.io/examplerobotics/jobs/not-a-number", None),
    ],
)
def test_ats_json_endpoints(url: str, endpoint: str | None) -> None:
    assert s.ats_json_url(url) == endpoint


def test_capture_keeps_a_readable_page() -> None:
    got = s.capture("https://jobs.example.com/1", lambda url: page(AD))
    assert got == s.Capture(AD, "html", "page")


def test_capture_uses_workday_json_for_a_js_shell() -> None:
    ad_url = "https://example.wd3.myworkdayjobs.com/External/job/Example-City/Synthetic_R-9"
    api = "https://example.wd3.myworkdayjobs.com/wday/cxs/example/External/job/Example-City/Synthetic_R-9"
    data = {"jobPostingInfo": {"title": "Synthetic Analyst", "jobDescription": "<p>Synthetic</p>"}}

    def fetcher(url: str) -> Fetched:
        return page(SHELL, ad_url) if url == ad_url else page(json.dumps(data).encode(), api)

    got = s.capture(ad_url, fetcher)
    assert got is not None and got.source == "ats_json" and got.ext == "html"
    assert b"Synthetic Analyst" in got.data and api.encode() in got.data


def test_capture_picks_the_one_ashby_job() -> None:
    board = {
        "jobs": [{"id": "other", "title": "Other"}, {"id": "uuid-1", "title": "Synthetic Role"}]
    }

    def fetcher(url: str) -> Fetched:
        return (
            page(SHELL, url) if "jobs.ashbyhq.com" in url else page(json.dumps(board).encode(), url)
        )

    got = s.capture("https://jobs.ashbyhq.com/exampleorg/uuid-1", fetcher)
    assert got is not None and b"Synthetic Role" in got.data and b"Other" not in got.data


@pytest.mark.parametrize("fails", ["page", "shell"])
def test_capture_gives_up_cleanly(fails: str) -> None:
    def fetcher(url: str) -> Fetched:
        if fails == "page":
            raise FetchError("refused")
        return page(SHELL, url)

    assert s.capture("https://careers.example.com/jobs/1", fetcher) is None


def test_save_is_owner_only_and_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "snapshots").mkdir()
    one = s.save(tmp_path, b"a", "html", dt.date(2026, 10, 28))
    two = s.save(tmp_path, b"b", "html", dt.date(2026, 10, 28))
    assert one != two and one.startswith("snapshots/2026/") and not one.startswith("/")
    assert (tmp_path / one).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "snapshots" / "2026").stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError):
        s.save(tmp_path, b"x", "../sh", dt.date(2026, 10, 28))


def test_upload_types_are_judged_by_their_bytes() -> None:
    assert s.upload_type(b"%PDF-1.7 synthetic") == "pdf"
    assert s.upload_type(b"\x89PNG\r\n\x1a\nsynthetic") == "png"
    assert s.upload_type(b"\xff\xd8\xffsynthetic") == "jpg"
    assert s.upload_type(b"<html>not a pdf</html>") is None
