"""Secrets live in the macOS Keychain, reached through the `keyring` library.

Never in files, env files, or the repo. There is deliberately no fallback: if the
Keychain backend isn't available, setting or reading a secret fails.
"""

from __future__ import annotations

import keyring
from keyring.backend import KeyringBackend

SERVICE = "career-hunter"
ALLOWED_NAMES = frozenset({"telegram_bot_token"})

# Backends that would store secrets in plaintext or silently drop them.
_INSECURE_BACKEND_PREFIXES = ("keyrings.alt", "keyring.backends.fail", "keyring.backends.null")


class SecretError(Exception):
    """User-facing error from the secrets helper. Never contains a secret value."""


def _backend() -> KeyringBackend:
    backend: KeyringBackend = keyring.get_keyring()
    qualname = f"{type(backend).__module__}.{type(backend).__name__}"
    if qualname.startswith(_INSECURE_BACKEND_PREFIXES):
        raise SecretError(
            f"Refusing to use keyring backend {qualname}: it is not the macOS Keychain."
        )
    return backend


def _check_name(name: str) -> None:
    if name not in ALLOWED_NAMES:
        allowed = ", ".join(sorted(ALLOWED_NAMES))
        raise SecretError(f"Unknown secret name {name!r}. Allowed: {allowed}")


def set_secret(name: str, value: str) -> None:
    _check_name(name)
    value = value.strip()
    if not value:
        raise SecretError("Refusing to store an empty secret.")
    _backend().set_password(SERVICE, name, value)


def get_secret(name: str) -> str | None:
    """For use by later phases at runtime. Never print the return value."""
    _check_name(name)
    value: str | None = _backend().get_password(SERVICE, name)
    return value


def describe_secret(name: str) -> str:
    """A safe, non-revealing description: 'present (N chars)' or 'missing'."""
    value = get_secret(name)
    return "missing" if not value else f"present ({len(value)} chars)"
