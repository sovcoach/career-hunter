"""Fakes for the loopback clients: a clock, a watchdog that fires only when told, and a
local server that replays one reply. Nothing here opens a socket."""

from __future__ import annotations

import http.client
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Arming:
    seconds: float
    fire: Callable[[], None]
    cancels: int = 0

    def cancel(self) -> None:
        self.cancels += 1


class ManualWatchdog:
    """Stands in for the timer: records each arm and cancel, and fires only when told."""

    def __init__(self) -> None:
        self.armed: list[Arming] = []

    def __call__(self, seconds: float, fire: Callable[[], None]) -> Arming:
        arming = Arming(seconds, fire)
        self.armed.append(arming)
        return arming

    def fire_all(self) -> None:
        for arming in self.armed:
            arming.fire()


@dataclass
class Reply:
    """What the fake server does with one request."""

    status: int = 200
    body: bytes = b"{}"
    headers: dict[str, str] = field(default_factory=dict)
    step: int | None = None  # bytes per read1; None hands out the whole body at once
    clock: FakeClock | None = None
    header_cost: float = 0.0  # seconds before the status line arrives
    read_cost: float = 0.0  # seconds for each read1
    error: BaseException | None = None
    error_at: str = "response"  # "request", "response" or "read"
    before_error: Callable[[], None] | None = None


@dataclass
class FakeServer:
    """Stands in for LoopbackConnection. Records every connection and replays `reply`."""

    reply: Reply
    opened: list[tuple[str, str, int, float]] = field(default_factory=list)
    requests: list[tuple[str, str, bytes | None, dict[str, str]]] = field(default_factory=list)
    rearmed: list[float] = field(default_factory=list)
    reads: int = 0
    aborts: int = 0
    closes: int = 0
    on_close: Callable[[], None] | None = None

    def _fail(self, where: str) -> None:
        reply = self.reply
        if reply.error is not None and reply.error_at == where:
            if reply.before_error is not None:
                reply.before_error()
            raise reply.error

    def __call__(self, host: str, address: str, port: int, timeout: float) -> Any:
        self.opened.append((host, address, port, timeout))
        server = self
        reply = self.reply
        remaining = [reply.body]

        class Conn:
            status = reply.status

            def getheader(self, name: str) -> str | None:
                for key, value in reply.headers.items():
                    if key.lower() == name.lower():
                        return value
                return None

            def request(
                self,
                method: str,
                url: str,
                body: bytes | None = None,
                headers: Any = None,
            ) -> None:
                server.requests.append((method, url, body, dict(headers or {})))
                server._fail("request")

            def getresponse(self) -> Conn:
                if reply.clock is not None:
                    reply.clock.advance(reply.header_cost)
                server._fail("response")
                return self

            def read1(self, n: int = -1) -> bytes:
                server.reads += 1
                if reply.clock is not None:
                    reply.clock.advance(reply.read_cost)
                server._fail("read")
                size = reply.step or len(remaining[0])
                chunk, remaining[0] = remaining[0][:size], remaining[0][size:]
                return chunk

            def set_timeout(self, seconds: float) -> None:
                server.rearmed.append(seconds)

            def abort(self) -> None:
                server.aborts += 1

            def close(self) -> None:
                server.closes += 1
                if server.on_close is not None:
                    server.on_close()

        return Conn()


class _Wire:
    """Stands in for a socket: http.client reads the canned bytes, then end-of-file."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def makefile(self, mode: str) -> io.BufferedReader:
        return io.BufferedReader(io.BytesIO(self.data))


class WireResponse:
    """A real http.client.HTTPResponse read from canned bytes, so the stdlib's own rules
    for Content-Length and chunked bodies apply, with no socket and no OS involved.

    `on_read` runs before each read1, for example to advance a clock or fire a watchdog.
    """

    def __init__(self, wire: bytes, on_read: Callable[[], None] | None = None) -> None:
        self._response = http.client.HTTPResponse(_Wire(wire))  # type: ignore[arg-type]
        self._response.begin()
        self._on_read = on_read
        self.reads = 0

    @property
    def status(self) -> int:
        return self._response.status

    def getheader(self, name: str) -> str | None:
        return self._response.getheader(name)

    def read1(self, n: int = -1) -> bytes:
        self.reads += 1
        if self._on_read is not None:
            self._on_read()
        return self._response.read1(n)


def wire(headers: str, body: bytes) -> bytes:
    """An HTTP/1.1 200 reply with these header lines and this body, exactly as sent."""
    return b"HTTP/1.1 200 OK\r\n" + headers.encode() + b"\r\n\r\n" + body
