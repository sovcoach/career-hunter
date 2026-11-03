"""A total deadline on a fake clock, and a chunked body read that honours it and a cap."""

from __future__ import annotations

import pytest

from careerhunter import deadline as d


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DripResponse:
    """Hands out the body `step` bytes per read1, advancing the clock each time."""

    def __init__(self, body: bytes, clock: FakeClock, *, step: int, cost: float) -> None:
        self.body = body
        self.clock = clock
        self.step = step
        self.cost = cost
        self.sizes: list[int] = []

    def read1(self, n: int = -1) -> bytes:
        self.sizes.append(n)
        self.clock.advance(self.cost)
        chunk, self.body = self.body[: self.step], self.body[self.step :]
        return chunk


def test_remaining_counts_down_and_stops_at_zero() -> None:
    clock = FakeClock()
    limit = d.Deadline(10.0, clock)
    assert limit.remaining() == 10.0
    clock.advance(4.0)
    assert limit.remaining() == 6.0
    assert not limit.expired()
    clock.advance(7.0)
    assert limit.remaining() == 0.0
    assert limit.expired()


def test_cap_is_the_smaller_of_the_step_timeout_and_the_time_left() -> None:
    clock = FakeClock()
    limit = d.Deadline(30.0, clock)
    assert limit.cap(20.0) == 20.0
    clock.advance(25.0)
    assert limit.cap(20.0) == 5.0
    clock.advance(5.0)
    with pytest.raises(d.DeadlineExceeded):
        limit.cap(20.0)


def test_sooner_picks_the_deadline_that_ends_first() -> None:
    clock = FakeClock()
    short = d.Deadline(5.0, clock)
    long = d.Deadline(50.0, clock)
    assert short.sooner(long) is short
    assert long.sooner(short) is short
    assert long.sooner(None) is long


def test_a_body_is_read_in_chunks_and_each_recv_gets_the_time_left() -> None:
    clock = FakeClock()
    limit = d.Deadline(10.0, clock)
    response = DripResponse(b"abcdefgh", clock, step=3, cost=2.0)
    timeouts: list[float] = []
    body = d.read_capped(
        response, max_bytes=100, deadline=limit, per_op=20.0, rearm=timeouts.append
    )
    assert body == b"abcdefgh"
    assert timeouts == [10.0, 8.0, 6.0, 4.0]
    assert response.sizes == [d.CHUNK] * 4  # read1: at most one recv per chunk


def test_a_drip_feed_hits_the_total_deadline() -> None:
    clock = FakeClock()
    limit = d.Deadline(10.0, clock)
    response = DripResponse(b"x" * 1000, clock, step=1, cost=1.0)
    with pytest.raises(d.DeadlineExceeded):
        d.read_capped(response, max_bytes=5000, deadline=limit, per_op=20.0, rearm=lambda _: None)
    assert len(response.sizes) == 10  # stopped at the deadline, not at the end of the body


def test_a_body_over_the_cap_is_refused_without_reading_further() -> None:
    clock = FakeClock()
    limit = d.Deadline(10.0, clock)
    response = DripResponse(b"y" * 50, clock, step=4, cost=0.0)
    with pytest.raises(d.BodyTooLarge):
        d.read_capped(response, max_bytes=10, deadline=limit, per_op=20.0, rearm=lambda _: None)
    assert len(response.sizes) == 3  # 4 + 4 + 4 bytes passes 10; nothing more is read
