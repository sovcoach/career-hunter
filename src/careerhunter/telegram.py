"""A minimal Telegram Bot API client: long polling over HTTPS, standard library only.

Only the handful of methods the bot needs, called as JSON over `urllib` (ADR-016). TLS
certificates are checked against the macOS trust store through `truststore`.

The bot token is part of every API URL (`/bot<token>/<method>`), and `urllib` errors can
include the URL. So every failure is re-raised as a TelegramError whose message has been
passed through `redact()`, with the original exception suppressed, and the token can
never reach a log or an error message.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Protocol

import truststore

API = "https://api.telegram.org"
# Telegram tokens look like 123456789:AAH...; matched loosely so variants are caught too.
TOKEN_SHAPE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")


class TelegramError(Exception):
    """A failed Bot API call. The message is safe to log: it never contains the token."""


def redact(text: str, token: str | None = None) -> str:
    if token:
        text = text.replace(token, "<token>")
    return TOKEN_SHAPE.sub("<token>", text)


class TelegramAPI(Protocol):
    def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...

    def download(self, file_path: str, max_bytes: int) -> bytes: ...


class HttpTelegram:
    """The real client. Tests use a fake with the same two methods."""

    def __init__(self, token: str, *, timeout: float = 65) -> None:
        self._token = token
        self._timeout = timeout
        self._context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def _open(self, request: urllib.request.Request, what: str) -> bytes:
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed https:// API host
                request, timeout=self._timeout, context=self._context
            ) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise TelegramError(redact(f"{what}: HTTP {exc.code} {body}", self._token)) from None
        except (OSError, ValueError) as exc:
            raise TelegramError(
                redact(f"{what}: {type(exc).__name__}: {exc}", self._token)
            ) from None

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(  # noqa: S310
            f"{API}/bot{self._token}/{method}",
            data=json.dumps(params or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        raw = self._open(request, method)
        try:
            reply = json.loads(raw)
        except ValueError:
            raise TelegramError(f"{method}: the reply was not JSON") from None
        if not isinstance(reply, dict) or not reply.get("ok"):
            description = reply.get("description") if isinstance(reply, dict) else None
            raise TelegramError(redact(f"{method}: {description or 'not ok'}", self._token))
        return reply.get("result")

    def download(self, file_path: str, max_bytes: int) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", file_path) or ".." in file_path:
            raise TelegramError("download: Telegram returned an unexpected file path")
        request = urllib.request.Request(f"{API}/file/bot{self._token}/{file_path}")  # noqa: S310
        data = self._open(request, "download")
        if len(data) > max_bytes:
            raise TelegramError(f"download: the file is larger than {max_bytes} bytes")
        return data
