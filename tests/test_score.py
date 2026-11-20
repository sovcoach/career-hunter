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
from tests.fakes import FakeClock, FakeServer, ManualWatchdog, Reply, fit_reply, requirement
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, postings, url

RUBRIC = {
    "fit_rubric": {
        "requirement_tiers": {
            "high_max_minor": 1,
            "entry_level_extra_minor": 2,
            "exclude_at_core": 2,
            "exempt_types": ["education"],
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
        return reply(fit_reply())

    result = score_posting(posting(), RUBRIC, transport=transport)
    assert result is not None and result.tier == "high" and result.gaps == "[]"
    assert result.excluded is False
    text = seen["body"].decode()
    assert seen["url"] == DEFAULT_URL.encode()
    assert "operations" in text and "Canada" in text
    assert "hard_gates" not in text and "Senior" not in text  # the gates ran already
    assert "A synthetic posting." in text
    assert "DO-NOT-LEAK-NOTES" not in text
    assert "DO-NOT-LEAK-WEIGHTS" not in text
    assert "424242" not in text
    assert "local-model" in text


def test_the_request_asks_for_a_schema_reply_with_the_fixed_sampling_and_seed() -> None:
    seen: list[dict[str, object]] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen.append(json.loads(payload))
        return reply(fit_reply())

    score_posting(posting(), RUBRIC, transport=transport)
    score_posting(posting(), RUBRIC, transport=transport)
    first, second = seen
    assert first == second  # the same request each time, seed included
    assert first["temperature"] == 0.6 and first["temperature"] != 0
    assert (first["top_p"], first["top_k"], first["min_p"]) == (0.95, 20, 0.0)
    assert (first["presence_penalty"], first["repeat_penalty"]) == (1.5, 1.0)
    assert first["seed"] == scoring.SEED
    assert {key: first[key] for key in scoring.SAMPLING} == scoring.SAMPLING
    response_format = first["response_format"]
    assert isinstance(response_format, dict) and response_format["type"] == "json_schema"
    spec = response_format["json_schema"]
    assert spec["strict"] is True and spec["name"] == scoring.SCHEMA_NAME
    assert spec["schema"] == scoring.reply_schema(False)
    assert set(spec["schema"]["required"]) == {"requirements", "key_requirements", "entry_level"}
    assert "veto" not in spec["schema"]["properties"]
    item = spec["schema"]["properties"]["requirements"]["items"]
    assert item["properties"]["kind"]["enum"] == [
        "required",
        "preferred",
        "duty",
        "trained",
        "soft",
    ]
    assert item["properties"]["met"]["enum"] == ["yes", "partly", "no"]
    assert set(item["required"]) == set(item["properties"])


def test_with_criteria_the_schema_asks_for_veto_and_cap_or_null() -> None:
    schema = scoring.reply_schema(True)
    assert set(schema["required"]) == {
        "requirements",
        "key_requirements",
        "entry_level",
        "veto",
        "cap",
    }
    assert schema["properties"]["veto"]["anyOf"][0] == {"type": "null"}
    assert schema["additionalProperties"] is False


def test_a_reply_that_breaks_the_schema_does_not_parse() -> None:
    bad_kind = json.dumps({"requirements": [requirement("Excel", "major")]})
    for content in (bad_kind, '{"gaps":[]}', "[]"):
        assert score_posting(posting(), RUBRIC, transport=lambda u, b, c=content: reply(c)) is None


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
        return reply(fit_reply())

    assert score_posting(posting(), noted, transport=transport) is not None
    assert "DO-NOT-LEAK-NOTES" not in seen["body"].decode()


def test_gap_tiers_name_the_tier_and_a_high_count_skips() -> None:
    def run(content: str) -> Score | None:
        return score_posting(posting(), RUBRIC, transport=lambda url, body: reply(content))

    empty = run(fit_reply())
    assert empty is not None and empty.tier == "high" and empty.excluded is False
    minor = run(fit_reply(("spreadsheets", "minor", "skill")))
    assert minor is not None and minor.tier == "high"
    assert json.loads(minor.gaps) == [{"name": "Spreadsheets", "kind": "minor", "type": "skill"}]
    core = run("```json\n" + fit_reply(("French", "core", "language")) + "\n```")
    assert core is not None and core.tier == "medium"
    two = run(fit_reply(("French", "core"), ("Excel", "minor")))
    assert two is not None and two.tier == "medium"
    exempt = run(fit_reply(("a degree", "core", "education"), ("Excel", "minor", "skill")))
    assert exempt is not None and exempt.tier == "high"
    assert json.loads(exempt.gaps) == [{"name": "Excel", "kind": "minor", "type": "skill"}]
    over = run(fit_reply(("a", "core"), ("b", "core"), ("c", "minor")))
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
        return Score(llm_output="model text", gaps="[]", tier="high")

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
    assert row["scout_metadata"] == '{"tier": "high"}'
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0

    other = url("lever", "samplelogistics")
    second = company("Sample Logistics", "lever", "samplelogistics")

    def mixed(text: PostingText) -> Score | None:
        if text.company == "Sample Logistics":
            raise OSError("synthetic")
        return Score(llm_output="kept", gaps="[]", tier="medium")

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
        assert text.description == "Needs French."  # met on its board again this run
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
        pytest.param(_message('{"requirements": "x"}'), id="requirements-not-list"),
        pytest.param(_message('{"requirements": ["SQL"]}'), id="item-not-object"),
        pytest.param(
            _message(json.dumps({"requirements": [requirement("SQL", "major")]})), id="bad-kind"
        ),
        pytest.param(
            _message(json.dumps({"requirements": [requirement("SQL", met="maybe")]})),
            id="bad-met",
        ),
        pytest.param(
            _message(json.dumps({"requirements": [requirement("SQL", years=-1)]})),
            id="negative-years",
        ),
        pytest.param(
            _message(json.dumps({"requirements": [{**requirement("SQL"), "core": "yes"}]})),
            id="core-not-bool",
        ),
        pytest.param(_message(json.dumps({"requirements": [requirement(" ")]})), id="blank-text"),
    ],
)
def test_a_reply_of_the_wrong_shape_leaves_the_posting_unscored(body: bytes) -> None:
    assert score_posting(posting(), RUBRIC, transport=lambda url, payload: (200, body)) is None


def test_a_bug_in_our_own_parser_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def bug(content: str) -> object:
        raise RuntimeError("synthetic bug")

    monkeypatch.setattr(scoring, "_parse_requirements", bug)
    with pytest.raises(RuntimeError, match="synthetic bug"):
        score_posting(posting(), RUBRIC, transport=lambda url, payload: reply(fit_reply()))


def test_a_model_that_stops_answering_is_not_asked_again_this_run(
    search: sqlite3.Connection,
) -> None:
    """The first failure stops scoring; both postings stay unscored and the run finishes."""
    first = url("greenhouse", "examplerobotics")
    second = url("greenhouse", "samplelogistics")
    calls: list[str] = []

    def broken(address: str, payload: bytes) -> tuple[int, bytes]:
        calls.append(address)
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
    assert len(calls) == 1
    assert (
        sum("left unscored (model unreachable: RemoteDisconnected)" in line for line in lines) == 1
    )
    assert sum("left for the next run (model unreachable)" in line for line in lines) == 1
    assert all(row["llm_output"] is None for row in postings(search))
    # The posting the failure happened on keeps its place for the next run.
    stamps = search.execute("SELECT score_attempted_at FROM postings").fetchall()
    assert [row[0] for row in stamps] == [None, None]
    assert lines[-1] == "watch: 2 boards fetched, 0 failed, 0 left"


def _model(monkeypatch: pytest.MonkeyPatch, header_cost: float) -> tuple[FakeServer, FakeClock]:
    clock = FakeClock()
    server = FakeServer(Reply(body=reply(fit_reply())[1], clock=clock, header_cost=header_cost))
    monkeypatch.setattr(loopback, "_clock", clock)
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    return server, clock


def test_a_reply_whose_headers_arrive_at_100_seconds_is_scored(
    monkeypatch: pytest.MonkeyPatch, loopback_watchdog: ManualWatchdog
) -> None:
    server, _clock = _model(monkeypatch, header_cost=100.0)
    result = score_posting(posting(), RUBRIC)
    assert result is not None and result.tier == "high"
    # The reply does not stream, so one step may take the whole total.
    assert [timeout for *_rest, timeout in server.opened] == [120.0]
    assert [a.seconds for a in loopback_watchdog.armed] == [120.0]
    assert [a.cancels for a in loopback_watchdog.armed] == [1]


def test_a_reply_whose_headers_arrive_at_121_seconds_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _clock = _model(monkeypatch, header_cost=121.0)
    with pytest.raises(ScoreError, match="took longer than the timeout") as caught:
        score_posting(posting(), RUBRIC)
    assert caught.value.kind == "timeout"
    assert server.reads == 0  # the body was never read
    assert server.closes == 1


def test_the_run_budget_can_end_scoring_sooner(monkeypatch: pytest.MonkeyPatch) -> None:
    server, clock = _model(monkeypatch, header_cost=50.0)
    with pytest.raises(ScoreError):
        default_transport(DEFAULT_URL, b"{}", budget=Deadline(40.0, clock))
    assert [timeout for *_rest, timeout in server.opened] == [40.0]


def test_the_prompt_never_carries_the_hard_gates() -> None:
    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["body"] = payload
        return reply(fit_reply())

    config = {
        **RUBRIC,
        "hard_gates": {
            "seniority_words": ["Senior"],
            "seniority_in_description": True,
            "required_languages": ["Examplish"],
            "credentials": ["Example Licence"],
        },
    }
    assert score_posting(posting(), config, transport=transport) is not None
    prompt = json.loads(json.loads(seen["body"])["messages"][1]["content"])
    assert "hard_gates" not in prompt
    assert "Examplish" not in seen["body"].decode()
    assert "Example Licence" not in seen["body"].decode()


# --- an avoided language the posting never states (ADR-027) --------------------------------

LANGUAGES = {**RUBRIC, "hard_gates": {"required_languages": ["Examplish", "français"]}}
PHANTOM = {
    "requirements": [
        requirement("Examplish language proficiency", type="language", core=True),
        requirement("Forklift licence"),
    ],
    "key_requirements": ["Examplish language required", "Forklift licence", "Remote in Canada"],
    "entry_level": "",
}


def _languages(description: str) -> Score:
    result = score_posting(
        PostingText(
            "Example Robotics",
            "Operations Coordinator",
            "Example City",
            work_model="remote",
            description=description,
        ),
        LANGUAGES,
        transport=lambda url, body: reply(json.dumps(PHANTOM)),
    )
    assert result is not None
    return result


def test_a_gap_and_a_requirement_naming_an_unstated_avoided_language_are_dropped() -> None:
    result = _languages("A synthetic posting about scheduling.")
    assert json.loads(result.gaps) == [
        {"name": "Forklift licence", "kind": "minor", "type": "other"}
    ]
    assert result.tier == "high"  # one minor gap, not one core and one minor
    assert result.key_requirements == ("Remote in Canada",)  # the licence names a gap


def test_a_language_the_posting_states_is_kept() -> None:
    result = _languages("Fluency in Examplish is required for this synthetic role.")
    assert [gap["name"] for gap in json.loads(result.gaps)] == [
        "Examplish language proficiency",
        "Forklift licence",
    ]
    assert result.tier == "medium"
    assert "Examplish language required" in result.key_requirements


def test_the_guard_folds_accents_and_case_like_the_filter() -> None:
    phantom = json.loads(fit_reply(("FRANÇAIS courant", "core")))
    result = score_posting(
        posting(), LANGUAGES, transport=lambda url, body: reply(json.dumps(phantom))
    )
    assert result is not None and result.gaps == "[]"
    stated = PostingText("Example Robotics", "Coordinator", "Example City", description="Français")
    kept = score_posting(stated, LANGUAGES, transport=lambda url, body: reply(json.dumps(phantom)))
    assert kept is not None and json.loads(kept.gaps)[0]["name"] == "FRANÇAIS courant"


def test_a_more_than_three_list_still_gives_three_after_the_guard() -> None:
    reply_body = json.loads(
        fit_reply(key=("Examplish fluency", "Forklift licence", "Weekend shifts", "Class 5"))
    )
    result = score_posting(
        posting(), LANGUAGES, transport=lambda url, body: reply(json.dumps(reply_body))
    )
    assert result is not None
    assert result.key_requirements == ("Forklift licence", "Weekend shifts", "Class 5")


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
        "Sure! " + fit_reply(("French", "core", "language")) + " Hope that helps."
    )
    assert result is not None and result.tier == "medium"
    view = json.loads(json.loads(sent[0])["messages"][1]["content"])
    assert view["resume"] == _RESUME_TEXT
    assert result.llm_output == '{"gaps":[{"kind":"core","name":"French","type":"language"}]}'
    assert "Hope that helps" not in result.llm_output
    assert result.resume_sha256 == _RESUME.sha256


def test_without_a_resume_the_request_has_none_and_the_reply_is_kept() -> None:
    sent: list[bytes] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        sent.append(payload)
        return reply(fit_reply() + " trailing words")

    result = score_posting(posting(), RUBRIC, transport=transport)
    assert result is not None and result.resume_sha256 is None
    assert "resume" not in json.loads(json.loads(sent[0])["messages"][1]["content"])
    assert result.llm_output == fit_reply() + " trailing words"


def test_canonical_gaps_keep_unicode_and_sort_keys() -> None:
    result, _sent = _with_resume(fit_reply(("Français", "minor", "language")))
    assert result is not None
    assert result.llm_output == '{"gaps":[{"kind":"minor","name":"Français","type":"language"}]}'


def test_a_gap_text_over_120_characters_is_unparseable() -> None:
    result, _sent = _with_resume(fit_reply(("x" * 121, "minor")))
    assert result is None
    assert _with_resume(fit_reply(("y" * 120, "minor")))[0] is not None


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
def test_forty_characters_copied_from_the_resume_is_unparseable(copied: str) -> None:
    result, _sent = _with_resume(fit_reply((copied, "minor")))
    assert result is None


def test_thirty_nine_copied_characters_pass() -> None:
    result, _sent = _with_resume(fit_reply((_RUN_40[:39], "minor")))
    assert result is not None


def test_the_copy_check_only_applies_with_a_resume() -> None:
    result = score_posting(
        posting(), RUBRIC, transport=lambda url, body: reply(fit_reply((_RUN_40, "minor")))
    )
    assert result is not None


def test_normalization_is_nfkc_casefold_and_single_spaces() -> None:
    assert normalized("  \uff21very\u00a0\t EXAMPLE\nLtd  ") == "avery example ltd"


# --- key requirements (#39) ----------------------------------------------------------------


def requirements(content: object, resume: Resume | None = None) -> tuple[str, ...]:
    text = content if isinstance(content, str) else json.dumps(content)
    result = score_posting(
        posting(), RUBRIC, transport=lambda url, body: reply(text), resume=resume
    )
    assert result is not None  # requirements never make a reply unparseable
    return result.key_requirements


def test_the_prompt_asks_for_requirements_apart_from_the_gaps() -> None:
    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["body"] = payload
        return reply(fit_reply())

    score_posting(posting(), RUBRIC, transport=transport)
    system = json.loads(seen["body"])["messages"][0]["content"]
    assert "key_requirements: up to three" in system


def test_up_to_three_requirements_are_kept_cleaned_and_deduplicated() -> None:
    found = requirements(
        {
            "requirements": [],
            "key_requirements": [
                "  Forklift\nlicence ",
                "forklift   LICENCE",  # the same phrase again
                "Weekend\x07 availability",
                42,
                "",
                "Valid driver's licence",
                "A fourth one",
            ],
        }
    )
    assert found == ("Forklift licence", "Weekend availability", "Valid driver's licence")


def test_a_long_requirement_is_cut_at_a_word() -> None:
    long = "Experience coordinating inbound and outbound freight across several sites"
    (found,) = requirements({"requirements": [], "key_requirements": [long]})
    assert len(found) <= scoring.MAX_REQUIREMENT and found.endswith("…")
    assert found == "Experience coordinating inbound and outbound freight…"


@pytest.mark.parametrize("value", [None, "Forklift licence", {"a": 1}, [None], []], ids=repr)
def test_a_missing_or_malformed_list_gives_none_and_keeps_the_score(value: object) -> None:
    payload: dict[str, object] = {"requirements": []}
    if value is not None:
        payload["key_requirements"] = value
    assert requirements(payload) == ()


def test_a_requirement_that_names_a_gap_is_dropped() -> None:
    found = requirements(
        {
            "requirements": [requirement("Class 1 licence")],
            "key_requirements": ["A valid CLASS 1 licence", "Bilingual service", "Class 1"],
        }
    )
    # "Class 1" alone does not contain the gap name, so it is posting text and stays.
    assert found == ("Bilingual service", "Class 1")


def test_a_requirement_that_copies_the_resume_is_dropped() -> None:
    resume = Resume(
        text="Avery Example. Coordinated forty warehouse staff across two night shifts.",
        sha256="0" * 64,
    )
    found = requirements(
        {
            "requirements": [],
            "key_requirements": [
                "Coordinated forty warehouse staff across two night shifts",
                "Forklift licence",
            ],
        },
        resume=resume,
    )
    assert found == ("Forklift licence",)


# --- stated items, classed; gaps derived in code (Batch Q) -----------------------------------

TRAINING = PostingText(
    "Example Automate",
    "Junior Automation Developer",
    "Example City",
    work_model="remote",
    description=(
        "You will build workflows for clients. We will train you on our tools. "
        "Don't meet every requirement? Apply anyway. Strong attention to detail."
    ),
)


def _scored(content: str, *, resume: Resume | None = None, text: PostingText = TRAINING) -> Score:
    result = score_posting(text, RUBRIC, transport=lambda url, body: reply(content), resume=resume)
    assert result is not None
    return result


def test_duties_and_trained_items_are_never_gaps() -> None:
    """A junior posting's duty list and what it will teach don't count against it."""
    content = fit_reply(
        items=(
            requirement("Build workflow automations", "duty", core=True),
            requirement("Develop dashboards for clients", "duty"),
            requirement("Our in-house tools", "trained", type="tool", core=True),
            requirement("Attention to detail", "soft", met="partly", evidence=""),
        )
    )
    result = _scored(content)
    assert json.loads(result.gaps) == [] and result.tier == "high"


def test_a_soft_item_counts_only_when_not_met_at_all_and_then_as_minor() -> None:
    result = _scored(fit_reply(items=(requirement("Teamwork", "soft", core=True),)))
    assert json.loads(result.gaps) == [{"name": "Teamwork", "kind": "minor", "type": "other"}]


def test_required_and_preferred_unmet_items_are_gaps_and_partly_is_minor() -> None:
    content = fit_reply(
        items=(
            requirement("SQL", type="skill", core=True),
            requirement("Azure", "preferred", type="tool"),
            requirement("Python", type="skill", core=True, met="partly", evidence="Python"),
            requirement("Excel", type="tool", met="yes", evidence="Excel"),
        )
    )
    result = _scored(content)
    assert json.loads(result.gaps) == [
        {"name": "SQL", "kind": "core", "type": "skill"},
        {"name": "Azure", "kind": "minor", "type": "tool"},
        {"name": "Python", "kind": "minor", "type": "skill"},
    ]


def test_evidence_not_in_the_resume_turns_yes_into_no() -> None:
    resume = Resume("Sam Example. Built weekly reports in Excel for a depot.", "cd" * 32)
    content = fit_reply(
        items=(
            requirement("Excel reporting", type="tool", met="yes", evidence="reports in Excel"),
            requirement(
                "Power BI", type="tool", core=True, met="yes", evidence="Power BI dashboards"
            ),
            requirement("SQL", type="skill", met="partly", evidence="SQL"),  # too short
        )
    )
    result = _scored(content, resume=resume)
    assert json.loads(result.gaps) == [
        {"name": "Power BI", "kind": "core", "type": "tool"},
        {"name": "SQL", "kind": "minor", "type": "skill"},
    ]
    assert "Power BI dashboards" not in result.llm_output  # evidence is never stored


def test_gap_names_are_one_style_and_types_a_fixed_list() -> None:
    assert scoring.gap_name("investment_wealth_management_experience") == (
        "Investment wealth management experience"
    )
    assert scoring.gap_name("toolExperience") == "Tool experience"
    assert scoring.gap_name("  SQL   basics ") == "SQL basics"
    assert scoring.gap_name("power BI") == "Power BI"
    content = fit_reply(
        items=(
            requirement("licensing_certification_requirements", type="technical_skills"),
            requirement("Licensing certification requirements"),  # the same gap again
        )
    )
    assert json.loads(_scored(content).gaps) == [
        {"name": "Licensing certification requirements", "kind": "minor", "type": "other"}
    ]


def test_entry_level_counts_only_with_a_quote_found_in_the_posting() -> None:
    assert _scored(fit_reply(entry_level="Apply anyway.")).entry_level is True
    assert _scored(fit_reply(entry_level="we'll train")).entry_level is False  # not in it
    assert _scored(fit_reply(entry_level="Junior")).entry_level is False  # too short
    assert _scored(fit_reply(entry_level="Junior Automation Developer")).entry_level is True
    assert _scored(json.dumps({"requirements": [], "entry_level": 5})).entry_level is False


def test_the_prompt_asks_for_every_stated_item_and_the_training_offer() -> None:
    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["body"] = payload
        return reply(fit_reply())

    score_posting(posting(), RUBRIC, transport=transport)
    system = json.loads(seen["body"])["messages"][0]["content"]
    for phrase in ("every item the posting states", '"duty"', '"trained"', "copied exactly"):
        assert phrase in system
    assert "gap_tiers" not in system and "max_gaps" not in system


# --- the tier, in code; no threshold is sent (Batch Q) ---------------------------------------

DEFAULTS = {"fit_rubric": {"requirement_tiers": {"exempt_types": ["education"]}}}


def _tier(content: str, text: PostingText = TRAINING) -> Score:
    result = score_posting(text, DEFAULTS, transport=lambda url, body: reply(content))
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        ((), "high"),
        ((("a", "minor"), ("b", "minor")), "high"),
        ((("a", "minor"), ("b", "minor"), ("c", "minor")), "medium"),
        (tuple((f"m{n}", "minor") for n in range(12)), "medium"),  # minor only: never low
        ((("a", "core"),), "medium"),
        ((("a", "core"), ("b", "minor"), ("c", "minor"), ("d", "minor")), "medium"),
        ((("a", "core"), ("b", "core")), "low"),
    ],
    ids=["none", "two-minor", "three-minor", "many-minor", "one-core", "core-and-minor", "2-core"],
)
def test_the_tier_follows_core_and_minor_gaps(
    gaps: tuple[tuple[str, str], ...], expected: str
) -> None:
    result = _tier(fit_reply(*gaps))
    assert result.tier == expected and result.excluded is False


def test_a_core_gap_in_the_main_function_is_low() -> None:
    main = requirement("2-3 years as an analyst in this function", core=True, main_function=True)
    assert _tier(fit_reply(items=(main,))).tier == "low"
    # The same item, met: no gap at all.
    met = {**main, "met": "yes"}
    assert _tier(fit_reply(items=(met,))).tier == "high"


def test_three_core_gaps_exclude_the_posting() -> None:
    result = _tier(fit_reply(("a", "core"), ("b", "core"), ("c", "core")))
    assert result.excluded and result.tier is None


def test_an_entry_level_posting_may_have_more_minor_gaps_at_high() -> None:
    four = tuple((f"m{n}", "minor") for n in range(4))
    assert _tier(fit_reply(*four)).tier == "medium"
    assert _tier(fit_reply(*four, entry_level="Apply anyway.")).tier == "high"
    five = tuple((f"m{n}", "minor") for n in range(5))
    assert _tier(fit_reply(*five, entry_level="Apply anyway.")).tier == "medium"


def test_an_exempt_type_never_counts() -> None:
    degree = requirement("Bachelor's degree", type="education", core=True)
    result = _tier(fit_reply(items=(degree,)))
    assert result.tier == "high" and json.loads(result.gaps) == []


def test_no_threshold_or_tier_reaches_the_model() -> None:
    seen: dict[str, bytes] = {}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen["body"] = payload
        return reply(fit_reply())

    tuned = {
        "fit_rubric": {
            "requirement_tiers": {"high_max_minor": 7, "exclude_at_core": 9, "exempt_types": []}
        }
    }
    score_posting(posting(), tuned, transport=transport)
    text = seen["body"].decode()
    for word in ("requirement_tiers", "high_max_minor", "exclude_at_core", "gap_tiers", "max_gaps"):
        assert word not in text
    view = json.loads(json.loads(seen["body"])["messages"][1]["content"])
    assert set(view) == {"lanes", "location_rules", "posting"}


# --- experience only from roles and skills (Batch Q) ----------------------------------------

SUMMARY_RESUME = Resume(
    "Jordan Sample\n"
    "Operations specialist with eight years across logistics, retail and finance.\n"
    "\n"
    "Work Experience\n"
    "Coordinator, Example Freight (2019-2024): ran dispatch schedules and SharePoint "
    "lists for three depots.\n"
    "Skills: Excel, SQL, Power Automate\n",
    "ef" * 32,
)


def _resumed(*items: dict[str, object]) -> Score:
    content = fit_reply(items=items)
    return _scored(content, resume=SUMMARY_RESUME, text=posting())


def test_the_summary_and_the_roles_are_told_apart() -> None:
    summary, body = scoring.resume_parts(SUMMARY_RESUME.text)
    assert "logistics, retail and finance" in summary and "Work Experience" not in summary
    assert body.startswith("Work Experience")
    assert scoring.resume_parts("No headings at all. Finance and tech.") == (
        "",
        "No headings at all. Finance and tech.",
    )
    for heading in ("EXPERIENCE", "Professional Experience:", "Employment history", "Work History"):
        assert scoring.resume_parts(f"Summary line\n{heading}\nA role")[0] == "Summary line\n"


def test_an_industry_word_in_the_summary_is_not_experience() -> None:
    fpa = requirement(
        "FP&A experience",
        type="experience",
        core=True,
        main_function=True,
        met="yes",
        evidence="finance",
    )
    result = _resumed(fpa)
    assert json.loads(result.gaps) == [
        {"name": "FP&A experience", "kind": "core", "type": "experience"}
    ]
    assert result.tier == "low"


def test_experience_a_role_shows_still_counts() -> None:
    dispatch = requirement(
        "Dispatch scheduling experience",
        type="experience",
        core=True,
        met="yes",
        evidence="ran dispatch schedules",
    )
    assert json.loads(_resumed(dispatch).gaps) == []


def test_evidence_matches_whole_words_so_short_skills_still_count() -> None:
    sql = requirement("SQL", type="skill", core=True, met="yes", evidence="SQL")
    excel = requirement("Excel", type="tool", met="yes", evidence="Excel")
    part = requirement("Excelsior suite", type="tool", met="yes", evidence="Excels")
    assert json.loads(_resumed(sql, excel).gaps) == []
    assert json.loads(_resumed(part).gaps) == [
        {"name": "Excelsior suite", "kind": "minor", "type": "tool"}
    ]


def test_a_listed_tool_the_model_missed_is_a_minor_gap_never_core() -> None:
    sharepoint = requirement("SharePoint development", type="tool", core=True)
    automate = requirement("Advanced Power Automate flows", type="skill", core=True)
    result = _resumed(sharepoint, automate)
    assert json.loads(result.gaps) == [
        {"name": "SharePoint development", "kind": "minor", "type": "tool"},
        {"name": "Advanced Power Automate flows", "kind": "minor", "type": "skill"},
    ]


def test_the_term_guard_reads_only_tools_and_skills_outside_the_summary() -> None:
    # "Retail" is in the summary only; an experience item is never softened by a term.
    retail = requirement("Retail tooling", type="tool", core=True)
    finance = requirement("Corporate Finance experience", type="experience", core=True)
    advanced = requirement("Advanced proficiency", type="skill", core=True)
    result = _resumed(retail, finance, advanced)
    assert [gap["kind"] for gap in json.loads(result.gaps)] == ["core", "core", "core"]


def test_the_prompt_states_the_general_experience_rule() -> None:
    assert "Experience counts only where the resume's roles" in scoring.SYSTEM_PROMPT
    assert "is not experience in that field's job functions" in scoring.SYSTEM_PROMPT
    assert "profile" not in scoring.SYSTEM_PROMPT


# --- the seniority line: 4 years (operator, Batch Q) ------------------------------------------


def _titled(title: str, *items: dict[str, object]) -> Score:
    text = PostingText("Example Robotics", title, "Example City", "remote", description="x")
    result = score_posting(
        text, DEFAULTS, transport=lambda url, body: reply(fit_reply(items=items))
    )
    assert result is not None
    return result


def _years(count: int, *, core: bool = True, met: str = "no") -> dict[str, object]:
    return requirement(
        f"{count}+ years in the role's own function",
        type="experience",
        core=core,
        main_function=True,
        years=count,
        met=met,
    )


def test_four_or_more_years_is_a_core_seniority_gap_and_rules_out_high() -> None:
    for count in (4, 5):
        result = _titled("Technical Account Manager", _years(count, core=False))
        assert json.loads(result.gaps) == [
            {
                "name": f"{count}+ years in the role's own function",
                "kind": "core",
                "type": "seniority",
            }
        ]
        assert result.tier == "low"


def test_up_to_three_years_is_minor_whatever_the_reply_says() -> None:
    for count in (1, 2, 3):
        result = _titled("Security Analyst", _years(count))
        assert json.loads(result.gaps) == [
            {
                "name": f"{count}+ years in the role's own function",
                "kind": "minor",
                "type": "experience",
            }
        ]
        assert result.tier == "high"


@pytest.mark.parametrize(
    "title",
    [
        "Junior Security Analyst",
        "Entry-Level Operations Support Analyst",
        "Associate, Customer Success",
        "Software Developer (New Graduate)",
        "IT Support Intern",
        "SOC Analyst Tier 1",
        "Analyst I",
        "Threat Analyst 1, Managed Response",
    ],
)
def test_a_junior_title_drops_a_three_year_ask_entirely(title: str) -> None:
    result = _titled(title, _years(3))
    assert json.loads(result.gaps) == [] and result.tier == "high"
    four = _titled(title, _years(4))
    assert four.tier == "low"  # four years stays core even with a junior title


def test_a_senior_or_lead_title_is_a_core_seniority_gap() -> None:
    for title in ("Senior Security Analyst", "Team Lead, Support", "Sr. Developer"):
        result = _titled(title)
        assert json.loads(result.gaps) == [
            {"name": "Senior or lead title", "kind": "core", "type": "seniority"}
        ]
        assert result.tier == "low"
    assert _titled("Leadership Coordinator").tier == "high"  # whole words only


def test_years_partly_met_are_minor_and_years_met_are_no_gap() -> None:
    assert _titled("Analyst", _years(5, met="partly")).tier == "high"
    assert json.loads(_titled("Analyst", _years(5, met="yes")).gaps) == []


def test_only_minor_gaps_on_a_years_heavy_posting_never_make_it_low() -> None:
    items = tuple(
        requirement(f"{n} years of tool {n}", type="experience", years=2) for n in range(6)
    )
    assert _titled("Systems Analyst", *items).tier == "medium"
