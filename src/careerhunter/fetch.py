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
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import truststore

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 20.0
USER_AGENT = "career-hunter (job-ad snapshot; self-hosted)"


class FetchError(Exception):
    """A URL that wasn't fetched, and why. The message is user-facing."""


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

    def read(self, amt: int | None = None) -> bytes: ...


class Connection(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None: ...

    def getresponse(self) -> Response: ...

    def close(self) -> None: ...


Resolver = Callable[[str, int], list[str]]
Connector = Callable[[str, str, str, int, float], Connection]


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"{host} could not be resolved") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def check_address(address: str) -> str:
    """Return the address if it is public, else raise FetchError."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise FetchError(f"{address!r} is not an IP address") from exc
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise FetchError("refusing to fetch from a private, local or reserved address")
    return str(ip)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
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


def _hop(url: str, resolver: Resolver) -> tuple[urllib.parse.SplitResult, str, int, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http and https links can be fetched")
    if parts.username is not None or parts.password is not None:
        raise FetchError("links with a user name or password are refused")
    host = parts.hostname
    if not host:
        raise FetchError("the link has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise FetchError("the link has an invalid port") from exc
    try:
        ipaddress.ip_address(host)
        addresses = [host]  # a literal address: nothing to resolve
    except ValueError:
        addresses = resolver(host, port)
    if not addresses:
        raise FetchError(f"{host} has no addresses")
    checked = [check_address(a) for a in addresses]  # every address must be public
    return parts, host, port, checked[0]


def fetch(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    connector: Connector = pinned_connector,
    max_bytes: int = MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    timeout: float = TIMEOUT,
) -> Fetched:
    for _ in range(max_redirects + 1):
        parts, host, port, address = _hop(url, resolver)
        conn = connector(parts.scheme, host, address, port, timeout)
        try:
            path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise FetchError("a redirect had no destination")
                url = urllib.parse.urljoin(url, location)
                continue
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError(f"the page is larger than {max_bytes // (1024 * 1024)} MB")
            content_type = response.getheader("Content-Type") or ""
            return Fetched(
                url, response.status, content_type, body, response.getheader("Retry-After")
            )
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError(f"couldn't fetch {host}: {type(exc).__name__}") from exc
        finally:
            conn.close()
    raise FetchError(f"more than {max_redirects} redirects")
