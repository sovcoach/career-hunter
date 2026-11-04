"""Fetching a job-ad URL safely: public addresses only, pinned against DNS rebinding.

A URL sent to the bot is fetched from the Mac mini, which sits on a private network. So
before each request (the first and every redirect hop):

1. the URL must be http or https, with no user name or password in it;
2. the host is resolved once, and every address it resolves to must be public (not
   loopback, private, link-local, carrier-grade NAT, multicast, reserved or
   unspecified; an IPv4 address mapped into IPv6 is checked as IPv4);
3. the connection is made to that checked address itself, not to the host name, with
   the original Host header and, for https, the original name for SNI and certificate
   verification.

Step 3 is what stops DNS rebinding: a second lookup between the check and the connect
could return a private address, so there is no second lookup. Redirects are followed by
hand (at most five), each going through all three steps again. Bodies are capped.

One call has one total deadline, shared by the lookup, every hop and the body. A
socket timeout starts again on every `recv`, so it cannot bound a slow drip on its
own: the lookup waits only for the time left, each body chunk re-arms the socket
timeout to the time left, and a watchdog shuts the socket down when the deadline
passes, which also covers the headers.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import truststore

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

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 20.0  # one step: the lookup, the connect, the headers, or one chunk of the body
DEADLINE = 60.0  # the whole call: every redirect hop and the body
LOOKUP_SLOTS = 4  # lookups still running after their caller stopped waiting
USER_AGENT = "career-hunter (job-ad snapshot; self-hosted)"


class FetchError(Exception):
    """A URL that wasn't fetched, and why. The message is user-facing."""


class FetchRefused(FetchError):
    """The link, or an address it resolves to, is not allowed."""


class FetchTimeout(FetchError):
    """The call ran out of time: in the lookup, on a hop, or in the body."""


class FetchTooLarge(FetchError):
    """The body is larger than the cap."""


class FetchFailed(FetchError):
    """No usable reply: the lookup, the network or the server failed, or redirects looped."""


@dataclass(frozen=True)
class Fetched:
    url: str  # after redirects
    status: int
    content_type: str
    body: bytes
    retry_after: str | None = None


class Response(Protocol):
    @property
    def status(self) -> int: ...

    def getheader(self, name: str) -> str | None: ...

    def read1(self, n: int = -1) -> bytes: ...


class Connection(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None: ...

    def getresponse(self) -> Response: ...

    def set_timeout(self, seconds: float) -> None: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


Resolver = Callable[[str, int, float], list[str]]
Connector = Callable[[str, str, str, int, float], Connection]
Lookup = Callable[..., Sequence[tuple[Any, ...]]]
Spawn = Callable[[Callable[[], None]], None]


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def bounded_resolver(
    lookup: Lookup | None = None, *, slots: int = LOOKUP_SLOTS, spawn: Spawn = _thread
) -> Resolver:
    """A resolver that gives up at the caller's timeout.

    getaddrinfo has no timeout of its own, so each lookup runs in a daemon thread and the
    caller waits only for the time it has left. A lookup still running holds one of
    `slots`; with every slot taken, a new lookup fails at once instead of waiting.
    `lookup` defaults to `socket.getaddrinfo`, looked up at each call.
    """
    gate = threading.BoundedSemaphore(slots)

    def resolve(host: str, port: int, timeout: float) -> list[str]:
        if not gate.acquire(blocking=False):
            raise FetchTimeout(f"{host} could not be resolved in time")
        done = threading.Event()
        found: list[Sequence[tuple[Any, ...]]] = []
        failed: list[Exception] = []

        def work() -> None:
            try:
                call = lookup or socket.getaddrinfo
                found.append(call(host, port, type=socket.SOCK_STREAM))
            except Exception as exc:
                failed.append(exc)
            finally:
                gate.release()
                done.set()

        spawn(work)
        if not done.wait(timeout):
            raise FetchTimeout(f"{host} could not be resolved in time")
        if failed or not found:
            raise FetchFailed(f"{host} could not be resolved") from (failed[0] if failed else None)
        return list(dict.fromkeys(str(info[4][0]) for info in found[0]))

    return resolve


system_resolver = bounded_resolver()


def check_address(address: str) -> str:
    """Return the address if it is public, else raise FetchRefused."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise FetchRefused(f"{address!r} is not an IP address") from exc
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise FetchRefused("refusing to fetch from a private, local or reserved address")
    return str(ip)


class _PinnedHTTPConnection(Controls, http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(Controls, http.client.HTTPSConnection):
    def __init__(
        self, host: str, address: str, port: int, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._pinned_context = context

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        # SNI and certificate checks use the original host name, not the address.
        self.sock = self._pinned_context.wrap_socket(raw, server_hostname=self.host)


def pinned_connector(scheme: str, host: str, address: str, port: int, timeout: float) -> Connection:
    if scheme == "https":
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return _PinnedHTTPSConnection(host, address, port, timeout, context)
    return _PinnedHTTPConnection(host, address, port, timeout)


def _late(host: str) -> str:
    return f"fetching {host} ran out of time"


def _step(limit: Deadline, timeout: float, host: str) -> float:
    try:
        return limit.cap(timeout)
    except DeadlineExceeded:
        raise FetchTimeout(_late(host)) from None


def _target(
    url: str, resolver: Resolver, limit: Deadline, timeout: float
) -> tuple[urllib.parse.SplitResult, str, int, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchRefused("only http and https links can be fetched")
    if parts.username is not None or parts.password is not None:
        raise FetchRefused("links with a user name or password are refused")
    host = parts.hostname
    if not host:
        raise FetchRefused("the link has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise FetchRefused("the link has an invalid port") from exc
    try:
        ipaddress.ip_address(host)
        addresses = [host]  # a literal address: nothing to resolve
    except ValueError:
        addresses = resolver(host, port, _step(limit, timeout, host))
    if not addresses:
        raise FetchFailed(f"{host} has no addresses")
    checked = [check_address(a) for a in addresses]  # every address must be public
    return parts, host, port, checked[0]


def _too_large(max_bytes: int) -> str:
    return f"the page is larger than {max_bytes // (1024 * 1024)} MB"


def fetch(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    connector: Connector = pinned_connector,
    max_bytes: int = MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    timeout: float = TIMEOUT,
    deadline: float = DEADLINE,
    budget: Deadline | None = None,
    clock: Clock = time.monotonic,
    watchdog: Watchdog | None = None,
) -> Fetched:
    """One GET, within `deadline` seconds, or within `budget` when that ends sooner."""
    limit = Deadline(deadline, clock).sooner(budget)
    arm = watchdog or _timer
    for _ in range(max_redirects + 1):
        parts, host, port, address = _target(url, resolver, limit, timeout)
        conn = connector(parts.scheme, host, address, port, _step(limit, timeout, host))
        hop = Hop(conn)
        armed = arm(limit.remaining(), hop.fire)
        try:
            path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            if hop.fired or limit.expired():
                raise FetchTimeout(_late(host))
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise FetchFailed("a redirect had no destination")
                url = urllib.parse.urljoin(url, location)
                continue
            declared, chunked = framing(response.getheader, response.status)
            if declared is not None and declared > max_bytes:
                raise FetchTooLarge(_too_large(max_bytes))
            body = read_capped(
                response,
                max_bytes=max_bytes,
                deadline=limit,
                per_op=timeout,
                rearm=conn.set_timeout,
                declared=declared,
            )
            # A declared length that arrived, or a chunked body that reached its last
            # chunk, is complete. A body that ended at close cannot be checked, so after a
            # fire or past the deadline it may be cut short: that is a timeout.
            if declared is None and not chunked and (hop.fired or limit.expired()):
                raise FetchTimeout(_late(host))
            content_type = response.getheader("Content-Type") or ""
            return Fetched(
                url, response.status, content_type, body, response.getheader("Retry-After")
            )
        except BodyTooLarge:
            raise FetchTooLarge(_too_large(max_bytes)) from None
        except DeadlineExceeded:
            raise FetchTimeout(_late(host)) from None
        except BodyCutShort:
            if hop.fired or limit.expired():
                raise FetchTimeout(_late(host)) from None
            raise FetchFailed(f"couldn't fetch {host}: body cut short") from None
        except ValueError as exc:
            # A read on a socket the watchdog shut can raise ValueError. Any other is a bug.
            if hop.fired or limit.expired():
                raise FetchTimeout(_late(host)) from exc
            raise
        except (OSError, http.client.HTTPException) as exc:
            if hop.fired or limit.expired():
                raise FetchTimeout(_late(host)) from exc
            raise FetchFailed(f"couldn't fetch {host}: {type(exc).__name__}") from exc
        finally:
            hop.close(armed)
    raise FetchFailed(f"more than {max_redirects} redirects")
