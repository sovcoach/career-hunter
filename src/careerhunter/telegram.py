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

Each failure also carries the method, the error code and Telegram's own description
(`TelegramError.detail()`), so a log can say which call was refused and why.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import truststore

API = "https://api.telegram.org"
# Telegram tokens look like 123456789:AAH...; matched loosely so variants are caught too.
TOKEN_SHAPE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
# Telegram's own description of a refusal, as logged: one line, no token, capped.
MAX_DESCRIPTION = 200
MAX_ERROR_BODY = 4096  # of an HTTP error's body, read to find Telegram's description
# How long one of the host's addresses gets to open a connection before the next is
# tried. Without it, an IPv6 address whose connection hangs held a call for the whole
# timeout (65 seconds) before IPv4 was tried, and a tap's answer arrived too late.
CONNECT_TIMEOUT = 5.0
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


class TelegramError(Exception):
    """A failed Bot API call. The message is safe to log: it never contains the token.

    `method` is the Bot API method, `error_code` Telegram's code or the HTTP status, and
    `description` Telegram's own reason (or, with no answer, the kind of network
    failure). Each is None when not known. The description is one line of at most
    MAX_DESCRIPTION characters with the token redacted. Telegram's descriptions name
    the problem, not the request, so no message text is in it.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        error_code: int | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.error_code = error_code
        self.description = description

    def detail(self) -> str:
        """`method code: description`, from the parts that are known; may be empty."""
        head = " ".join(str(part) for part in (self.method, self.error_code) if part is not None)
        if self.description:
            return f"{head}: {self.description}" if head else self.description
        return head


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


def clean_description(text: object, token: str | None = None) -> str | None:
    """Telegram's description made safe to log: text only, one line, redacted, capped."""
    if not isinstance(text, str):
        return None
    line = " ".join(_CONTROL.sub(" ", redact(text, token)).split())
    if len(line) > MAX_DESCRIPTION:
        line = line[: MAX_DESCRIPTION - 1] + "…"
    return line or None


def _code(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _reply_description(body: str) -> object:
    """The `description` of a Bot API reply body, when the body is that JSON."""
    try:
        reply = json.loads(body)
    except ValueError:
        return None
    return reply.get("description") if isinstance(reply, dict) else None


Address = tuple[int, int, int, Any]  # family, type, proto, sockaddr
Connector = Callable[[Address, float], socket.socket]


def _addresses(host: str, port: int) -> list[Address]:
    return [
        (family, kind, proto, sockaddr)
        for family, kind, proto, _, sockaddr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    ]


def _connect_one(address: Address, timeout: float) -> socket.socket:
    family, kind, proto, sockaddr = address
    sock = socket.socket(family, kind, proto)
    try:
        sock.settimeout(timeout)
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


def connect_first(
    addresses: Sequence[Address],
    connect_timeout: float = CONNECT_TIMEOUT,
    connector: Connector | None = None,
) -> socket.socket:
    """A socket to the first address that opens within `connect_timeout`, trying them
    in the resolver's order. Raises the last address's OSError when none does."""
    connect = connector or _connect_one
    last: OSError = OSError("no address to connect to")
    for address in addresses:
        try:
            return connect(address, connect_timeout)
        except OSError as exc:
            last = exc
    raise last


class _QuickConnection(http.client.HTTPSConnection):
    """HTTPS whose connection step gives each address CONNECT_TIMEOUT seconds; the
    request and the reply then have the call's own timeout."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = self._open_socket

    def _open_socket(
        self,
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: Any = None,
    ) -> socket.socket:
        host, port = address
        limit = CONNECT_TIMEOUT if timeout is None else min(CONNECT_TIMEOUT, timeout)
        sock = connect_first(_addresses(host, port), limit)
        sock.settimeout(timeout)
        return sock


class _QuickHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._quick_context = context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_QuickConnection, req, context=self._quick_context)


def _urlopen(
    request: urllib.request.Request, timeout: float, context: ssl.SSLContext
) -> http.client.HTTPResponse:
    """urllib's urlopen, with _QuickConnection for https."""
    opener = urllib.request.build_opener(_QuickHTTPSHandler(context))
    response: http.client.HTTPResponse = opener.open(request, timeout=timeout)
    return response


class TelegramAPI(Protocol):
    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any: ...

    def download(self, file_path: str, max_bytes: int) -> bytes: ...


class HttpTelegram:
    """The real client. Tests use a fake with the same two methods."""

    def __init__(self, token: str, *, timeout: float = 65) -> None:
        self._token = token
        self._timeout = timeout
        self._context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def _open(
        self, request: urllib.request.Request, what: str, timeout: float | None = None
    ) -> bytes:
        try:
            with _urlopen(  # fixed https:// API host
                request,
                timeout=self._timeout if timeout is None else timeout,
                context=self._context,
            ) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(MAX_ERROR_BODY).decode("utf-8", "replace")
            except OSError:
                body = ""
            message = redact(f"{what}: HTTP {exc.code} {body[:300]}", self._token)
            description = clean_description(_reply_description(body), self._token)
            if exc.code == 429:
                kind: type[TelegramError] = TelegramRateLimited
            elif 400 <= exc.code < 500:
                kind = TelegramRejected
            else:
                kind = TelegramUnreachable
            raise kind(message, method=what, error_code=exc.code, description=description) from None
        except (OSError, ValueError) as exc:
            raise TelegramUnreachable(
                redact(f"{what}: {type(exc).__name__}: {exc}", self._token),
                method=what,
                description=type(exc).__name__,
            ) from None

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """One Bot API call. `timeout`, when given, replaces the client's for this call."""
        request = urllib.request.Request(  # noqa: S310
            f"{API}/bot{self._token}/{method}",
            data=json.dumps(params or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        raw = self._open(request, method, timeout)
        try:
            reply = json.loads(raw)
        except ValueError:
            raise TelegramBadReply(f"{method}: the reply was not JSON", method=method) from None
        if not isinstance(reply, dict):
            raise TelegramBadReply(f"{method}: the reply was not a JSON object", method=method)
        if not reply.get("ok"):
            description = reply.get("description")
            message = redact(f"{method}: {description or 'not ok'}", self._token)
            code = _code(reply.get("error_code"))
            kind: type[TelegramError] = TelegramRateLimited if code == 429 else TelegramRejected
            raise kind(
                message,
                method=method,
                error_code=code,
                description=clean_description(description, self._token),
            )
        return reply.get("result")

    def download(self, file_path: str, max_bytes: int) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", file_path) or ".." in file_path:
            raise TelegramError("download: Telegram returned an unexpected file path")
        request = urllib.request.Request(f"{API}/file/bot{self._token}/{file_path}")  # noqa: S310
        data = self._open(request, "download")
        if len(data) > max_bytes:
            raise TelegramError(f"download: the file is larger than {max_bytes} bytes")
        return data
