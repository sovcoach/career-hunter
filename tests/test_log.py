"""`careerhunter log` (#34): a self-sourced application in one step. Synthetic pages only."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pytest

from careerhunter import cli, db, ledger, selfsourced
from careerhunter.fetch import Fetched, FetchError
from careerhunter.models import Dating
from tests.conftest import ACTIVE, FakeProbe, set_claims

AD_URL = "https://jobs.example.com/ads/7"
JSON_LD = {
    "@type": "JobPosting",
    "title": "Synthetic Analyst",
    "hiringOrganization": {"name": "Example Robotics"},
    "jobLocation": {"address": {"addressLocality": "Example City"}},
}
AD = f'<script type="application/ld+json">{json.dumps(JSON_LD)}</script><p>An ad.</p>'.encode()
DAY = "2026-10-20"  # inside the active claim, and before today


class Pages:
    """A fetcher that never opens a socket."""

    def __init__(self, body: bytes | Exception = AD) -> None:
        self.body = body
        self.calls: list[str] = []

    def __call__(self, url: str) -> Fetched:
        self.calls.append(url)
        if isinstance(self.body, Exception):
            raise self.body
        return Fetched(url, 200, "text/html", self.body)


@pytest.fixture
def ready(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    assert cli.main(["migrate"], probe=probe) == 0
    capsys.readouterr()
    return data_env


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> Pages:
    found = Pages()
    monkeypatch.setattr(cli, "fetch_page", found)
    return found


def _log(probe: FakeProbe, *extra: str, url: str = AD_URL) -> int:
    return cli.main(["log", url, "--method", "company_site", "--date", DAY, *extra], probe=probe)


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    conn = db.connect_read_only(path)
    try:
        return [tuple(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def _ei(ready: Path) -> Path:
    return ready / f"ei_record_{ACTIVE.label}.db"


def test_one_command_writes_the_posting_and_the_application_under_one_id(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _log(probe) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"Recorded: Example Robotics — Synthetic Analyst (EI claim {ACTIVE.label})." in out
    assert "Job ad saved." in out
    search = ready / "search.db"
    posting = _rows(search, "SELECT title, location, url, source, snapshot_path FROM postings")
    assert len(posting) == 1
    title, location, url, source, snapshot = posting[0]
    assert (title, location, url, source) == (
        "Synthetic Analyst",
        "Example City",
        AD_URL,
        "self_sourced",
    )
    assert isinstance(snapshot, str) and (ready / snapshot).is_file()
    linked = _rows(
        search, "SELECT correlation_id, ei_claim, method, date_applied FROM applications"
    )
    assert [row[1:] for row in linked] == [(ACTIVE.label, "company_site", DAY)]
    ei = _rows(
        _ei(ready),
        "SELECT correlation_id, employer, role, posting_url, date_applied, snapshot_path "
        "FROM applications",
    )
    assert ei == [(linked[0][0], "Example Robotics", "Synthetic Analyst", AD_URL, DAY, snapshot)]
    assert _rows(search, "SELECT status FROM decision_history") == [("applied",)]
    assert _rows(_ei(ready), "SELECT status FROM status_history") == [("applied",)]


def test_one_ledger_call_writes_both(
    ready: Path, probe: FakeProbe, pages: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = ledger.record_application

    def counted(*args: object, **kwargs: object) -> ledger.Recorded:
        calls.append("record_application")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ledger, "record_application", counted)
    assert _log(probe) == cli.EXIT_OK
    assert calls == ["record_application"]


def test_without_config_nothing_is_fetched_or_written(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    (ready / "config.yaml").unlink()
    assert _log(probe) == cli.EXIT_ERROR
    assert "config.yaml is missing, so nothing was recorded" in capsys.readouterr().err
    assert pages.calls == []
    assert _rows(ready / "search.db", "SELECT count(*) FROM postings") == [(0,)]


def test_names_given_on_the_command_line_need_no_readable_page(
    ready: Path, probe: FakeProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "fetch_page", Pages(FetchError("the page could not be fetched")))
    assert _log(probe, "--company", "Example Gadgets", "--title", "Data Clerk") == cli.EXIT_OK
    assert _rows(_ei(ready), "SELECT employer, role FROM applications") == [
        ("Example Gadgets", "Data Clerk")
    ]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--method", "carrier pigeon", "--date", DAY], "The method must be one of"),
        (["--method", "email", "--date", "2999-01-01"], "is in the future"),
        (["--method", "email", "--date", "last tuesday"], "YYYY-MM-DD, today or yesterday"),
        (["--method", "email", "--date", "2026-08-01"], "outside the active EI claim"),
        (["--method", "email", "--date", DAY, "--company", "Solo"], "together, or neither"),
    ],
)
def test_a_refusal_writes_nothing(
    ready: Path,
    probe: FakeProbe,
    pages: Pages,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    assert cli.main(["log", AD_URL, *argv], probe=probe) == cli.EXIT_ERROR
    assert message in capsys.readouterr().err
    assert _rows(ready / "search.db", "SELECT count(*) FROM postings") == [(0,)]
    assert _rows(_ei(ready), "SELECT count(*) FROM applications") == [(0,)]
    assert not list((ready / "snapshots").rglob("*.*"))


def test_an_unreadable_page_without_names_is_refused(
    ready: Path,
    probe: FakeProbe,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "fetch_page", Pages(FetchError("the page could not be fetched")))
    assert _log(probe) == cli.EXIT_ERROR
    assert "I couldn't read that link" in capsys.readouterr().err
    assert _rows(ready / "search.db", "SELECT count(*) FROM postings") == [(0,)]


def test_the_same_application_twice_is_refused(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _log(probe) == cli.EXIT_OK
    assert _log(probe) == cli.EXIT_ERROR
    assert "That application is already recorded." in capsys.readouterr().err
    assert _rows(_ei(ready), "SELECT count(*) FROM applications") == [(1,)]


def test_a_stored_watcher_posting_is_reused(ready: Path, probe: FakeProbe, pages: Pages) -> None:
    conn = db.connect(ready / "search.db")
    try:
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Robotics', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, url, first_seen, source) "
            "VALUES (?, 'Synthetic Analyst', ?, '2026-10-19', 'watcher')",
            (company, AD_URL),
        )
    finally:
        conn.close()
    assert _log(probe) == cli.EXIT_OK
    rows = _rows(ready / "search.db", "SELECT source, snapshot_path IS NOT NULL FROM postings")
    assert rows == [("watcher", 1)]  # the same posting, now with its snapshot


def _named(probe: FakeProbe, url: str, title: str, day: str) -> int:
    return cli.main(
        [
            "log",
            url,
            "--method",
            "email",
            "--date",
            day,
            "--company",
            "Example Robotics",
            "--title",
            title,
        ],
        probe=probe,
    )


def test_a_repost_logged_again_is_recorded_with_the_earlier_date(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repost match: same company row, same title key, another requisition code."""
    assert _named(probe, "https://jobs.example.com/ads/1", "Analyst (R12345)", DAY) == 0
    capsys.readouterr()
    assert _named(probe, "https://jobs.example.com/ads/2", "Analyst (R67890)", "2026-10-27") == 0
    assert capsys.readouterr().out.splitlines() == [
        f"Recorded: Example Robotics — Analyst (R67890) (EI claim {ACTIVE.label}).",
        "Job ad saved.",
        "No job description was saved.",
        f"You logged an application to this role on {DAY}.",
    ]
    assert _rows(_ei(ready), "SELECT role, date_applied FROM applications ORDER BY id") == [
        ("Analyst (R12345)", DAY),
        ("Analyst (R67890)", "2026-10-27"),
    ]


def test_another_level_is_not_the_same_role(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _named(probe, "https://jobs.example.com/ads/1", "Analyst (Level 2)", DAY) == 0
    capsys.readouterr()
    assert _named(probe, "https://jobs.example.com/ads/2", "Analyst (Level 3)", DAY) == 0
    assert "You logged an application" not in capsys.readouterr().out
    assert _rows(_ei(ready), "SELECT count(*) FROM applications") == [(2,)]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("company_site", "company_site"),
        ("Company site", "company_site"),
        ("LinkedIn", "linkedin_easy_apply"),
        ("recruiter_inbound", "recruiter_inbound"),
        ("  EMAIL ", "email"),
    ],
)
def test_methods_by_code_or_by_word(text: str, code: str) -> None:
    assert selfsourced.parse_method(text) == code


def test_dates_by_word_or_by_day() -> None:
    today = dt.date(2026, 10, 28)
    assert selfsourced.parse_day("today", today) == Dating("exact", day="2026-10-28")
    assert selfsourced.parse_day("Yesterday", today) == Dating("exact", day="2026-10-27")
    assert selfsourced.parse_day("2026-10-01", today) == Dating("exact", day="2026-10-01")
    with pytest.raises(selfsourced.LogError, match="isn't a real date"):
        selfsourced.parse_day("2026-02-30", today)


def test_a_link_owned_by_another_company_is_refused(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = db.connect(ready / "search.db")
    try:
        company = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Example Gadgets', 'watcher')"
        ).lastrowid
        conn.execute(
            "INSERT INTO postings (company_id, title, url, first_seen, source) "
            "VALUES (?, 'Other Role', ?, '2026-10-19', 'watcher')",
            (company, AD_URL),
        )
    finally:
        conn.close()
    assert _log(probe, "--company", "Example Robotics", "--title", "Synthetic Analyst") == 1
    assert "already recorded for another company" in capsys.readouterr().err
    assert _rows(_ei(ready), "SELECT count(*) FROM applications") == [(0,)]


def test_a_page_without_names_says_how_to_give_them(
    ready: Path,
    probe: FakeProbe,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "fetch_page", Pages(b"<html><title></title></html>"))
    assert _log(probe) == cli.EXIT_ERROR
    assert "Add --company and --title." in capsys.readouterr().err


def test_a_link_that_is_too_long_or_a_bad_method_is_refused(tmp_path: Path) -> None:
    with pytest.raises(selfsourced.LogError, match="too long"):
        selfsourced.describe(
            "https://jobs.example.com/" + "a" * 2000, Pages(), dt.date(2026, 10, 1)
        )
    with pytest.raises(selfsourced.LogError, match="isn't recognized"):
        selfsourced.record(
            tmp_path,
            None,
            None,
            None,
            "pigeon",
            Dating("exact", day="2026-10-01"),  # type: ignore[arg-type]
            dt.date(2026, 10, 1),
            Pages(),
        )


# --- the job description (ADR-024) -------------------------------------------------------

LINKEDIN = "https://www.linkedin.com/jobs/view/4000000002/?trk=public_jobs_topcard"


def _jds(ready: Path) -> list[tuple[object, ...]]:
    return _rows(_ei(ready), "SELECT text, jd_source, file_type FROM job_descriptions ORDER BY id")


def test_a_jd_file_is_recorded(
    ready: Path, probe: FakeProbe, pages: Pages, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_text("Synthetic analyst duties.\n")
    assert _log(probe, "--jd-file", str(jd)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Job description saved." in out and "Synthetic analyst" not in out
    assert _jds(ready) == [("Synthetic analyst duties.\n", "file", "txt")]


def test_a_jd_on_stdin_is_recorded_verbatim(
    ready: Path,
    probe: FakeProbe,
    pages: Pages,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = "  About the role\n\nSynthetic duties.\n"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(text.encode())))
    assert _log(probe, "--jd-stdin") == cli.EXIT_OK
    assert _jds(ready) == [(text, "pasted", "txt")]


def test_no_jd_is_said_and_flagged(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _log(probe) == cli.EXIT_OK
    assert "No job description was saved." in capsys.readouterr().out
    assert _rows(_ei(ready), "SELECT jd_expected FROM applications") == [(1,)]


def test_the_two_jd_flags_cannot_be_used_together(
    ready: Path, probe: FakeProbe, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit):
        _log(probe, "--jd-stdin", "--jd-file", str(tmp_path / "jd.txt"))


@pytest.mark.parametrize(
    ("data", "message"), [(b"\x89PNG\r\n\x1a\n", "not a PDF, HTML"), (b"", "No text")]
)
def test_a_bad_jd_file_records_nothing(
    ready: Path,
    probe: FakeProbe,
    pages: Pages,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    data: bytes,
    message: str,
) -> None:
    jd = tmp_path / "jd.bin"
    jd.write_bytes(data)
    assert _log(probe, "--jd-file", str(jd)) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert message in err and "Nothing was recorded." in err
    assert _rows(_ei(ready), "SELECT count(*) FROM applications") == [(0,)]


def test_a_missing_jd_file_is_an_error(
    ready: Path, probe: FakeProbe, pages: Pages, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _log(probe, "--jd-file", str(tmp_path / "missing.txt")) != cli.EXIT_OK
    assert "could not be read" in capsys.readouterr().err


def test_a_linkedin_link_needs_the_names_and_is_never_fetched(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _log(probe, url=LINKEDIN) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "LinkedIn links are never fetched" in err and "Add --company and --title." in err
    assert _log(probe, "--company", "Example Robotics", "--title", "Analyst", url=LINKEDIN) == 0
    assert pages.calls == []
    assert _rows(_ei(ready), "SELECT posting_url FROM applications") == [
        ("https://www.linkedin.com/jobs/view/4000000002/",)
    ]


def test_a_stored_link_is_matched_canonically(ready: Path, probe: FakeProbe, pages: Pages) -> None:
    assert _log(probe, url=AD_URL + "?utm_source=mail") == cli.EXIT_OK
    stored = _rows(ready / "search.db", "SELECT url FROM postings")
    assert stored == [(AD_URL,)]  # saved canonical
    assert (
        cli.main(
            ["log", AD_URL + "#apply", "--method", "email", "--date", "2026-10-21"], probe=probe
        )
        == cli.EXIT_OK
    )
    assert _rows(ready / "search.db", "SELECT count(*) FROM postings") == [(1,)]
