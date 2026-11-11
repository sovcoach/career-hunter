"""Safe fetching: public addresses only, pinned to the checked address, on every hop."""

from __future__ import annotations

import http.client
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from careerhunter import fetch as f
from careerhunter.deadline import Deadline
from tests.fakes import WireResponse, wire

PUBLIC = "93.184.216.34"  # documentation-style public address; nothing is contacted


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeResponse:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b"<html>synthetic</html>"
    step: int | None = None  # bytes per read1; None hands out the whole body at once
    clock: FakeClock | None = None
    cost: float = 0.0  # seconds each read1 takes on the clock
    reads: int = 0

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read1(self, n: int = -1) -> bytes:
        self.reads += 1
        if self.clock is not None:
            self.clock.advance(self.cost)
        size = self.step or len(self.body)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


@dataclass
class Raise:
    """A route that raises instead of answering, from request() or getresponse()."""

    error: BaseException
    at: str = "response"


class FakeWeb:
    """Records every connection: scheme, host name, the address dialled, port and path."""

    def __init__(self, routes: dict[str, FakeResponse | Raise]) -> None:
        self.routes = routes
        self.dialled: list[tuple[str, str, str, int]] = []
        self.requests: list[tuple[str, str]] = []
        self.sent: list[tuple[str, bytes | None, dict[str, str]]] = []  # method, body, headers
        self.timeouts: list[float] = []
        self.rearmed: list[float] = []
        self.aborts = 0
        self.closes = 0
        self.clock: FakeClock | None = None
        self.request_cost = 0.0
        self.before_raise: Callable[[], None] | None = None
        self.on_close: Callable[[], None] | None = None

    def connector(self, scheme: str, host: str, address: str, port: int, timeout: float) -> Any:
        self.dialled.append((scheme, host, address, port))
        self.timeouts.append(timeout)
        web = self

        class Conn:
            def request(
                self, method: str, url: str, body: bytes | None = None, *, headers: dict[str, str]
            ) -> None:
                web.requests.append((host, url))
                web.sent.append((method, body, dict(headers)))
                self.key = f"{scheme}://{host}{url}"
                if web.clock is not None:
                    web.clock.advance(web.request_cost)
                route = web.routes.get(self.key)
                if isinstance(route, Raise) and route.at == "request":
                    raise route.error

            def getresponse(self) -> FakeResponse:
                route = web.routes.get(self.key, FakeResponse(404))
                if isinstance(route, Raise):
                    if web.before_raise is not None:
                        web.before_raise()
                    raise route.error
                return route

            def set_timeout(self, seconds: float) -> None:
                web.rearmed.append(seconds)

            def abort(self) -> None:
                web.aborts += 1

            def close(self) -> None:
                web.closes += 1
                if web.on_close is not None:
                    web.on_close()

        return Conn()


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


@pytest.fixture(autouse=True)
def dog(monkeypatch: pytest.MonkeyPatch) -> ManualWatchdog:
    """No test starts a real timer."""
    watchdog = ManualWatchdog()
    monkeypatch.setattr(f, "_timer", watchdog)
    return watchdog


def resolver(table: dict[str, list[str]]) -> Any:
    calls: list[str] = []
    timeouts: list[float] = []

    def resolve(host: str, port: int, timeout: float) -> list[str]:
        calls.append(host)
        timeouts.append(timeout)
        return table[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    resolve.timeouts = timeouts  # type: ignore[attr-defined]
    return resolve


def spawner() -> tuple[Callable[[Callable[[], None]], None], list[threading.Thread]]:
    threads: list[threading.Thread] = []

    def spawn(work: Callable[[], None]) -> None:
        thread = threading.Thread(target=work, daemon=True)
        threads.append(thread)
        thread.start()

    return spawn, threads


def answer(*addresses: str) -> list[tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]


def test_a_public_page_is_fetched_through_the_checked_address() -> None:
    web = FakeWeb(
        {"https://jobs.example.com/ad/1": FakeResponse(headers={"Content-Type": "text/html"})}
    )
    got = f.fetch(
        "https://jobs.example.com/ad/1",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    assert got.status == 200 and got.content_type == "text/html" and got.retry_after is None
    # Dialled the checked address, while the request still names the original host.
    assert web.dialled == [("https", "jobs.example.com", PUBLIC, 443)]
    assert web.requests == [("jobs.example.com", "/ad/1")]


def test_a_429_keeps_the_retry_after_header() -> None:
    web = FakeWeb(
        {
            "https://jobs.example.com/board": FakeResponse(
                status=429,
                headers={"Content-Type": "text/plain", "Retry-After": "120"},
                body=b"slow down",
            )
        }
    )
    got = f.fetch(
        "https://jobs.example.com/board",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    assert got.status == 429
    assert got.retry_after == "120"
    assert got.body == b"slow down"


def test_dns_rebinding_cannot_swap_in_a_private_address() -> None:
    """The host is resolved once per hop; a later answer can never be dialled."""
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    calls: list[str] = []

    def rebinding(host: str, port: int, timeout: float) -> list[str]:
        calls.append(host)
        return next(answers)

    web = FakeWeb({"http://rebind.example.com/": FakeResponse()})
    f.fetch("http://rebind.example.com/", resolver=rebinding, connector=web.connector)
    assert calls == ["rebind.example.com"]  # one lookup, no second chance
    assert [d[2] for d in web.dialled] == [PUBLIC]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.10",
        "169.254.169.254",  # link-local (cloud metadata)
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104 - unspecified, as an address to refuse
        "224.0.0.1",  # multicast
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    web = FakeWeb({})
    with pytest.raises(f.FetchRefused, match="private, local or reserved"):
        f.fetch(
            "https://evil.example.com/",
            resolver=resolver({"evil.example.com": [address]}),
            connector=web.connector,
        )
    assert web.dialled == []


def test_any_private_answer_among_several_is_refused() -> None:
    web = FakeWeb({})
    with pytest.raises(f.FetchRefused):
        f.fetch(
            "https://mixed.example.com/",
            resolver=resolver({"mixed.example.com": [PUBLIC, "10.0.0.1"]}),
            connector=web.connector,
        )
    assert web.dialled == []


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/admin", "http://[::1]:8080/", "https://10.1.2.3/"]
)
def test_literal_private_addresses_are_refused_without_resolving(url: str) -> None:
    def no_dns(host: str, port: int, timeout: float) -> list[str]:
        raise AssertionError("a literal address needs no lookup")

    with pytest.raises(f.FetchRefused):
        f.fetch(url, resolver=no_dns, connector=FakeWeb({}).connector)


def test_every_redirect_hop_is_checked_and_pinned() -> None:
    web = FakeWeb(
        {
            "https://jobs.example.com/a": FakeResponse(
                301, {"Location": "https://cdn.example.net/b"}
            ),
            "https://cdn.example.net/b": FakeResponse(
                302, {"Location": "http://inside.example.org/c"}
            ),
        }
    )
    table = {
        "jobs.example.com": [PUBLIC],
        "cdn.example.net": ["93.184.216.35"],
        "inside.example.org": ["192.168.0.2"],
    }
    resolve = resolver(table)
    with pytest.raises(f.FetchRefused, match="private"):
        f.fetch("https://jobs.example.com/a", resolver=resolve, connector=web.connector)
    assert resolve.calls == ["jobs.example.com", "cdn.example.net", "inside.example.org"]
    assert [d[2] for d in web.dialled] == [PUBLIC, "93.184.216.35"]  # never the private one


def test_a_redirect_to_another_scheme_is_refused() -> None:
    web = FakeWeb(
        {"https://jobs.example.com/a": FakeResponse(302, {"Location": "file:///etc/passwd"})}
    )
    with pytest.raises(f.FetchRefused, match="only http and https"):
        f.fetch(
            "https://jobs.example.com/a",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
        )


def test_too_many_redirects() -> None:
    web = FakeWeb(
        {"https://loop.example.com/": FakeResponse(302, {"Location": "https://loop.example.com/"})}
    )
    with pytest.raises(f.FetchFailed, match="more than 5 redirects"):
        f.fetch(
            "https://loop.example.com/",
            resolver=resolver({"loop.example.com": [PUBLIC]}),
            connector=web.connector,
        )
    assert len(web.dialled) == 6


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://jobs.example.com/x", "only http and https"),
        ("https://user:pw@jobs.example.com/x", "user name or password"),
        ("https:///nohost", "no host"),
        ("https://jobs.example.com:99999/x", "invalid port"),
    ],
)
def test_bad_links_are_refused(url: str, message: str) -> None:
    with pytest.raises(f.FetchRefused, match=message):
        f.fetch(
            url, resolver=resolver({"jobs.example.com": [PUBLIC]}), connector=FakeWeb({}).connector
        )


def test_bodies_are_capped() -> None:
    big = FakeResponse(body=b"x" * 2048, step=512)
    web = FakeWeb({"https://jobs.example.com/big": big})
    with pytest.raises(f.FetchTooLarge, match="larger than"):
        f.fetch(
            "https://jobs.example.com/big",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            max_bytes=1024,
        )
    assert big.reads == 3  # 512 + 512 + 512 passes 1024; nothing more is read


def test_a_declared_length_over_the_cap_is_refused_before_any_read() -> None:
    big = FakeResponse(headers={"Content-Length": "2048"}, body=b"x" * 2048)
    web = FakeWeb({"https://jobs.example.com/big": big})
    with pytest.raises(f.FetchTooLarge):
        f.fetch(
            "https://jobs.example.com/big",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            max_bytes=1024,
        )
    assert big.reads == 0


def test_the_pinned_https_connection_uses_the_host_name_for_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialled: list[tuple[str, int]] = []
    wrapped: list[str | None] = []

    class Ctx:
        def wrap_socket(self, sock: Any, server_hostname: str | None = None) -> Any:
            wrapped.append(server_hostname)
            return sock

    monkeypatch.setattr(
        f.socket, "create_connection", lambda addr, timeout=None: dialled.append(addr) or object()
    )
    conn = f._PinnedHTTPSConnection("jobs.example.com", PUBLIC, 443, 5.0, Ctx())  # type: ignore[arg-type]
    conn.connect()
    assert dialled == [(PUBLIC, 443)]
    assert wrapped == ["jobs.example.com"]
    assert conn.host == "jobs.example.com"  # so the Host header names the site, not the address


# The total deadline: the lookup, every hop and the body share one budget.


def test_the_lookup_gets_the_time_left() -> None:
    clock = FakeClock()
    resolve = resolver({"jobs.example.com": [PUBLIC]})
    web = FakeWeb({"https://jobs.example.com/": FakeResponse()})
    budget = Deadline(7.0, clock)
    f.fetch(
        "https://jobs.example.com/",
        resolver=resolve,
        connector=web.connector,
        clock=clock,
        budget=budget,
    )
    assert resolve.timeouts == [7.0]  # less than TIMEOUT, because the budget ends sooner


def test_an_expired_budget_never_resolves() -> None:
    clock = FakeClock()
    resolve = resolver({"jobs.example.com": [PUBLIC]})
    with pytest.raises(f.FetchTimeout):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolve,
            connector=FakeWeb({}).connector,
            clock=clock,
            budget=Deadline(0.0, clock),
        )
    assert resolve.calls == []


def test_redirect_hops_share_one_deadline(dog: ManualWatchdog) -> None:
    clock = FakeClock()
    web = FakeWeb(
        {
            "https://jobs.example.com/a": FakeResponse(301, {"Location": "/b"}),
            "https://jobs.example.com/b": FakeResponse(301, {"Location": "/c"}),
            "https://jobs.example.com/c": FakeResponse(),
        }
    )
    web.clock = clock
    web.request_cost = 25.0
    resolve = resolver({"jobs.example.com": [PUBLIC]})
    with pytest.raises(f.FetchTimeout, match="ran out of time"):
        f.fetch(
            "https://jobs.example.com/a",
            resolver=resolve,
            connector=web.connector,
            clock=clock,
            deadline=60.0,
        )
    assert len(web.dialled) == 3
    assert resolve.timeouts == [20.0, 20.0, 10.0]
    assert web.timeouts == [20.0, 20.0, 10.0]
    assert [a.seconds for a in dog.armed] == [60.0, 35.0, 10.0]


def test_a_drip_fed_body_hits_the_total_deadline() -> None:
    clock = FakeClock()
    drip = FakeResponse(body=b"x" * 1000, step=1, clock=clock, cost=1.0)
    web = FakeWeb({"https://jobs.example.com/": drip})
    with pytest.raises(f.FetchTimeout):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            clock=clock,
            deadline=10.0,
        )
    assert drip.reads == 10
    assert web.rearmed == [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]


def test_a_broken_reply_without_a_fire_is_a_failed_fetch() -> None:
    web = FakeWeb({"https://jobs.example.com/": Raise(http.client.IncompleteRead(b"par"))})
    with pytest.raises(f.FetchFailed, match="IncompleteRead"):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
        )


# The lookup thread: bounded by the caller's wait, and by a fixed number of slots.


def test_a_lookup_that_outlasts_its_wait_times_out() -> None:
    release = threading.Event()
    spawn, threads = spawner()

    def stuck(host: str, port: int, **_kw: Any) -> list[tuple[Any, ...]]:
        release.wait(5.0)
        return answer(PUBLIC)

    resolve = f.bounded_resolver(stuck, slots=2, spawn=spawn)
    with pytest.raises(f.FetchTimeout, match="could not be resolved in time"):
        resolve("slow.example.com", 443, 0.0)
    release.set()
    threads[0].join(5.0)
    assert not threads[0].is_alive()


def test_full_lookup_slots_fail_at_once_without_blocking() -> None:
    release = threading.Event()
    spawn, threads = spawner()
    calls: list[str] = []

    def stuck(host: str, port: int, **_kw: Any) -> list[tuple[Any, ...]]:
        calls.append(host)
        release.wait(5.0)
        return answer(PUBLIC)

    resolve = f.bounded_resolver(stuck, slots=1, spawn=spawn)
    with pytest.raises(f.FetchTimeout):
        resolve("slow.example.com", 443, 0.0)
    started = time.monotonic()
    with pytest.raises(f.FetchTimeout):
        resolve("next.example.com", 443, 5.0)  # the only slot is still held
    assert time.monotonic() - started < 1.0  # failed at once; it did not wait 5 s
    assert calls == ["slow.example.com"]
    release.set()
    threads[0].join(5.0)
    assert resolve("next.example.com", 443, 5.0) == [PUBLIC]  # the slot came back
    threads[1].join(5.0)


def test_a_lookup_error_is_a_failed_fetch_and_answers_are_deduplicated() -> None:
    spawn, threads = spawner()

    def lookup(host: str, port: int, **_kw: Any) -> list[tuple[Any, ...]]:
        if host == "missing.example.com":
            raise socket.gaierror("synthetic")
        return answer(PUBLIC, PUBLIC, "93.184.216.35")

    resolve = f.bounded_resolver(lookup, slots=1, spawn=spawn)
    with pytest.raises(f.FetchFailed, match="could not be resolved"):
        resolve("missing.example.com", 443, 5.0)
    assert resolve("jobs.example.com", 443, 5.0) == [PUBLIC, "93.184.216.35"]
    for thread in threads:
        thread.join(5.0)


def test_the_system_resolver_looks_up_getaddrinfo_at_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f.socket, "getaddrinfo", lambda host, port, **_kw: answer(PUBLIC))
    assert f.system_resolver("jobs.example.com", 443, 5.0) == [PUBLIC]


# The watchdog: armed and cancelled on every hop, and harmless once the hop is closed.


def _armed_once_each(dog: ManualWatchdog, hops: int) -> None:
    assert len(dog.armed) == hops
    assert [a.cancels for a in dog.armed] == [1] * hops


def test_the_watchdog_is_cancelled_on_a_plain_reply(dog: ManualWatchdog) -> None:
    web = FakeWeb({"https://jobs.example.com/": FakeResponse()})
    f.fetch(
        "https://jobs.example.com/",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    _armed_once_each(dog, 1)


def test_the_watchdog_is_cancelled_on_every_redirect_hop(dog: ManualWatchdog) -> None:
    web = FakeWeb(
        {
            "https://jobs.example.com/a": FakeResponse(301, {"Location": "/b"}),
            "https://jobs.example.com/b": FakeResponse(302, {"Location": "/c"}),
            "https://jobs.example.com/c": FakeResponse(),
        }
    )
    f.fetch(
        "https://jobs.example.com/a",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    _armed_once_each(dog, 3)
    assert web.closes == 3


def test_the_watchdog_is_cancelled_when_the_connection_fails(dog: ManualWatchdog) -> None:
    web = FakeWeb({"https://jobs.example.com/": Raise(ConnectionRefusedError(), at="request")})
    with pytest.raises(f.FetchFailed):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
        )
    _armed_once_each(dog, 1)


def test_the_watchdog_is_cancelled_when_the_deadline_runs_out(dog: ManualWatchdog) -> None:
    clock = FakeClock()
    drip = FakeResponse(body=b"x" * 100, step=1, clock=clock, cost=1.0)
    web = FakeWeb({"https://jobs.example.com/": drip})
    with pytest.raises(f.FetchTimeout):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            clock=clock,
            deadline=5.0,
        )
    _armed_once_each(dog, 1)
    assert dog.armed[0].seconds == 5.0


def test_a_fire_after_the_hop_closed_aborts_nothing(dog: ManualWatchdog) -> None:
    web = FakeWeb({"https://jobs.example.com/": FakeResponse()})
    f.fetch(
        "https://jobs.example.com/",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    dog.fire_all()
    assert web.aborts == 0


def test_a_fire_that_arrives_mid_close_aborts_nothing(dog: ManualWatchdog) -> None:
    web = FakeWeb({"https://jobs.example.com/": FakeResponse()})
    web.on_close = dog.fire_all  # the timer goes off while the hop is closing
    got = f.fetch(
        "https://jobs.example.com/",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    assert got.status == 200
    assert web.aborts == 0
    dog.fire_all()  # and once the close has finished
    assert web.aborts == 0


def test_abort_on_a_real_connection_is_harmless_before_connect_and_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    near, far = socket.socketpair()
    monkeypatch.setattr(f.socket, "create_connection", lambda addr, timeout=None: near)
    conn = f._PinnedHTTPConnection("jobs.example.com", PUBLIC, 80, 5.0)
    conn.abort()  # no socket yet
    conn.connect()
    conn.close()
    conn.abort()  # the socket is gone
    assert near.fileno() == -1
    far.close()


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError("synthetic"),
        ValueError("I/O operation on closed file"),
        http.client.IncompleteRead(b"par"),
        OSError("synthetic"),
        ConnectionResetError(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_any_error_after_the_watchdog_fired_is_a_timeout(
    dog: ManualWatchdog, error: BaseException
) -> None:
    web = FakeWeb({"https://jobs.example.com/": Raise(error)})
    web.before_raise = dog.fire_all  # the deadline passes while the reply is read
    with pytest.raises(f.FetchTimeout):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
        )
    assert web.aborts == 1


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError("synthetic"),
        http.client.IncompleteRead(b"par"),
        http.client.RemoteDisconnected("synthetic"),
        http.client.BadStatusLine("synthetic"),
        OSError("synthetic"),
        ConnectionResetError(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_network_and_protocol_errors_without_a_fire_are_failed_fetches(
    error: BaseException,
) -> None:
    web = FakeWeb({"https://jobs.example.com/": Raise(error)})
    with pytest.raises(f.FetchFailed, match=type(error).__name__):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
        )
    assert web.aborts == 0


def test_a_value_error_without_a_fire_propagates() -> None:
    web = FakeWeb({"https://jobs.example.com/": Raise(ValueError("synthetic bug"))})
    with pytest.raises(ValueError, match="synthetic bug"):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
        )


def test_a_value_error_after_the_deadline_passed_is_a_timeout() -> None:
    clock = FakeClock()
    web = FakeWeb({"https://jobs.example.com/": Raise(ValueError("closed file"))})
    web.before_raise = lambda: clock.advance(61.0)
    with pytest.raises(f.FetchTimeout):
        f.fetch(
            "https://jobs.example.com/",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            clock=clock,
        )


# The watchdog over real TLS: a server that drips the body, cut off by a fire.


@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway self-signed certificate for jobs.example.com, made by openssl."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.fail(
            "the TLS watchdog test needs the openssl command-line tool on PATH "
            "(LibreSSL on macOS or OpenSSL 3 on Linux); install it and run the tests again"
        )
    folder = tmp_path_factory.mktemp("tls")
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=jobs.example.com",
            "-addext",
            "subjectAltName=DNS:jobs.example.com",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def test_the_watchdog_cuts_off_a_real_tls_drip_feed(
    tls_cert: tuple[Path, Path], dog: ManualWatchdog, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert, key = tls_cert
    near, far = socket.socketpair()
    near.settimeout(30.0)  # a broken test fails instead of hanging
    far.settimeout(30.0)
    monkeypatch.setattr(f.socket, "create_connection", lambda addr, timeout=None: near)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.create_default_context(cafile=str(cert))
    problems: list[BaseException] = []
    dripped = threading.Event()

    def serve() -> None:
        try:
            with server_context.wrap_socket(far, server_side=True) as tls:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = tls.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                tls.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100\r\n\r\nabc"
                )
                dripped.set()
                dog.armed[-1].fire()  # the client is still waiting for 97 more bytes
                try:
                    while tls.recv(4096):
                        pass
                except OSError:
                    pass  # the client end is gone, as intended
        except BaseException as exc:
            problems.append(exc)
        finally:
            far.close()

    aborts: list[bool] = []

    class Counted(f._PinnedHTTPSConnection):
        def abort(self) -> None:
            aborts.append(self.sock is not None)
            super().abort()

    def connector(scheme: str, host: str, address: str, port: int, timeout: float) -> Any:
        return Counted(host, address, port, timeout, client_context)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    started = time.monotonic()
    with pytest.raises(f.FetchTimeout):
        f.fetch(
            "https://jobs.example.com/ad",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=connector,
            timeout=30.0,
        )
    # Woken by the shutdown, not by the 30 s socket timeout.
    assert time.monotonic() - started < 10.0
    server.join(5.0)
    assert not server.is_alive()
    assert problems == []
    assert dripped.is_set()  # the handshake and the headers really went over TLS
    assert aborts == [True]  # the real abort ran on a live TLS socket
    _armed_once_each(dog, 1)


# A body that ends early: checked against what the reply declared, not against how the OS
# reports a closed connection. These replies are real http.client responses read from
# canned bytes, so the stdlib's own Content-Length and chunked rules apply.


SITE = "https://jobs.example.com/"


def fetch_wired(response: WireResponse, **kw: Any) -> f.Fetched:
    web = FakeWeb({SITE: response})  # type: ignore[dict-item]
    return f.fetch(
        SITE, resolver=resolver({"jobs.example.com": [PUBLIC]}), connector=web.connector, **kw
    )


def test_a_declared_length_cut_short_is_a_failed_fetch() -> None:
    with pytest.raises(f.FetchFailed, match="body cut short"):
        fetch_wired(WireResponse(wire("Content-Length: 100", b"abc")))


def test_a_declared_length_cut_short_after_a_fire_is_a_timeout(dog: ManualWatchdog) -> None:
    with pytest.raises(f.FetchTimeout):
        fetch_wired(WireResponse(wire("Content-Length: 100", b"abc"), on_read=dog.fire_all))


def test_a_complete_declared_length_is_kept_when_the_deadline_passes_during_it() -> None:
    clock = FakeClock()
    response = WireResponse(wire("Content-Length: 10", b"0123456789"), lambda: clock.advance(61))
    got = fetch_wired(response, clock=clock)
    assert got.body == b"0123456789"


def test_a_complete_chunked_body_is_kept_when_the_deadline_passes_at_its_last_chunk() -> None:
    clock = FakeClock()
    reads: list[int] = []

    def late_on_the_final_chunk() -> None:
        reads.append(1)
        if len(reads) == 2:  # the read that finds the 0-size chunk
            clock.advance(61)

    body = b"5\r\nhello\r\n0\r\n\r\n"
    response = WireResponse(wire("Transfer-Encoding: chunked", body), late_on_the_final_chunk)
    got = fetch_wired(response, clock=clock)
    assert got.body == b"hello"
    assert response.reads == 2


@pytest.mark.parametrize(
    "body",
    [b"a\r\n0123", b"3\r\nabc\r\n"],
    ids=["mid-chunk", "at-a-chunk-boundary"],
)
def test_a_chunked_body_cut_short_is_a_failed_fetch(body: bytes) -> None:
    with pytest.raises(f.FetchFailed, match="IncompleteRead"):
        fetch_wired(WireResponse(wire("Transfer-Encoding: chunked", body)))


def test_a_body_of_unknown_length_ending_after_a_fire_is_a_timeout(dog: ManualWatchdog) -> None:
    with pytest.raises(f.FetchTimeout):
        fetch_wired(WireResponse(wire("Content-Type: text/plain", b"abc"), on_read=dog.fire_all))


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
    assert fetch_wired(WireResponse(wire(headers, body))).body == expected


# POST with a JSON body (#35): the same pinning, caps and deadline as a GET.


def test_a_post_sends_its_json_through_the_checked_address() -> None:
    web = FakeWeb({"https://acme.wd5.example.com/wday/jobs": FakeResponse(body=b'{"ok":1}')})
    got = f.post_json(
        "https://acme.wd5.example.com/wday/jobs",
        {"limit": 20, "offset": 0},
        resolver=resolver({"acme.wd5.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    assert got.status == 200 and got.body == b'{"ok":1}'
    assert web.dialled == [("https", "acme.wd5.example.com", PUBLIC, 443)]
    method, body, headers = web.sent[0]
    assert method == "POST"
    assert body == b'{"limit":20,"offset":0}'
    assert headers["Content-Type"] == "application/json"
    assert headers["Content-Length"] == str(len(body))


def test_a_get_sends_no_body() -> None:
    web = FakeWeb({"https://jobs.example.com/": FakeResponse()})
    f.fetch(
        "https://jobs.example.com/",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    assert web.sent == [("GET", None, {"User-Agent": f.USER_AGENT, "Accept": "*/*"})]


def test_a_board_api_get_asks_for_json() -> None:
    web = FakeWeb({"https://jobs.example.com/careers/list": FakeResponse(body=b"{}")})
    f.fetch(
        "https://jobs.example.com/careers/list",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
        accept=f.JSON,
    )
    assert web.sent == [("GET", None, {"User-Agent": f.USER_AGENT, "Accept": "application/json"})]


def test_an_html_reply_to_a_json_get_is_returned_as_it_came() -> None:
    """Asking for JSON gates nothing here: the caller's JSON parse does."""
    page = FakeResponse(headers={"Content-Type": "text/html"}, body=b"<html>synthetic</html>")
    web = FakeWeb({"https://jobs.example.com/careers/list": page})
    got = f.fetch(
        "https://jobs.example.com/careers/list",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
        accept=f.JSON,
    )
    assert (got.status, got.content_type, got.body) == (200, "text/html", b"<html>synthetic</html>")


def test_a_post_to_a_private_address_is_refused_before_dialling() -> None:
    web = FakeWeb({})
    with pytest.raises(f.FetchRefused, match="private"):
        f.post_json(
            "https://inside.example.org/jobs",
            {},
            resolver=resolver({"inside.example.org": ["10.0.0.5"]}),
            connector=web.connector,
        )
    assert web.dialled == []


def test_a_post_reply_is_capped() -> None:
    big = FakeResponse(body=b"x" * 2048, step=512)
    web = FakeWeb({"https://jobs.example.com/list": big})
    with pytest.raises(f.FetchTooLarge):
        f.post_json(
            "https://jobs.example.com/list",
            {},
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            max_bytes=1024,
        )
    assert big.reads == 3


def test_a_post_shares_the_total_deadline() -> None:
    clock = FakeClock()
    drip = FakeResponse(body=b"x" * 1000, step=1, clock=clock, cost=1.0)
    web = FakeWeb({"https://jobs.example.com/list": drip})
    with pytest.raises(f.FetchTimeout):
        f.post_json(
            "https://jobs.example.com/list",
            {},
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            clock=clock,
            deadline=10.0,
        )
    assert drip.reads == 10


def test_a_redirected_post_is_not_followed() -> None:
    web = FakeWeb(
        {
            "https://jobs.example.com/list": FakeResponse(
                307, {"Location": "https://cdn.example.net/x"}
            )
        }
    )
    resolve = resolver({"jobs.example.com": [PUBLIC], "cdn.example.net": ["93.184.216.35"]})
    with pytest.raises(f.FetchFailed, match="POST was redirected"):
        f.post_json("https://jobs.example.com/list", {}, resolver=resolve, connector=web.connector)
    assert resolve.calls == ["jobs.example.com"]
    assert len(web.dialled) == 1


def test_a_request_body_over_its_cap_is_refused_without_a_lookup() -> None:
    resolve = resolver({"jobs.example.com": [PUBLIC]})
    with pytest.raises(f.FetchRefused, match="request body"):
        f.post_json(
            "https://jobs.example.com/list",
            {"searchText": "x" * f.MAX_POST_BYTES},
            resolver=resolve,
            connector=FakeWeb({}).connector,
        )
    assert resolve.calls == []


def _no_lookup(host: str, port: int, timeout: float) -> list[str]:
    raise AssertionError("a never-fetched host must be refused before any lookup")


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/jobs/view/4000000001/",
        "https://www.linkedin.com/jobs/view/4000000001/",
        "https://ca.linkedin.com/jobs/view/4000000001/",
        "http://WWW.LINKEDIN.COM./jobs",
        "https://lnkd.in/abcdef",
    ],
)
def test_linkedin_is_refused_before_any_lookup_or_socket(url: str) -> None:
    web = FakeWeb({})
    with pytest.raises(f.FetchRefused, match="LinkedIn"):
        f.fetch(url, resolver=_no_lookup, connector=web.connector)
    assert web.dialled == []


def test_a_post_to_linkedin_is_refused_before_any_lookup() -> None:
    web = FakeWeb({})
    with pytest.raises(f.FetchRefused, match="LinkedIn"):
        f.post_json(
            "https://www.linkedin.com/x", {"a": 1}, resolver=_no_lookup, connector=web.connector
        )
    assert web.dialled == []


@pytest.mark.parametrize(
    "location", ["https://www.linkedin.com/jobs/view/4000000001/", "https://lnkd.in/abc"]
)
def test_a_redirect_to_linkedin_is_refused(location: str) -> None:
    web = FakeWeb({"https://jobs.example.com/a": FakeResponse(302, {"Location": location})})
    resolve = resolver({"jobs.example.com": [PUBLIC]})
    with pytest.raises(f.FetchRefused, match="LinkedIn"):
        f.fetch("https://jobs.example.com/a", resolver=resolve, connector=web.connector)
    assert resolve.calls == ["jobs.example.com"]
    assert len(web.dialled) == 1


def test_a_host_that_only_contains_the_name_is_fetched() -> None:
    web = FakeWeb({"https://notlinkedin.com/ad": FakeResponse()})
    got = f.fetch(
        "https://notlinkedin.com/ad",
        resolver=resolver({"notlinkedin.com": [PUBLIC]}),
        connector=web.connector,
    )
    assert got.status == 200
    assert web.dialled == [("https", "notlinkedin.com", PUBLIC, 443)]
