"""Job-description text: a paste, a file or an allowed fetch, and owner-only saving.

Every text and file here is made up in the test.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

import pypdf
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from careerhunter import jobdesc as j
from careerhunter.fetch import Fetched, FetchError, FetchRefused

TEXT = "About the role\nExample Robotics is looking for a Client Experience Analyst.\n"
TODAY = dt.date(2026, 10, 28)


def make_pdf(pages: list[str]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    for text in pages:
        if text:
            pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def refused(reason: str) -> Any:
    return pytest.raises(j.JobDescError, match=re.escape(j._MESSAGES[reason]))


# --- a paste ---------------------------------------------------------------------------


def test_a_paste_is_kept_verbatim_and_hashed_exactly() -> None:
    jd = j.from_paste("  " + TEXT + "\n\n")
    assert jd.text == "  " + TEXT + "\n\n"
    assert jd.source == "pasted" and jd.file_type == "txt" and jd.has_text
    assert jd.text_sha256 == hashlib.sha256(jd.text.encode()).hexdigest()
    assert jd.file_bytes == jd.text.encode() and jd.file_sha256 == jd.text_sha256
    assert TEXT not in repr(jd)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        (" \n\t", "empty"),
        ("a\x00b", "nul"),
        ("lone \ud800 surrogate", "utf8"),
        ("x" * (j.MAX_CHARS + 1), "too_long"),
    ],
)
def test_a_bad_paste_is_refused(text: str, reason: str) -> None:
    with refused(reason):
        j.from_paste(text)


def test_a_paste_at_the_cap_is_kept() -> None:
    assert len(j.from_paste("y" * j.MAX_CHARS).text) == j.MAX_CHARS


def test_a_paste_from_bytes_must_be_utf8() -> None:
    assert j.from_paste_bytes(TEXT.encode()).text == TEXT
    with refused("utf8"):
        j.from_paste_bytes(b"\xff\xfe not text")


# --- a file ----------------------------------------------------------------------------


def test_a_pdf_gives_its_text_and_keeps_its_bytes() -> None:
    data = make_pdf(["Synthetic duties at Example Robotics."])
    jd = j.from_file(data)
    assert "Synthetic duties" in jd.text
    assert jd.source == "file" and jd.file_type == "pdf" and jd.file_bytes == data
    assert jd.file_sha256 == hashlib.sha256(data).hexdigest() != jd.text_sha256


def test_a_pdf_with_no_text_is_kept_without_a_jd() -> None:
    data = make_pdf([""])
    jd = j.from_file(data)
    assert not jd.has_text and jd.text == "" and jd.text_sha256 is None
    assert jd.file_type == "pdf" and jd.file_bytes == data


def test_an_encrypted_pdf_is_refused() -> None:
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(make_pdf(["Synthetic."]))))
    writer.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    with refused("encrypted"):
        j.from_file(buffer.getvalue())


def test_a_broken_pdf_is_refused() -> None:
    with refused("pdf_unreadable"):
        j.from_file(b"%PDF-1.4\nnot really a pdf")


def test_a_pdf_over_the_page_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j, "MAX_PAGES", 1)
    with refused("too_many_pages"):
        j.from_file(make_pdf(["one", "two"]))


def test_a_pdf_over_the_text_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j, "MAX_CHARS", 10)
    with pytest.raises(j.JobDescError) as caught:
        j.from_file(make_pdf(["Synthetic text longer than ten."]))
    assert caught.value.reason == "too_long"


@pytest.mark.parametrize(
    "data",
    [
        b"<!DOCTYPE html><html><body><h1>Analyst</h1><p>Synthetic duties.</p></body></html>",
        b"\xef\xbb\xbf<html lang='en'><p>Analyst</p><p>Synthetic duties.</p></html>",
        b"<!-- saved from url -->\n<HTML><p>Analyst</p><p>Synthetic duties.</p></HTML>",
    ],
)
def test_html_gives_its_text(data: bytes) -> None:
    jd = j.from_file(data)
    assert jd.file_type == "html" and jd.text == "Analyst\nSynthetic duties."
    assert jd.file_bytes == data


def test_plain_text_is_kept_as_written() -> None:
    jd = j.from_file(TEXT.encode())
    assert jd.file_type == "txt" and jd.text == TEXT


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"\x89PNG\r\n\x1a\nimage", "unsupported"),
        (b"text with a \x00 NUL", "unsupported"),
        (b"\xff\xd8\xff jpeg", "unsupported"),
        (b"   \n", "no_text"),
        (b"<html><script>x()</script></html>", "no_text"),
        (b"<html><p>caf\xe9</p></html>", "utf8"),
    ],
)
def test_a_bad_file_is_refused(data: bytes, reason: str) -> None:
    with pytest.raises(j.JobDescError) as caught:
        j.from_file(data)
    assert caught.value.reason == reason


def test_a_file_over_the_upload_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j, "MAX_FILE_BYTES", 10)
    with refused("too_large"):
        j.from_file(b"x" * 11)


def test_only_a_pdf_may_have_no_text() -> None:
    with pytest.raises(ValueError):
        j.JobDescription("", "pasted", "txt")
    with pytest.raises(ValueError):
        j.JobDescription("x", "scraped", "txt")
    with pytest.raises(ValueError):
        j.JobDescription("x", "file", "docx")
    with pytest.raises(ValueError):
        j.JobDescError("nope")


# --- a fetch ---------------------------------------------------------------------------


def _page(body: bytes, url: str = "https://careers.example.com/jobs/1") -> Fetched:
    return Fetched(url, 200, "text/html", body)


def _ld(description: str) -> bytes:
    data = {"@type": "JobPosting", "title": "Analyst", "description": description}
    return f'<html><script type="application/ld+json">{json.dumps(data)}</script></html>'.encode()


def test_a_fetch_takes_the_json_ld_description() -> None:
    jd = j.fetched(
        "https://careers.example.com/jobs/1",
        lambda url: _page(_ld("<p>Synthetic duties.</p><ul><li>One</li></ul>")),
    )
    assert jd is not None and jd.source == "fetched" and jd.file_type == "txt"
    assert jd.text == "Synthetic duties.\nOne"


@pytest.mark.parametrize(
    ("url", "endpoint", "data", "expected"),
    [
        (
            "https://exampleco.wd3.myworkdayjobs.com/en-US/External/job/City/Analyst_R1",
            "https://exampleco.wd3.myworkdayjobs.com/wday/cxs/exampleco/External/job/City/Analyst_R1",
            {"jobPostingInfo": {"jobDescription": "<p>Workday duties.</p>"}},
            "Workday duties.",
        ),
        (
            "https://boards.greenhouse.io/exampleco/jobs/123",
            "https://boards-api.greenhouse.io/v1/boards/exampleco/jobs/123",
            {"content": "&lt;p&gt;Greenhouse duties.&lt;/p&gt;"},
            "Greenhouse duties.",
        ),
        (
            "https://jobs.lever.co/exampleco/abc",
            "https://api.lever.co/v0/postings/exampleco/abc",
            {"descriptionPlain": "Lever duties."},
            "Lever duties.",
        ),
        (
            "https://jobs.ashbyhq.com/exampleco/j2",
            "https://api.ashbyhq.com/posting-api/job-board/exampleco",
            {
                "jobs": [
                    {"id": "j1", "descriptionPlain": "Other."},
                    {"id": "j2", "descriptionPlain": "Ashby duties."},
                ]
            },
            "Ashby duties.",
        ),
    ],
)
def test_a_fetch_falls_back_to_the_ats_json(
    url: str, endpoint: str, data: dict[str, object], expected: str
) -> None:
    def fetcher(asked: str) -> Fetched:
        if asked == endpoint:
            return Fetched(asked, 200, "application/json", json.dumps(data).encode())
        return _page(b"<html><div id='root'></div></html>", asked)

    jd = j.fetched(url, fetcher)
    assert jd is not None and jd.text == expected


def test_linkedin_is_never_fetched_for_a_jd() -> None:
    def fetcher(url: str) -> Fetched:
        raise AssertionError("LinkedIn must not be fetched")

    assert j.fetched("https://www.linkedin.com/jobs/view/4000000001/", fetcher) is None
    assert j.fetched("https://lnkd.in/abc", fetcher) is None
    assert j.fetched("", fetcher) is None


def test_a_page_that_redirected_to_linkedin_gets_no_ats_fetch() -> None:
    calls: list[str] = []

    def fetcher(url: str) -> Fetched:
        calls.append(url)
        raise FetchRefused("LinkedIn pages are never fetched")

    assert j.fetched("https://careers.example.com/jobs/1", fetcher) is None
    assert calls == ["https://careers.example.com/jobs/1"]


@pytest.mark.parametrize(
    "fetcher",
    [
        lambda url: _page(b"<html><p>No structured data.</p></html>"),
        lambda url: Fetched(url, 404, "text/html", b""),
        lambda url: (_ for _ in ()).throw(FetchError("offline")),
        lambda url: _page(_ld("x" * (j.MAX_CHARS + 1))),
    ],
)
def test_a_fetch_that_finds_nothing_gives_none(fetcher: object) -> None:
    assert j.fetched("https://careers.example.com/jobs/1", fetcher) is None  # type: ignore[arg-type]


def test_a_broken_ats_reply_gives_none() -> None:
    def fetcher(url: str) -> Fetched:
        if "api.lever.co" in url:
            return Fetched(url, 200, "application/json", b"not json")
        return _page(b"<html></html>", url)

    assert j.fetched("https://jobs.lever.co/exampleco/abc", fetcher) is None
    assert j._ats_description("https://example.com/other", {"x": 1}) is None
    assert j._ats_description("https://example.com/wday/cxs/x", {"jobPostingInfo": {}}) is None
    assert j._ats_description("https://example.com/wday/cxs/x", []) is None


# --- saving ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("jd", "ext"),
    [
        (j.from_paste(TEXT), "txt"),
        (j.from_file(b"<html><p>Synthetic duties.</p></html>"), "html"),
        (j.from_file(make_pdf(["Synthetic duties."])), "pdf"),
    ],
)
def test_save_writes_an_owner_only_snapshot(tmp_path: Path, jd: j.JobDescription, ext: str) -> None:
    (tmp_path / "snapshots").mkdir(mode=0o700)
    saved = j.save(tmp_path, jd, TODAY)
    assert saved.path.startswith("snapshots/2026/") and saved.path.endswith("." + ext)
    assert saved.has_text and saved.description is jd
    file = tmp_path / saved.path
    assert file.read_bytes() == jd.file_bytes
    assert hashlib.sha256(file.read_bytes()).hexdigest() == jd.file_sha256
    assert file.stat().st_mode & 0o777 == 0o600
    assert file.parent.stat().st_mode & 0o777 == 0o700
