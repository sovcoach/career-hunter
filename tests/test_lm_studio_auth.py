"""LM Studio's API token: a Bearer header on chat and embedding calls, never printed."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, db, embed, loopback, score, search_store, secrets_store, watch
from careerhunter.filters import PostingText
from careerhunter.platform_checks import SystemProbe
from careerhunter.score import Score
from tests.fakes import FakeServer, Reply, use_example_watchlist
from tests.test_score import RUBRIC, posting
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

TOKEN = "SYNTHETIC-lm-studio-token-not-real"
STAMP = "2026-11-01T12:00:00Z"


def _headers(server: FakeServer) -> dict[str, str]:
    assert len(server.requests) == 1
    return server.requests[0][3]


def test_the_token_is_a_bearer_header_on_a_chat_call(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(Reply(200, b"{}"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    score.default_transport(score.DEFAULT_URL, b"{}", token=TOKEN)
    assert _headers(server)["Authorization"] == f"Bearer {TOKEN}"


def test_the_token_is_a_bearer_header_on_an_embedding_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(Reply(200, b"{}"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    embed.default_transport(embed.DEFAULT_URL, b"{}", token=TOKEN)
    assert _headers(server)["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("token", [None, ""])
def test_no_token_sends_no_authorization_header(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    server = FakeServer(Reply(200, b"{}"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    score.default_transport(score.DEFAULT_URL, b"{}", token=token)
    assert "Authorization" not in _headers(server)


def test_a_refused_call_never_carries_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(Reply(error=ConnectionRefusedError("synthetic"), error_at="request"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    with pytest.raises(score.ScoreError) as caught:
        score.default_transport(score.DEFAULT_URL, b"{}", token=TOKEN)
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
    with pytest.raises(embed.EmbedError) as embed_caught:
        embed.default_transport(embed.DEFAULT_URL, b"{}", token=TOKEN)
    assert TOKEN not in str(embed_caught.value)


def test_lm_studio_token_is_an_allowed_secret() -> None:
    assert "lm_studio_token" in secrets_store.ALLOWED_NAMES
    secrets_store.set_secret("lm_studio_token", TOKEN)
    assert secrets_store.describe_secret("lm_studio_token") == f"present ({len(TOKEN)} chars)"


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    use_example_watchlist(data_env / "config.yaml")
    capsys.readouterr()
    return data_env


def test_watch_sends_the_stored_token_and_never_prints_it(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_store.set_secret("lm_studio_token", TOKEN)
    seen: dict[str, Any] = {}

    def run(*_args: object, **kwargs: Any) -> list[str]:
        seen["embed"] = kwargs["embed_transport"].keywords.get("token")
        kwargs["scorer"](object())
        return []

    def local_score(posting: object, config: object, **kwargs: Any) -> Score:
        seen["score"] = kwargs["transport"].keywords.get("token")
        return Score(llm_output="{}", gaps="[]", tier="strong")

    monkeypatch.setattr(cli.watch, "run", run)
    monkeypatch.setattr(cli, "score_posting", local_score)
    assert cli.main(["watch"], probe=probe) == cli.EXIT_OK
    assert seen == {"embed": TOKEN, "score": TOKEN}
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert TOKEN not in (inited / "logs" / "watch.log").read_text()


def test_watch_without_a_readable_keychain_goes_without_the_token(
    inited: Path, probe: SystemProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(name: str) -> str | None:
        raise secrets_store.SecretError("synthetic: no Keychain")

    seen: dict[str, Any] = {}

    def run(*_args: object, **kwargs: Any) -> list[str]:
        seen["embed"] = kwargs["embed_transport"].keywords.get("token")
        return []

    monkeypatch.setattr(cli.secrets_store, "get_secret", broken)
    monkeypatch.setattr(cli.watch, "run", run)
    assert cli.main(["watch"], probe=probe) == cli.EXIT_OK
    assert seen == {"embed": None}
    assert "lm_studio_token could not be read" in (inited / "logs" / "watch.log").read_text()


# --- a refused token is "unauthorized", never "unreachable" -------------------------------


def _answering(status: int) -> Any:
    def transport(address: str, payload: bytes) -> tuple[int, bytes]:
        return status, b'{"error":"synthetic"}'

    return transport


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_an_unauthorized_score_error(status: int) -> None:
    with pytest.raises(score.ScoreError) as caught:
        score.score_posting(posting(), RUBRIC, transport=_answering(status))
    assert caught.value.kind == "unauthorized"
    assert "unreachable" not in str(caught.value)


def test_another_status_stays_unreachable() -> None:
    with pytest.raises(score.ScoreError) as caught:
        score.score_posting(posting(), RUBRIC, transport=_answering(500))
    assert caught.value.kind == "unreachable"


def test_a_refused_token_is_an_unauthorized_embedding_error() -> None:
    with pytest.raises(embed.EmbedError) as caught:
        embed.embed_text(
            "synthetic text", model="example-embedding-model", transport=_answering(401)
        )
    assert caught.value.reason == "unauthorized" and caught.value.down
    assert embed.miss_line(7, "unauthorized") == "embedding miss posting 7: unauthorized"


def test_watch_names_an_unauthorized_model_and_records_it(search: sqlite3.Connection) -> None:
    first = url("greenhouse", "examplerobotics")
    second = url("greenhouse", "samplelogistics")

    def scorer(text: PostingText) -> Score | None:
        return score.score_posting(text, RUBRIC, transport=_answering(401))

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
    text = "\n".join(lines)
    unauthorized = "model unauthorized: LM Studio refused the API token"
    assert f"left unscored ({unauthorized})" in text
    assert f"left for the next run ({unauthorized})" in text
    assert "unreachable" not in text
    (health,) = [h for h in search_store.component_health(search) if h.component == "lm_studio"]
    assert (health.failures, health.last_failure) == (1, "unauthorized")


def test_migration_0022_keeps_every_health_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    try:
        before = [m for m in db.load_migrations("search") if m.version <= 21]
        db.migrate(conn, "search", before)
        with db.transaction(conn):  # as 0021's code wrote it
            conn.execute(
                "UPDATE component_health SET failures = 1, last_failure = 'throttled', "
                "held = 0, updated_at = ? WHERE component = 'searxng'",
                (STAMP,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE component_health SET failures = 1, last_failure = 'unauthorized' "
                "WHERE component = 'lm_studio'"
            )
        db.migrate(conn, "search")
        rows = {
            str(r["component"]): (r["failures"], r["last_failure"], r["updated_at"])
            for r in conn.execute("SELECT * FROM component_health")
        }
        assert rows == {
            "lm_studio": (0, None, None),
            "searxng": (1, "throttled", STAMP),
            "boards": (0, None, None),
            "criteria": (0, None, None),  # added by 0027
        }
        with db.transaction(conn):
            search_store.record_component(conn, "lm_studio", failed="unauthorized", stamp=STAMP)
        assert (
            conn.execute(
                "SELECT last_failure FROM component_health WHERE component = 'lm_studio'"
            ).fetchone()[0]
            == "unauthorized"
        )
    finally:
        conn.close()


# --- doctor asks LM Studio for its model list, with the token -----------------------------

REAL_STATUS = cli.lm_studio_status  # conftest replaces it with a healthy fake per test


def _server(monkeypatch: pytest.MonkeyPatch, reply: Reply) -> FakeServer:
    server = FakeServer(reply)
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    return server


def test_doctor_status_is_ok_when_the_token_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_store.set_secret("lm_studio_token", TOKEN)
    server = _server(monkeypatch, Reply(200, b'{"data":[]}'))
    assert REAL_STATUS() == ("OK", "answers with the API token")
    ((method, path, body, headers),) = server.requests
    assert (method, path, body) == ("GET", "/v1/models", None)
    assert headers["Authorization"] == f"Bearer {TOKEN}" and "Content-Type" not in headers


@pytest.mark.parametrize("status", [401, 403])
def test_doctor_status_names_a_refused_token_unauthorized(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    secrets_store.set_secret("lm_studio_token", TOKEN)
    _server(monkeypatch, Reply(status, b"{}"))
    level, detail = REAL_STATUS()
    assert level == "BAD"
    assert detail.startswith("unauthorized: LM Studio refused the API token")
    assert "careerhunter secret set lm_studio_token" in detail
    assert "unreachable" not in detail and TOKEN not in detail


def test_doctor_status_names_a_server_that_does_not_answer_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_store.set_secret("lm_studio_token", TOKEN)
    _server(monkeypatch, Reply(error=ConnectionRefusedError("synthetic"), error_at="request"))
    assert REAL_STATUS() == ("BAD", "unreachable at 127.0.0.1:1234; start LM Studio's server")


def test_doctor_status_names_another_status(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_store.set_secret("lm_studio_token", TOKEN)
    _server(monkeypatch, Reply(500, b"{}"))
    assert REAL_STATUS() == ("BAD", "answered HTTP 500 to a model list request")


def test_doctor_status_without_a_token_asks_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server(monkeypatch, Reply(200, b"{}"))
    assert REAL_STATUS() == (
        "BAD",
        "lm_studio_token is missing; run: careerhunter secret set lm_studio_token",
    )
    assert server.requests == []


def test_doctor_status_with_an_unreadable_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str) -> str | None:
        raise secrets_store.SecretError("synthetic: no Keychain")

    monkeypatch.setattr(cli.secrets_store, "get_secret", broken)
    assert REAL_STATUS() == ("BAD", "lm_studio_token could not be read from the Keychain")


def test_doctor_fails_on_a_refused_token_and_never_prints_it(
    inited: Path,
    probe: SystemProbe,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_store.set_secret("lm_studio_token", TOKEN)
    monkeypatch.setattr(cli, "lm_studio_status", REAL_STATUS)
    _server(monkeypatch, Reply(401, b"{}"))
    assert cli.main(["doctor"], probe=probe) == cli.EXIT_ERROR
    out = capsys.readouterr().out
    assert "LM Studio       BAD unauthorized: LM Studio refused the API token" in out
    assert TOKEN not in out


def test_doctor_skips_lm_studio_when_nothing_calls_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "lm_studio_status", REAL_STATUS)
    cli._doctor_lm_studio(cli._Report(), {"fit_rubric": {"weights": {}}})
    cli._doctor_lm_studio(cli._Report(), None)  # an invalid config.yaml: no line
    assert capsys.readouterr().out == (
        "LM Studio       --  not used (no fit_rubric.gap_tiers and no resume)\n"
    )
