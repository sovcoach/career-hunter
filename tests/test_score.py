"""Local scoring. A fake transport stands in for the model; no socket is opened."""

from __future__ import annotations

import http.client
import json
import sqlite3

import pytest

from careerhunter import loopback, watch
from careerhunter import score as scoring
from careerhunter.deadline import Deadline
from careerhunter.fetch import Fetched
from careerhunter.filters import PostingText, decide
from careerhunter.resume import Resume
from careerhunter.score import (
    DEFAULT_URL,
    Score,
    ScoreError,
    default_transport,
    normalized,
    require_loopback,
    score_posting,
)
from tests.fakes import FakeClock, FakeServer, ManualWatchdog, Reply
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, postings, url

RUBRIC = {
    "fit_rubric": {
        "gap_tiers": {
            "tiers": [
                {"name": "strong", "max_gaps": 1},
                {"name": "possible", "max_gaps": 2},
            ],
            "exclude_from_gaps": 3,
            "boundary": {"at_gaps": 1, "all_minor": "strong", "any_core": "possible"},
            "exempt_requirements": ["degree"],
        }
    },
    "search": {
        "lanes": [{"name": "operations", "priority": 1, "titles": ["Operations Coordinator"]}],
        "location_rules": {"remote": ["Canada"]},
    },
    "hard_gates": {"seniority_words": ["Senior"], "min_salary": None},
    "notes": "DO-NOT-LEAK-NOTES",
    "telegram": {"chat_id": 424242},
    "fit_weights_sentinel": "DO-NOT-LEAK-WEIGHTS",
}


def posting() -> PostingText:
    return PostingText(
        "Example Robotics",
        "Operations Coordinator",
        "Example City",
        work_model="remote",
        description="A synthetic posting.",
    )


def reply(content: str, status: int = 200) -> tuple[int, bytes]:
    body = {"choices": [{"message": {"content": content}}]}
    return status, json.dumps(body).encode()


def test_only_a_loopback_http_url_is_accepted() -> None:
    host, port, path = require_loopback(DEFAULT_URL)
    assert (host, port, path) == ("127.0.0.1", 1234, "/v1/chat/completions")
    assert require_loopback("http://localhost/v1")[0] == "localhost"
    assert require_loopback("http://[::1]:9/v1")[:2] == ("::1", 9)
    for bad in (
        "https://127.0.0.1/v1",
        "http://example.com/v1",
        "http://10.0.0.1/v1",
        "http://user:secret@127.0.0.1/v1",
    ):
        with pytest.raises(ScoreError):
            require_loopback(bad)


def test_the_prompt_uses_only_the_rubric_and_the_posting() -> None:
    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["url"] = url.encode()
        seen["body"] = payload
        return reply('{"gaps":[]}')

    result = score_posting(posting(), RUBRIC, transport=transport)
    assert result is not None and result.tier == "strong" and result.gaps == "[]"
    assert result.excluded is False
    text = seen["body"].decode()
    assert seen["url"] == DEFAULT_URL.encode()
    assert "operations" in text and "Canada" in text and "Senior" in text
    assert "A synthetic posting." in text
    assert "DO-NOT-LEAK-NOTES" not in text
    assert "DO-NOT-LEAK-WEIGHTS" not in text
    assert "424242" not in text
    assert "local-model" in text


def test_notes_do_not_change_a_verdict_or_enter_the_score() -> None:
    plain = {
        "hard_gates": {"seniority_words": ["Senior"]},
        "search": {"location_rules": {"remote": ["Canada"]}},
        "fit_rubric": RUBRIC["fit_rubric"],
    }
    noted = {**plain, "notes": "DO-NOT-LEAK-NOTES"}
    exclusions = {"companies": ["Example Staffing Mill"], "notes": "DO-NOT-LEAK-NOTES"}
    kept = PostingText("Example Robotics", "Operations Coordinator", "Canada", work_model="remote")
    assert decide(kept, config=plain, exclusions={}, role_filters=None, applied=False) == decide(
        kept, config=noted, exclusions=exclusions, role_filters=None, applied=False
    )
    senior = PostingText("Example Robotics", "Senior Coordinator", "Canada", work_model="remote")
    skipped = decide(senior, config=plain, exclusions={}, role_filters=None, applied=False)
    assert skipped.reason == "seniority"
    assert (
        decide(senior, config=noted, exclusions=exclusions, role_filters=None, applied=False)
        == skipped
    )

    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["body"] = payload
        return reply('{"gaps":[]}')

    assert score_posting(posting(), noted, transport=transport) is not None
    assert "DO-NOT-LEAK-NOTES" not in seen["body"].decode()


def test_gap_tiers_name_the_tier_and_a_high_count_skips() -> None:
    def run(content: str) -> Score | None:
        return score_posting(posting(), RUBRIC, transport=lambda url, body: reply(content))

    empty = run('{"gaps":[]}')
    assert empty is not None and empty.tier == "strong" and empty.excluded is False
    minor = run('{"gaps":[{"name":"spreadsheets","kind":"minor","type":"skill"}]}')
    assert minor is not None and minor.tier == "strong"
    assert json.loads(minor.gaps) == [{"name": "spreadsheets", "kind": "minor", "type": "skill"}]
    core = run('```json\n{"gaps":[{"name":"French","kind":"core","type":"language"}]}\n```')
    assert core is not None and core.tier == "possible"
    two = run('{"gaps":[{"name":"French","kind":"core"},{"name":"Excel","kind":"minor"}]}')
    assert two is not None and two.tier == "possible"
    exempt = run(
        '{"gaps":[{"name":"a degree","kind":"core","type":"degree"},'
        '{"name":"Excel","kind":"minor","type":"skill"}]}'
    )
    assert exempt is not None and exempt.tier == "strong"
    assert json.loads(exempt.gaps) == [{"name": "Excel", "kind": "minor", "type": "skill"}]
    over = run(
        '{"gaps":[{"name":"a","kind":"core"},{"name":"b","kind":"core"},{"name":"c","kind":"minor"}]}'
    )
    assert over is not None and over.excluded and over.tier is None
    assert score_posting(posting(), RUBRIC, transport=lambda url, body: reply("nope")) is None


@pytest.mark.parametrize("status", [302, 404, 500, 503])
def test_a_status_other_than_200_is_a_score_error(status: int) -> None:
    """The model did not answer: told apart from a reply that does not parse."""
    with pytest.raises(ScoreError, match=f"answered HTTP {status}") as caught:
        score_posting(posting(), RUBRIC, transport=lambda url, body: reply("{}", status))
    assert caught.value.__cause__ is None


def test_a_down_server_is_a_score_error() -> None:
    def down(url: str, body: bytes) -> tuple[int, bytes]:
        raise OSError("synthetic")

    with pytest.raises(ScoreError, match="could not be reached") as caught:
        score_posting(posting(), RUBRIC, transport=down)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_non_loopback_url_is_not_requested() -> None:
    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        raise AssertionError("transport called")

    with pytest.raises(ScoreError):
        score_posting(posting(), RUBRIC, url="http://example.com/v1", transport=transport)
    assert score_posting(posting(), {"fit_rubric": {}}, transport=transport) is None


def test_the_default_transport_posts_once_and_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(Reply(302, b"go away"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    status, body = default_transport(DEFAULT_URL, b"{}")
    assert status == 302 and body == b"go away"
    assert [(host, address, port) for host, address, port, _t in server.opened] == [
        ("127.0.0.1", "127.0.0.1", 1234)
    ]
    assert [(method, path, sent) for method, path, sent, _h in server.requests] == [
        ("POST", "/v1/chat/completions", b"{}")
    ]
    assert server.closes == 1


def test_a_refused_connection_stays_a_score_error(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(Reply(error=ConnectionRefusedError("synthetic"), error_at="request"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    with pytest.raises(ScoreError):
        default_transport(DEFAULT_URL, b"{}")


def test_watch_scores_a_kept_posting_and_leaves_a_failure_unscored(
    search: sqlite3.Connection,
) -> None:
    board = url("greenhouse", "examplerobotics")
    item = company("Example Robotics", "greenhouse", "examplerobotics")
    payload = one_greenhouse("examplerobotics")

    def good(text: PostingText) -> Score:
        assert text.description is None  # Greenhouse list rows have no description
        return Score(llm_output="model text", gaps="[]", tier="strong")

    lines = watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config=RUBRIC,
        scorer=good,
    )
    assert "left unscored" not in "\n".join(lines)
    row = postings(search)[0]
    assert row["fit_rating"] is None
    assert row["gaps"] == "[]"
    assert row["llm_output"] == "model text"
    assert row["scout_metadata"] == '{"tier": "strong"}'
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0

    other = url("lever", "samplelogistics")
    second = company("Sample Logistics", "lever", "samplelogistics")

    def mixed(text: PostingText) -> Score | None:
        if text.company == "Sample Logistics":
            raise OSError("synthetic")
        return Score(llm_output="kept", gaps="[]", tier="possible")

    later = watch.run(
        search,
        [item, second],
        today=DAY,
        fetcher=Scripted(
            {
                board: fetched(payload),
                other: fetched(
                    [
                        {
                            "id": "abc-123",
                            "text": "Operations Coordinator",
                            "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
                            "categories": {"location": "Canada"},
                            "workplaceType": "remote",
                        }
                    ]
                ),
            }
        ),
        config=RUBRIC,
        scorer=mixed,
    )
    rows = {row["company"]: row for row in postings(search)}
    # The first posting was already scored, so this run does not score it again.
    assert rows["Example Robotics"]["llm_output"] == "model text"
    assert rows["Sample Logistics"]["llm_output"] is None
    assert rows["Sample Logistics"]["fit_rating"] is None
    assert any("left unscored" in line for line in later)


def test_a_later_run_scores_waiting_rows_after_the_fetch(search: sqlite3.Connection) -> None:
    board = url("ashby", "exampleorg")
    item = company("Example Org", "ashby", "exampleorg")
    payload = {
        "jobs": [
            {
                "id": "job-1",
                "title": "Operations Coordinator",
                "jobUrl": "https://jobs.ashbyhq.com/exampleorg/job-1",
                "location": "Canada",
                "workplaceType": "Remote",
                "descriptionPlain": "Needs French.",
            }
        ]
    }
    seen: list[str | None] = []

    def first(text: PostingText) -> None:
        seen.append(text.description)
        return None

    watch.run(
        search,
        [item],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        scorer=first,  # type: ignore[arg-type]
    )
    assert seen == ["Needs French."]
    assert postings(search)[0]["llm_output"] is None
    order: list[str] = []

    def second(text: PostingText) -> Score:
        order.append("score")
        assert text.description is None
        return Score(
            llm_output="raw",
            gaps='[{"name":"a","kind":"core"},{"name":"b","kind":"core"},{"name":"c","kind":"minor"}]',
            tier=None,
            excluded=True,
        )

    def fetcher(address: str) -> Fetched:
        order.append("fetch")
        return fetched(payload)

    lines = watch.run(search, [item], today=DAY, fetcher=fetcher, scorer=second)
    assert order == ["fetch", "score"]  # the boards get first claim on the run budget
    assert any("skipped (gap_tier)" in line for line in lines)
    row = postings(search)[0]
    assert row["llm_output"] == "raw"
    assert row["scout_metadata"] is None
    assert row["fit_rating"] is None
    assert search.execute("SELECT reason FROM skips").fetchone()["reason"] == "gap_tier"


BROKEN = [
    http.client.IncompleteRead(b""),
    http.client.RemoteDisconnected("synthetic"),
    http.client.BadStatusLine("synthetic"),
]


def broken_connection(error: BaseException, *, at: str = "response") -> FakeServer:
    """A fake local server whose reply breaks, at the status line or in the body."""
    return FakeServer(Reply(error=error, error_at=at))


@pytest.mark.parametrize("error", BROKEN, ids=lambda e: type(e).__name__)
def test_a_broken_reply_is_a_score_error(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(loopback, "LoopbackConnection", broken_connection(error))
    with pytest.raises(ScoreError):
        default_transport(DEFAULT_URL, b"{}")


def test_a_reply_that_breaks_mid_body_is_a_score_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loopback,
        "LoopbackConnection",
        broken_connection(http.client.IncompleteRead(b"par"), at="read"),
    )
    with pytest.raises(ScoreError):
        default_transport(DEFAULT_URL, b"{}")


def test_an_injected_transport_that_breaks_is_a_score_error() -> None:
    def broken(url: str, payload: bytes) -> tuple[int, bytes]:
        raise http.client.IncompleteRead(b"")

    with pytest.raises(ScoreError, match="could not be reached"):
        score_posting(posting(), RUBRIC, transport=broken)


def _message(content: object) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(json.dumps({"error": "model not loaded"}).encode(), id="error-object"),
        pytest.param(json.dumps({"choices": []}).encode(), id="no-choices"),
        pytest.param(json.dumps({"choices": ["x"]}).encode(), id="choice-not-object"),
        pytest.param(json.dumps({"choices": [{}]}).encode(), id="no-message"),
        pytest.param(json.dumps({"choices": [{"message": "x"}]}).encode(), id="message-text"),
        pytest.param(_message(""), id="empty-content"),
        pytest.param(_message(None), id="null-content"),
        pytest.param(_message(42), id="number-content"),
        pytest.param(_message("[1]"), id="content-not-object"),
        pytest.param(_message('{"gaps": "x"}'), id="gaps-not-list"),
        pytest.param(_message('{"gaps": [{"kind": "minor"}]}'), id="gap-without-name"),
        pytest.param(_message('{"gaps": [{"name": "SQL", "kind": "major"}]}'), id="bad-kind"),
        pytest.param(
            _message('{"gaps": [{"name": "SQL", "kind": "minor", "type": 5}]}'), id="bad-type"
        ),
    ],
)
def test_a_reply_of_the_wrong_shape_leaves_the_posting_unscored(body: bytes) -> None:
    assert score_posting(posting(), RUBRIC, transport=lambda url, payload: (200, body)) is None


def test_a_bug_in_our_own_parser_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def bug(content: str) -> object:
        raise RuntimeError("synthetic bug")

    monkeypatch.setattr(scoring, "_parse_gaps", bug)
    with pytest.raises(RuntimeError, match="synthetic bug"):
        score_posting(posting(), RUBRIC, transport=lambda url, payload: reply('{"gaps": []}'))


def test_a_broken_model_reply_leaves_postings_unscored_and_the_run_finishes(
    search: sqlite3.Connection,
) -> None:
    first = url("greenhouse", "examplerobotics")
    second = url("greenhouse", "samplelogistics")

    def broken(address: str, payload: bytes) -> tuple[int, bytes]:
        raise http.client.RemoteDisconnected("synthetic")

    def scorer(text: PostingText) -> Score | None:
        return score_posting(text, RUBRIC, transport=broken)

    lines = watch.run(
        search,
        [
            company("Example Robotics", "greenhouse", "examplerobotics"),
            company("Sample Logistics", "greenhouse", "samplelogistics"),
        ],
        today=DAY,
        fetcher=Scripted(
            {
                first: fetched(one_greenhouse("examplerobotics")),
                second: fetched(one_greenhouse("samplelogistics", ident=2002)),
            }
        ),
        config=RUBRIC,
        scorer=scorer,
    )
    assert len(postings(search)) == 2
    assert sum("left unscored" in line for line in lines) == 2
    assert all(row["llm_output"] is None for row in postings(search))


def _model(monkeypatch: pytest.MonkeyPatch, header_cost: float) -> tuple[FakeServer, FakeClock]:
    clock = FakeClock()
    server = FakeServer(Reply(body=reply('{"gaps": []}')[1], clock=clock, header_cost=header_cost))
    monkeypatch.setattr(loopback, "_clock", clock)
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    return server, clock


def test_a_reply_whose_headers_arrive_at_100_seconds_is_scored(
    monkeypatch: pytest.MonkeyPatch, loopback_watchdog: ManualWatchdog
) -> None:
    server, _clock = _model(monkeypatch, header_cost=100.0)
    result = score_posting(posting(), RUBRIC)
    assert result is not None and result.tier == "strong"
    # The reply does not stream, so one step may take the whole total.
    assert [timeout for *_rest, timeout in server.opened] == [120.0]
    assert [a.seconds for a in loopback_watchdog.armed] == [120.0]
    assert [a.cancels for a in loopback_watchdog.armed] == [1]


def test_a_reply_whose_headers_arrive_at_121_seconds_is_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _clock = _model(monkeypatch, header_cost=121.0)
    with pytest.raises(ScoreError, match="could not be reached"):
        score_posting(posting(), RUBRIC)
    assert server.reads == 0  # the body was never read
    assert server.closes == 1


def test_the_run_budget_can_end_scoring_sooner(monkeypatch: pytest.MonkeyPatch) -> None:
    server, clock = _model(monkeypatch, header_cost=50.0)
    with pytest.raises(ScoreError):
        default_transport(DEFAULT_URL, b"{}", budget=Deadline(40.0, clock))
    assert [timeout for *_rest, timeout in server.opened] == [40.0]


def test_the_prompt_shows_the_seniority_option() -> None:
    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["body"] = payload
        return reply('{"gaps":[]}')

    config = {
        **RUBRIC,
        "hard_gates": {"seniority_words": ["Senior"], "seniority_in_description": True},
    }
    assert score_posting(posting(), config, transport=transport) is not None
    prompt = json.loads(json.loads(seen["body"])["messages"][1]["content"])
    assert prompt["hard_gates"]["seniority_in_description"] is True


# --- the resume in the prompt, and D-A1 ------------------------------------------------------

_RESUME_TEXT = (
    "Avery Example. Coordinated warehouse scheduling for Example Widgets Ltd, "
    "reducing late shipments across three sites."
)
_RESUME = Resume(_RESUME_TEXT, "ab" * 32)


def _with_resume(content: str) -> tuple[Score | None, list[bytes]]:
    sent: list[bytes] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        sent.append(payload)
        return reply(content)

    return score_posting(posting(), RUBRIC, transport=transport, resume=_RESUME), sent


def test_the_resume_is_in_the_request_and_not_in_the_score() -> None:
    result, sent = _with_resume(
        'Sure! {"gaps":[{"name":"French","kind":"core","type":"language"}]} Hope that helps.'
    )
    assert result is not None and result.tier == "possible"
    view = json.loads(json.loads(sent[0])["messages"][1]["content"])
    assert view["resume"] == _RESUME_TEXT
    assert result.llm_output == '{"gaps":[{"kind":"core","name":"French","type":"language"}]}'
    assert "Hope that helps" not in result.llm_output
    assert result.resume_sha256 == _RESUME.sha256


def test_without_a_resume_the_request_has_none_and_the_reply_is_kept() -> None:
    sent: list[bytes] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        sent.append(payload)
        return reply('{"gaps":[]} trailing words')

    result = score_posting(posting(), RUBRIC, transport=transport)
    assert result is not None and result.resume_sha256 is None
    assert "resume" not in json.loads(json.loads(sent[0])["messages"][1]["content"])
    assert result.llm_output == '{"gaps":[]} trailing words'


def test_canonical_gaps_keep_unicode_and_sort_keys() -> None:
    result, _sent = _with_resume('{"gaps":[{"type":"langue","kind":"minor","name":"Français"}]}')
    assert result is not None
    assert result.llm_output == '{"gaps":[{"kind":"minor","name":"Français","type":"langue"}]}'


@pytest.mark.parametrize("field", ["name", "type"])
def test_a_gap_text_over_120_characters_is_unparseable(field: str) -> None:
    long = "x" * 121
    gap = {"name": "Excel", "kind": "minor", field: long}
    result, _sent = _with_resume(json.dumps({"gaps": [gap]}))
    assert result is None
    ok = {"name": "Excel", "kind": "minor", field: "y" * 120}
    assert _with_resume(json.dumps({"gaps": [ok]}))[0] is not None


_RUN_40 = _RESUME_TEXT[10:50]  # exactly 40 characters of the resume
assert len(_RUN_40) == 40


@pytest.mark.parametrize(
    "copied",
    [
        _RUN_40,
        _RUN_40.upper(),
        _RUN_40.replace(" ", "\n"),
        _RUN_40.replace(" ", "\t\t"),
        _RUN_40.replace(" ", "\u00a0"),
        "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in _RUN_40),
        "Missing: " + _RUN_40 + " (from the resume)",
    ],
    ids=["exact", "upper", "newlines", "tabs", "nbsp", "full-width", "inside"],
)
@pytest.mark.parametrize("field", ["name", "type"])
def test_forty_characters_copied_from_the_resume_is_unparseable(copied: str, field: str) -> None:
    gap = {"name": "Excel", "kind": "minor", field: copied}
    result, _sent = _with_resume(json.dumps({"gaps": [gap]}))
    assert result is None


def test_thirty_nine_copied_characters_pass() -> None:
    gap = {"name": _RUN_40[:39], "kind": "minor"}
    result, _sent = _with_resume(json.dumps({"gaps": [gap]}))
    assert result is not None


def test_the_copy_check_only_applies_with_a_resume() -> None:
    gap = {"name": _RUN_40, "kind": "minor"}
    result = score_posting(
        posting(), RUBRIC, transport=lambda url, body: reply(json.dumps({"gaps": [gap]}))
    )
    assert result is not None


def test_normalization_is_nfkc_casefold_and_single_spaces() -> None:
    assert normalized("  \uff21very\u00a0\t EXAMPLE\nLtd  ") == "avery example ltd"
