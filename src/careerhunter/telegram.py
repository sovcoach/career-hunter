"""A minimal Telegram Bot API client: long polling over HTTPS, standard library only.

Only the handful of methods the bot needs, called as JSON over `urllib` (ADR-016). TLS
certificates are checked against the macOS trust store through `truststore`.

The bot token is part of every API URL (`/bot<token>/<method>`), and `urllib` errors can
include the URL. So every failure is re-raised as a TelegramError whose message has been
passed through `redact()`, with the original exception suppressed, and the token can
never reach a log or an error message.

Failures come in four kinds, all TelegramError, so a caller that only needs "it
failed" catches the base class:

- TelegramUnreachable: no answer from Telegram (a network error, a timeout, a TLS
  failure, or HTTP 5xx). A sent message may or may not have arrived.
- TelegramRateLimited: HTTP 429. Nothing was done; try again later.
- TelegramRejected: Telegram answered and refused the call (HTTP 4xx other than 429,
  or a reply with `ok` false). A message was not sent; sending it again unchanged
  will be refused again.
- TelegramBadReply: an answer that is not the Bot API's JSON. Whether a sent message
  arrived is unknown.
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


class TelegramUnreachable(TelegramError):
    """No answer: the network, a timeout, TLS, or HTTP 5xx."""


class TelegramRateLimited(TelegramError):
    """HTTP 429: Telegram asked us to slow down. Nothing was done."""


class TelegramRejected(TelegramError):
    """Telegram answered and refused the call: HTTP 4xx (not 429), or `ok` false."""


class TelegramBadReply(TelegramError):
    """An answer that is not the Bot API's JSON."""


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
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except OSError:
                body = ""
            message = redact(f"{what}: HTTP {exc.code} {body}", self._token)
            if exc.code == 429:
                raise TelegramRateLimited(message) from None
            if 400 <= exc.code < 500:
                raise TelegramRejected(message) from None
            raise TelegramUnreachable(message) from None
        except (OSError, ValueError) as exc:
            raise TelegramUnreachable(
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
            raise TelegramBadReply(f"{method}: the reply was not JSON") from None
        if not isinstance(reply, dict):
            raise TelegramBadReply(f"{method}: the reply was not a JSON object")
        if not reply.get("ok"):
            description = reply.get("description")
            message = redact(f"{method}: {description or 'not ok'}", self._token)
            if reply.get("error_code") == 429:
                raise TelegramRateLimited(message)
            raise TelegramRejected(message)
        return reply.get("result")

    def download(self, file_path: str, max_bytes: int) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", file_path) or ".." in file_path:
            raise TelegramError("download: Telegram returned an unexpected file path")
        request = urllib.request.Request(f"{API}/file/bot{self._token}/{file_path}")  # noqa: S310
        data = self._open(request, "download")
        if len(data) > max_bytes:
            raise TelegramError(f"download: the file is larger than {max_bytes} bytes")
        return data
