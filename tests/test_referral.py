"""The referral label (Batch R): set at /log or careerhunter log, or on a stored posting.
A referral counts as local, needs no location, and its alert says Referral."""

from __future__ import annotations

from pathlib import Path

import pytest

from careerhunter import cli, db, filters
from careerhunter.filters import PostingText
from tests import test_bot, test_bot_digest, test_log
from tests.conftest import FakeProbe
from tests.test_bot import AD_URL, SHELL_URL, Env, _poll, _rows
from tests.test_log import DAY, Pages
from tests.test_log import _rows as rows_of

env = test_bot.env
ready = test_log.ready
pages = test_log.pages

RULES = {"onsite_hybrid": ["Example City"], "remote": ["Exampleland"]}


# --- the gates --------------------------------------------------------------------------


def _decide(
    location: str, *, referral: bool, description: str | None = None, **kw: object
) -> str | None:
    return filters.decide(
        PostingText(
            company="Example Robotics",
            title="Analyst",
            location=location,
            description=description,
            referral=referral,
        ),
        config={"search": {"location_rules": RULES}},
        exclusions={},
        role_filters=kw.get("role_filters"),  # type: ignore[arg-type]
        applied=False,
    ).reason


def test_a_referral_counts_as_local_wherever_it_is() -> None:
    assert _decide("Faraway Town", referral=False) == "location_rule"
    assert _decide("Faraway Town", referral=True) is None
    assert _decide("", referral=True) is None


def test_a_referral_is_never_skipped_for_office_days_elsewhere() -> None:
    text = "You will work 3 days a week in the office in Faraway Town."
    assert _decide("Example City", referral=False, description=text) == "office_days_elsewhere"
    assert _decide("Example City", referral=True, description=text) is None


def test_a_referral_passes_a_watchlist_entrys_locations() -> None:
    roles = {"locations": ["Example City"]}
    assert _decide("Faraway Town", referral=False, role_filters=roles) == "location_rule"
    verdict = filters.decide(
        PostingText(
            company="Example Robotics", title="Analyst", location="Faraway Town", referral=True
        ),
        config={},
        exclusions={},
        role_filters=roles,
        applied=False,
    )
    assert verdict.reason is None


def test_a_referral_still_meets_every_other_gate() -> None:
    verdict = filters.decide(
        PostingText(company="Example Robotics", title="Senior Analyst", location="", referral=True),
        config={"hard_gates": {"seniority_words": ["senior"]}},
        exclusions={},
        role_filters=None,
        applied=False,
    )
    assert verdict.reason == "seniority"


# --- /log ------------------------------------------------------------------------------


def _referrals(env: Env) -> list[int]:
    conn = db.connect_read_only(env.datadir.search_db_path)
    try:
        return [int(r[0]) for r in conn.execute("SELECT referral FROM postings ORDER BY id")]
    finally:
        conn.close()


def _methods(env: Env) -> list[str]:
    return [str(r["method"]) for r in test_bot.ei_apps(env)]


def test_a_referral_field_marks_the_posting_and_keeps_the_method(env: Env) -> None:
    env.say(f"/log {AD_URL} | Company site | 2026-10-21 | referral")
    assert env.api.texts()[-2].endswith("\nLocation: Example City\nReferral")
    assert _referrals(env) == [1]
    assert _methods(env) == ["company_site"]


def test_the_referral_method_marks_the_posting_too(env: Env) -> None:
    env.say(f"/log {AD_URL} | referral | 2026-10-21")
    assert _referrals(env) == [1]
    assert _methods(env) == ["referral"]
    assert env.api.texts()[-2].endswith("\nReferral")


def test_a_bare_log_keeps_its_referral_through_the_buttons(env: Env) -> None:
    env.say(f"/log {AD_URL} | referral")
    env.press("Email")
    env.press("Today")
    assert _referrals(env) == [1]
    assert _methods(env) == ["email"]


def test_a_log_without_it_is_not_a_referral(env: Env) -> None:
    env.say(f"/log {SHELL_URL} | email | 2026-10-21 | Sample Logistics | Synthetic Clerk")
    assert _referrals(env) == [0]
    assert "Referral" not in env.api.texts()[-2]


def test_referral_given_twice_is_refused(env: Env) -> None:
    env.say(f"/log {AD_URL} | email | 2026-10-21 | referral | referral")
    assert env.api.texts()[-1] == "Give referral once only."
    assert env.counts()["search.postings"] == 0


# --- careerhunter log and careerhunter referral -------------------------------------------


def test_log_takes_referral(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["log", test_log.AD_URL, "--method", "email", "--date", DAY, "--referral"], probe=probe
    )
    assert code == 0
    assert "Referral: it counts as local and needs no location." in capsys.readouterr().out
    assert rows_of(ready / "search.db", "SELECT referral FROM postings") == [(1,)]


def _stored(root: Path, *, skipped: str | None = None) -> int:
    conn = db.connect(root / "search.db")
    try:
        conn.execute("INSERT INTO companies (name, source) VALUES ('Example Robotics', 'manual')")
        cur = conn.execute(
            "INSERT INTO postings (company_id, title, url, first_seen, source) "
            "VALUES (1, 'Analyst', 'https://jobs.example.com/ref/1', '2026-11-02', 'watcher')"
        )
        posting_id = int(cur.lastrowid or 0)
        if skipped:
            conn.execute(
                "INSERT INTO skips (posting_id, date, reason) VALUES (?, '2026-11-02', ?)",
                (posting_id, skipped),
            )
        return posting_id
    finally:
        conn.close()


def test_the_referral_command_marks_a_stored_posting_by_id_or_link(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    posting = _stored(ready)
    capsys.readouterr()
    assert cli.main(["referral", "--posting", str(posting)], probe=probe) == 0
    assert capsys.readouterr().out.strip() == (
        f"posting {posting} (Example Robotics — Analyst) is now marked as a referral."
    )
    link = "https://jobs.example.com/ref/1?utm_source=x"
    assert cli.main(["referral", "--url", link], probe=probe) == 0
    assert "was already marked as a referral." in capsys.readouterr().out
    assert rows_of(ready / "search.db", "SELECT referral FROM postings") == [(1,)]


def test_the_referral_command_says_a_skip_stays(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    posting = _stored(ready, skipped="location_rule")
    capsys.readouterr()
    assert cli.main(["referral", "--posting", str(posting)], probe=probe) == 0
    assert "Its skip (location_rule) stays" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--posting", "99"], "no stored posting has that id or link"),
        (["--url", "https://jobs.example.com/none"], "no stored posting has that id or link"),
        ([], "give --posting or --url, not both"),
        (["--posting", "1", "--url", "https://jobs.example.com/x"], "not both"),
    ],
)
def test_the_referral_command_refuses_what_it_cannot_mark(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str], args: list[str], error: str
) -> None:
    capsys.readouterr()
    assert cli.main(["referral", *args], probe=probe) == cli.EXIT_ERROR
    assert error in capsys.readouterr().err


# --- the alert -----------------------------------------------------------------------------


def test_a_referrals_alert_says_referral_and_needs_no_location(env: Env) -> None:
    placed = test_bot_digest._post(env, "Placed Analyst")
    unplaced = test_bot_digest._post(env, "Unplaced Analyst", fit="medium")
    test_bot_digest._set(env, placed, location="Example City", referral=1)
    test_bot_digest._set(env, unplaced, location="", referral=1)
    assert _poll(env) == 0
    rows = _rows(env)
    assert rows[0][1].startswith("Example City | Referral | Lane: operations | Fit: High")
    assert rows[1][1].startswith("Referral | Lane: operations | Fit: Medium")
