"""A total time limit for one network call, measured on a monotonic clock.

A socket timeout covers one operation and starts again on every hop and every
`recv`, so a server that drips one byte at a time can keep a call alive far past it.
A `Deadline` is created once per call and caps every step: the lookup, the connect,
each redirect hop and each chunk of the body.

`read_capped` reads a body in chunks with `read1`, which makes at most one `recv` per
call. Before each chunk the socket timeout is set to the time left, so no single
`recv` can outlast the deadline.

A connection can end before the body does without any error: `http.client`'s `read1`
returns an empty read when a `Content-Length` body ends early, and TLS suppresses an
unexpected end-of-file by default. So a body is complete only when it can be checked:
its declared length arrived, or its chunked encoding reached the final chunk (a
chunked body that ends early raises `IncompleteRead`). A body that falls short of its
declared length is `BodyCutShort`. A body with no declared length that ends at close
cannot be checked; the caller decides what that means.

A socket timeout cannot bound the reply headers, which `http.client` reads in many
`recv` calls. A `Hop` pairs one connection with a watchdog timer that shuts the socket
down when the deadline passes, which wakes any blocked read.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

Clock = Callable[[], float]
CHUNK = 64 * 1024


class DeadlineExceeded(Exception):
    """The time for this call ran out."""


class BodyTooLarge(Exception):
    """The body is larger than the cap. Nothing past the cap is read."""


class BodyCutShort(Exception):
    """The connection ended before the body reached its declared length."""


class Chunked(Protocol):
    def read1(self, n: int = -1) -> bytes: ...


class Armed(Protocol):
    def cancel(self) -> None: ...


class Abortable(Protocol):
    def abort(self) -> None: ...

    def close(self) -> None: ...


Watchdog = Callable[[float, Callable[[], None]], Armed]


def start_timer(seconds: float, fire: Callable[[], None]) -> Armed:
    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return timer


class Controls:
    """A timeout that can change between reads, and a shutdown another thread can call."""

    sock: Any

    def set_timeout(self, seconds: float) -> None:
        if self.sock is not None:
            self.sock.settimeout(seconds)

    def abort(self) -> None:
        """Wake a blocked read by shutting the socket down. No effect once closed."""
        sock = self.sock
        if sock is None:
            return
        # The plain socket's shutdown, not SSLSocket's: that one drops the SSL object
        # first, which a read blocked in another thread is still using.
        with contextlib.suppress(OSError, ValueError):
            socket.socket.shutdown(sock, socket.SHUT_RDWR)


class Hop:
    """One connection and its watchdog. The lock keeps a late fire off a closing hop."""

    def __init__(self, conn: Abortable) -> None:
        self.conn = conn
        self.lock = threading.Lock()
        self.closed = False
        self.fired = False

    def fire(self) -> None:
        # Only this fire, which runs once, and close take the lock. A fire that cannot
        # take it has arrived mid-close, and there is nothing left to abort.
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.closed:
                return
            self.fired = True
            self.conn.abort()
        finally:
            self.lock.release()

    def close(self, armed: Armed) -> None:
        with self.lock:
            self.closed = True
            armed.cancel()
            self.conn.close()


class Deadline:
    """The end of a time budget, fixed when it is created."""

    def __init__(self, seconds: float, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self._end - self._clock() <= 0

    def cap(self, per_op: float) -> float:
        """The timeout for the next step: `per_op`, or less when less time is left."""
        left = self._end - self._clock()
        if left <= 0:
            raise DeadlineExceeded
        return min(per_op, left)

    def sooner(self, other: Deadline | None) -> Deadline:
        """Whichever of the two ends first. Both must use the same clock."""
        if other is None or self._end <= other._end:
            return self
        return other


def framing(getheader: Callable[[str], str | None], status: int) -> tuple[int | None, bool]:
    """The declared body length, and whether the body is chunked.

    Chunked encoding overrides `Content-Length`. A 204 or 304 reply has no body, so its
    length is 0. A missing or malformed `Content-Length` declares nothing.
    """
    if status in (204, 304):
        return 0, False
    encoding = (getheader("Transfer-Encoding") or "").lower()
    if "chunked" in encoding:
        return None, True
    value = getheader("Content-Length")
    if value is None:
        return None, False
    try:
        declared = int(value.strip())
    except ValueError:
        return None, False
    return (declared, False) if declared >= 0 else (None, False)


def read_capped(
    response: Chunked,
    *,
    max_bytes: int,
    deadline: Deadline,
    per_op: float,
    rearm: Callable[[float], None],
    declared: int | None = None,
) -> bytes:
    """The whole body, or BodyTooLarge past `max_bytes`, DeadlineExceeded, or
    BodyCutShort when the connection ends before `declared` bytes arrived.

    Once `declared` bytes are in, the body is complete and nothing more is read.
    """
    parts: list[bytes] = []
    size = 0
    while declared is None or size < declared:
        rearm(deadline.cap(per_op))
        chunk = response.read1(CHUNK)
        if not chunk:
            if declared is not None:
                raise BodyCutShort
            break
        size += len(chunk)
        if size > max_bytes:
            raise BodyTooLarge
        parts.append(chunk)
    return b"".join(parts)
