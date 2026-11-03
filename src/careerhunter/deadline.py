"""A total time limit for one network call, measured on a monotonic clock.

A socket timeout covers one operation and starts again on every hop and every
`recv`, so a server that drips one byte at a time can keep a call alive far past it.
A `Deadline` is created once per call and caps every step: the lookup, the connect,
each redirect hop and each chunk of the body.

`read_capped` reads a body in chunks with `read1`, which makes at most one `recv` per
call. Before each chunk the socket timeout is set to the time left, so no single
`recv` can outlast the deadline.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

Clock = Callable[[], float]
CHUNK = 64 * 1024


class DeadlineExceeded(Exception):
    """The time for this call ran out."""


class BodyTooLarge(Exception):
    """The body is larger than the cap. Nothing past the cap is read."""


class Chunked(Protocol):
    def read1(self, n: int = -1) -> bytes: ...


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


def read_capped(
    response: Chunked,
    *,
    max_bytes: int,
    deadline: Deadline,
    per_op: float,
    rearm: Callable[[float], None],
) -> bytes:
    """The whole body, or BodyTooLarge past `max_bytes`, or DeadlineExceeded."""
    parts: list[bytes] = []
    size = 0
    while True:
        rearm(deadline.cap(per_op))
        chunk = response.read1(CHUNK)
        if not chunk:
            return b"".join(parts)
        size += len(chunk)
        if size > max_bytes:
            raise BodyTooLarge
        parts.append(chunk)
