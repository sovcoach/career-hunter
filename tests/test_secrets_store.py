from __future__ import annotations

import keyring
import pytest

from careerhunter import cli, secrets_store
from careerhunter.secrets_store import SERVICE, SecretError
from tests.conftest import FakeProbe, InMemoryKeyring

FAKE_TOKEN = "123456:SYNTHETIC-test-token-not-real"


def test_set_and_get_round_trip(memory_keyring: InMemoryKeyring) -> None:
    secrets_store.set_secret("telegram_bot_token", f"  {FAKE_TOKEN}\n")
    assert memory_keyring.store[(SERVICE, "telegram_bot_token")] == FAKE_TOKEN
    assert secrets_store.get_secret("telegram_bot_token") == FAKE_TOKEN


def test_describe_never_reveals_value(memory_keyring: InMemoryKeyring) -> None:
    assert secrets_store.describe_secret("telegram_bot_token") == "missing"
    secrets_store.set_secret("telegram_bot_token", FAKE_TOKEN)
    described = secrets_store.describe_secret("telegram_bot_token")
    assert described == f"present ({len(FAKE_TOKEN)} chars)"
    assert "SYNTHETIC" not in described


def test_unknown_name_and_empty_value_refused() -> None:
    with pytest.raises(SecretError, match="Unknown secret name"):
        secrets_store.set_secret("aws_key", "x")
    with pytest.raises(SecretError, match="empty"):
        secrets_store.set_secret("telegram_bot_token", "   ")


def test_plaintext_backend_refused() -> None:
    class PlaintextKeyring(InMemoryKeyring):
        pass

    PlaintextKeyring.__module__ = "keyrings.alt.file"
    keyring.set_keyring(PlaintextKeyring())
    with pytest.raises(SecretError, match="not the macOS Keychain"):
        secrets_store.set_secret("telegram_bot_token", FAKE_TOKEN)


def _run_cli(argv: list[str], probe: FakeProbe) -> int:
    return cli.main(argv, probe=probe)


def test_cli_set_and_verify_never_echo(
    data_env: object,
    probe: FakeProbe,
    memory_keyring: InMemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["init"], probe) == 0
    answers = iter([FAKE_TOKEN, FAKE_TOKEN])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert _run_cli(["secret", "set", "telegram_bot_token"], probe) == 0
    assert _run_cli(["secret", "verify", "telegram_bot_token"], probe) == 0
    out = capsys.readouterr()
    assert FAKE_TOKEN not in out.out + out.err
    assert "present" in out.out
    assert memory_keyring.store[(SERVICE, "telegram_bot_token")] == FAKE_TOKEN


def test_cli_set_mismatch_stores_nothing(
    data_env: object,
    probe: FakeProbe,
    memory_keyring: InMemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run_cli(["init"], probe) == 0
    answers = iter([FAKE_TOKEN, "something-else"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert _run_cli(["secret", "set", "telegram_bot_token"], probe) == 1
    assert memory_keyring.store == {}
