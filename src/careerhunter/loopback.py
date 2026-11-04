"""One HTTP exchange with a server on this Mac, within a total deadline.

The scoring, embedding and SearXNG clients talk to loopback only. None of them is
`fetch.py`, which refuses loopback. They share this exchange instead:

- one total deadline covers the lookup, the connect, the reply headers and the body;
- a host name is resolved inside that deadline, and only a loopback answer
  (127.0.0.0/8 or ::1) is used; anything else is refused;
- the body is read in chunks, with the socket timeout re-armed to the time left, and a
  body that ends before its declared length is a failure;
- a watchdog shuts the socket down when the deadline passes.

Redirects are not followed: a 3xx status is returned as it is. Every failure is one
`LoopbackError` with a fixed message, so no URL, query or text reaches a log.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from careerhunter.deadline import (
    BodyCutShort,
    BodyTooLarge,
    Clock,
    Controls,
    Deadline,
    DeadlineExceeded,
    Hop,
    Watchdog,
    framing,
    read_capped,
)
from careerhunter.deadline import start_timer as _timer
from careerhunter.fetch import FetchError, Resolver, bounded_resolver

_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_address("::1")
_clock: Clock = time.monotonic
# Its own lookup slots: a stuck public lookup in fetch.py cannot hold these.
_resolver: Resolver = bounded_resolver()


class LoopbackError(Exception):
    """No usable reply from the local server."""

    def __init__(self) -> None:
        super().__init__("no usable reply from the local server")


class Response(Protocol):
    @property
    def status(self) -> int: ...

    def getheader(self, name: str) -> str | None: ...

    def read1(self, n: int = -1) -> bytes: ...


class Connection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] = ...,
    ) -> None: ...

    def getresponse(self) -> Response: ...

    def set_timeout(self, seconds: float) -> None: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


Connector = Callable[[str, str, int, float], Connection]


class LoopbackConnection(Controls, http.client.HTTPConnection):
    """Connects to the checked address. The Host header keeps the URL's host."""

    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def _address(host: str, port: int, limit: Deadline, per_op: float, resolver: Resolver) -> str:
    try:
        ipaddress.ip_address(host)
        answers = [host]
    except ValueError:
        try:
            answers = resolver(host, port, limit.cap(per_op))
        except (FetchError, DeadlineExceeded):
            raise LoopbackError from None
    if not answers:
        raise LoopbackError
    for answer in answers:
        try:
            ip = ipaddress.ip_address(answer.split("%", 1)[0])
        except ValueError:
            raise LoopbackError from None
        # Exactly 127.0.0.0/8 or ::1. Not is_loopback, which newer Pythons also grant to
        # an IPv4-mapped address such as ::ffff:127.0.0.1.
        if not (ip in _LOOPBACK_V4 or ip == _LOOPBACK_V6):
            raise LoopbackError
    return answers[0]


def exchange(
    method: str,
    host: str,
    port: int,
    path: str,
    *,
    body: bytes | None,
    headers: Mapping[str, str],
    total: float,
    per_op: float,
    max_bytes: int,
    budget: Deadline | None = None,
    clock: Clock | None = None,
    watchdog: Watchdog | None = None,
    connector: Connector | None = None,
    resolver: Resolver | None = None,
) -> tuple[int, bytes]:
    """The status and the whole body, within `total`, or within `budget` when sooner."""
    limit = Deadline(total, clock or _clock).sooner(budget)
    address = _address(host, port, limit, per_op, resolver or _resolver)
    try:
        timeout = limit.cap(per_op)
    except DeadlineExceeded:
        raise LoopbackError from None
    conn = (connector or LoopbackConnection)(host, address, port, timeout)
    hop = Hop(conn)
    armed = (watchdog or _timer)(limit.remaining(), hop.fire)
    try:
        conn.request(method, path, body=body, headers=dict(headers))
        if hop.fired or limit.expired():
            raise LoopbackError
        response = conn.getresponse()
        declared, chunked = framing(response.getheader, response.status)
        data = read_capped(
            response,
            max_bytes=max_bytes,
            deadline=limit,
            per_op=per_op,
            rearm=conn.set_timeout,
            declared=declared,
        )
        # A declared length that arrived, or a chunked body that reached its last chunk,
        # is complete. A body that ended at close cannot be checked, so after a fire or
        # past the deadline it may be cut short.
        if declared is None and not chunked and (hop.fired or limit.expired()):
            raise LoopbackError
        return response.status, data
    except (BodyTooLarge, BodyCutShort, DeadlineExceeded):
        raise LoopbackError from None
    except ValueError as exc:
        # A read on a socket the watchdog shut can raise ValueError. Any other is a bug.
        if hop.fired or limit.expired():
            raise LoopbackError from exc
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise LoopbackError from exc
    finally:
        hop.close(armed)
