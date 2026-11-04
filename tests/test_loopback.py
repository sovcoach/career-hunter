"""The loopback exchange: one total deadline, loopback answers only, and a watchdog."""

from __future__ import annotations

import http.client
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from careerhunter import fetch, loopback
from careerhunter.deadline import Deadline
from tests.fakes import FakeClock, FakeServer, ManualWatchdog, Reply, WireResponse, wire


def call(server: FakeServer, clock: FakeClock | None = None, **kw: Any) -> tuple[int, bytes]:
    options: dict[str, Any] = {
        "body": b"{}",
        "headers": {"Accept": "application/json"},
        "total": 60.0,
        "per_op": 20.0,
        "max_bytes": 1024,
        "connector": server,
    }
    if clock is not None:
        options["clock"] = clock
    options.update(kw)
    return loopback.exchange("POST", "127.0.0.1", 1234, "/v1/x", **options)


def test_a_reply_is_returned_whole_and_the_connection_closed(
    loopback_watchdog: ManualWatchdog,
) -> None:
    server = FakeServer(Reply(body=b"abcdef", step=2))
    assert call(server) == (200, b"abcdef")
    assert server.requests == [("POST", "/v1/x", b"{}", {"Accept": "application/json"})]
    assert server.closes == 1
    assert [a.cancels for a in loopback_watchdog.armed] == [1]


def test_a_drip_fed_body_hits_the_total_deadline(loopback_watchdog: ManualWatchdog) -> None:
    clock = FakeClock()
    server = FakeServer(Reply(body=b"x" * 500, step=1, clock=clock, read_cost=1.0))
    with pytest.raises(loopback.LoopbackError):
        call(server, clock, total=10.0)
    assert server.reads == 10
    assert server.rearmed == [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
    assert [a.cancels for a in loopback_watchdog.armed] == [1]


def test_a_budget_that_ends_sooner_wins(loopback_watchdog: ManualWatchdog) -> None:
    clock = FakeClock()
    server = FakeServer(Reply())
    call(server, clock, total=60.0, budget=Deadline(7.0, clock))
    assert [timeout for *_rest, timeout in server.opened] == [7.0]
    assert [a.seconds for a in loopback_watchdog.armed] == [7.0]


def test_an_expired_budget_never_connects() -> None:
    clock = FakeClock()
    server = FakeServer(Reply())
    with pytest.raises(loopback.LoopbackError):
        call(server, clock, budget=Deadline(0.0, clock))
    assert server.opened == []


def test_a_body_over_the_cap_is_refused() -> None:
    server = FakeServer(Reply(body=b"y" * 50, step=4))
    with pytest.raises(loopback.LoopbackError):
        call(server, max_bytes=10)
    assert server.reads == 3


def test_the_watchdog_is_cancelled_when_the_request_fails(
    loopback_watchdog: ManualWatchdog,
) -> None:
    server = FakeServer(Reply(error=ConnectionRefusedError(), error_at="request"))
    with pytest.raises(loopback.LoopbackError):
        call(server)
    assert [a.cancels for a in loopback_watchdog.armed] == [1]
    assert server.closes == 1


@pytest.mark.parametrize(
    "error",
    [
        ValueError("I/O operation on closed file"),
        http.client.IncompleteRead(b"par"),
        OSError("synthetic"),
        ConnectionResetError(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_any_error_after_the_watchdog_fired_is_a_miss(
    loopback_watchdog: ManualWatchdog, error: BaseException
) -> None:
    server = FakeServer(Reply(error=error, before_error=loopback_watchdog.fire_all))
    with pytest.raises(loopback.LoopbackError):
        call(server)
    assert server.aborts == 1


@pytest.mark.parametrize(
    "error",
    [http.client.RemoteDisconnected("synthetic"), OSError("synthetic")],
    ids=lambda e: type(e).__name__,
)
def test_network_and_protocol_errors_without_a_fire_are_a_miss(error: BaseException) -> None:
    server = FakeServer(Reply(error=error))
    with pytest.raises(loopback.LoopbackError):
        call(server)
    assert server.aborts == 0


def test_a_value_error_without_a_fire_propagates() -> None:
    server = FakeServer(Reply(error=ValueError("synthetic bug")))
    with pytest.raises(ValueError, match="synthetic bug"):
        call(server)


def test_a_fire_after_close_or_mid_close_aborts_nothing(
    loopback_watchdog: ManualWatchdog,
) -> None:
    server = FakeServer(Reply())
    server.on_close = loopback_watchdog.fire_all
    assert call(server)[0] == 200
    loopback_watchdog.fire_all()
    assert server.aborts == 0


# Resolving a host name: inside the deadline, and loopback answers only.


def recording(answers: list[str]) -> Any:
    seen: list[tuple[str, float]] = []

    def resolve(host: str, port: int, timeout: float) -> list[str]:
        seen.append((host, timeout))
        return answers

    resolve.seen = seen  # type: ignore[attr-defined]
    return resolve


def via_localhost(server: FakeServer, resolver: Any, **kw: Any) -> tuple[int, bytes]:
    options: dict[str, Any] = {
        "body": None,
        "headers": {},
        "total": 30.0,
        "per_op": 20.0,
        "max_bytes": 1024,
        "connector": server,
        "resolver": resolver,
    }
    options.update(kw)
    return loopback.exchange("GET", "localhost", 8888, "/search", **options)


@pytest.mark.parametrize("answer", ["::1", "127.0.0.1", "127.0.0.53"])
def test_localhost_is_resolved_and_the_loopback_answer_dialled(answer: str) -> None:
    server = FakeServer(Reply())
    resolve = recording([answer])
    assert via_localhost(server, resolve)[0] == 200
    assert [(host, address) for host, address, _p, _t in server.opened] == [("localhost", answer)]


@pytest.mark.parametrize(
    "answers",
    [["93.184.216.34"], ["10.0.0.5"], ["127.0.0.1", "192.168.1.10"], ["::ffff:127.0.0.1"], []],
    ids=["public", "private", "mixed", "mapped", "none"],
)
def test_a_non_loopback_answer_is_refused_and_never_dialled(answers: list[str]) -> None:
    server = FakeServer(Reply())
    with pytest.raises(loopback.LoopbackError):
        via_localhost(server, recording(answers))
    assert server.opened == []


def test_a_literal_address_is_not_resolved() -> None:
    def no_dns(host: str, port: int, timeout: float) -> list[str]:
        raise AssertionError("a literal address needs no lookup")

    server = FakeServer(Reply())
    assert call(server, resolver=no_dns)[0] == 200


def test_the_lookup_gets_the_time_left() -> None:
    clock = FakeClock()
    resolve = recording(["127.0.0.1"])
    via_localhost(FakeServer(Reply()), resolve, clock=clock, total=7.0)
    assert resolve.seen == [("localhost", 7.0)]


def test_a_stuck_lookup_times_out_within_the_deadline() -> None:
    release = threading.Event()
    threads: list[threading.Thread] = []

    def spawn(work: Callable[[], None]) -> None:
        thread = threading.Thread(target=work, daemon=True)
        threads.append(thread)
        thread.start()

    def stuck(host: str, port: int, **_kw: Any) -> list[tuple[Any, ...]]:
        release.wait(5.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    clock = FakeClock()
    server = FakeServer(Reply())
    resolve = fetch.bounded_resolver(stuck, slots=1, spawn=spawn)
    with pytest.raises(loopback.LoopbackError):
        # A budget of no time left for this step: the wait for the lookup is zero.
        via_localhost(server, resolve, clock=clock, per_op=0.0)
    assert server.opened == []
    release.set()
    threads[0].join(5.0)
    assert not threads[0].is_alive()


# The watchdog over a real socket: a local server that drips the body.


def test_the_watchdog_cuts_off_a_real_drip_feed(
    loopback_watchdog: ManualWatchdog, monkeypatch: pytest.MonkeyPatch
) -> None:
    near, far = socket.socketpair()
    near.settimeout(30.0)  # a broken test fails instead of hanging
    far.settimeout(30.0)
    monkeypatch.setattr(loopback.socket, "create_connection", lambda addr, timeout=None: near)
    problems: list[BaseException] = []
    dripped = threading.Event()

    def serve() -> None:
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = far.recv(4096)
                if not chunk:
                    return
                request += chunk
            far.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nabc")
            dripped.set()
            loopback_watchdog.armed[-1].fire()  # the client still waits for 97 bytes
            try:
                while far.recv(4096):
                    pass
            except OSError:
                pass  # the client end is gone, as intended
        except BaseException as exc:
            problems.append(exc)
        finally:
            far.close()

    aborts: list[bool] = []

    class Counted(loopback.LoopbackConnection):
        def abort(self) -> None:
            aborts.append(self.sock is not None)
            super().abort()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    started = time.monotonic()
    with pytest.raises(loopback.LoopbackError):
        loopback.exchange(
            "GET",
            "127.0.0.1",
            8888,
            "/search",
            body=None,
            headers={},
            total=60.0,
            per_op=30.0,
            max_bytes=1024,
            connector=Counted,
        )
    # Woken by the shutdown, not by the 30 s socket timeout.
    assert time.monotonic() - started < 10.0
    server.join(5.0)
    assert not server.is_alive()
    assert problems == []
    assert dripped.is_set()
    assert aborts == [True]  # the real abort ran on a live socket


# A body that ends early, checked against what the reply declared. Real http.client
# responses read from canned bytes, so no OS reports a closed connection.


class Wired:
    """A connector whose one connection answers with `response`."""

    def __init__(self, response: WireResponse) -> None:
        self.response = response
        self.aborts = 0

    def __call__(self, host: str, address: str, port: int, timeout: float) -> Any:
        wired = self

        class Conn:
            def request(self, *args: Any, **kwargs: Any) -> None:
                pass

            def getresponse(self) -> WireResponse:
                return wired.response

            def set_timeout(self, seconds: float) -> None:
                pass

            def abort(self) -> None:
                wired.aborts += 1

            def close(self) -> None:
                pass

        return Conn()


def exchange_wired(response: WireResponse, **kw: Any) -> tuple[int, bytes]:
    return call(FakeServer(Reply()), connector=Wired(response), max_bytes=1 << 20, **kw)


def test_a_declared_length_cut_short_is_a_miss() -> None:
    with pytest.raises(loopback.LoopbackError):
        exchange_wired(WireResponse(wire("Content-Length: 100", b"abc")))


def test_a_declared_length_cut_short_after_a_fire_is_a_miss(
    loopback_watchdog: ManualWatchdog,
) -> None:
    response = WireResponse(wire("Content-Length: 100", b"abc"), loopback_watchdog.fire_all)
    with pytest.raises(loopback.LoopbackError):
        exchange_wired(response)


def test_a_complete_declared_length_is_kept_when_the_deadline_passes_during_it() -> None:
    clock = FakeClock()
    response = WireResponse(wire("Content-Length: 10", b"0123456789"), lambda: clock.advance(61))
    assert exchange_wired(response, clock=clock) == (200, b"0123456789")


def test_a_complete_chunked_body_is_kept_when_the_deadline_passes_at_its_last_chunk() -> None:
    clock = FakeClock()
    reads: list[int] = []

    def late_on_the_final_chunk() -> None:
        reads.append(1)
        if len(reads) == 2:  # the read that finds the 0-size chunk
            clock.advance(61)

    body = b"5\r\nhello\r\n0\r\n\r\n"
    response = WireResponse(wire("Transfer-Encoding: chunked", body), late_on_the_final_chunk)
    assert exchange_wired(response, clock=clock) == (200, b"hello")
    assert response.reads == 2


@pytest.mark.parametrize(
    "body",
    [b"a\r\n0123", b"3\r\nabc\r\n"],
    ids=["mid-chunk", "at-a-chunk-boundary"],
)
def test_a_chunked_body_cut_short_is_a_miss(body: bytes) -> None:
    with pytest.raises(loopback.LoopbackError):
        exchange_wired(WireResponse(wire("Transfer-Encoding: chunked", body)))


def test_a_body_of_unknown_length_ending_after_a_fire_is_a_miss(
    loopback_watchdog: ManualWatchdog,
) -> None:
    response = WireResponse(wire("Content-Type: text/plain", b"abc"), loopback_watchdog.fire_all)
    with pytest.raises(loopback.LoopbackError):
        exchange_wired(response)


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ("Content-Length: 5", b"hello", b"hello"),
        ("Transfer-Encoding: chunked", b"5\r\nhello\r\n0\r\n\r\n", b"hello"),
        ("Content-Type: text/plain", b"hello", b"hello"),
    ],
    ids=["declared-length", "chunked", "until-close"],
)
def test_a_complete_body_is_returned_whole(headers: str, body: bytes, expected: bytes) -> None:
    assert exchange_wired(WireResponse(wire(headers, body))) == (200, expected)
