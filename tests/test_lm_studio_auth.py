"""LM Studio's API token: a Bearer header on chat and embedding calls, never printed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, embed, loopback, score, secrets_store
from careerhunter.platform_checks import SystemProbe
from careerhunter.score import Score
from tests.fakes import FakeServer, Reply, use_example_watchlist

TOKEN = "SYNTHETIC-lm-studio-token-not-real"


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
