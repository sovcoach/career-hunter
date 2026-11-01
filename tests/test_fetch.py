"""Safe fetching: public addresses only, pinned to the checked address, on every hop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from careerhunter import fetch as f

PUBLIC = "93.184.216.34"  # documentation-style public address; nothing is contacted


@dataclass
class FakeResponse:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b"<html>synthetic</html>"

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read(self, amt: int | None = None) -> bytes:
        return self.body if amt is None else self.body[:amt]


class FakeWeb:
    """Records every connection: scheme, host name, the address dialled, port and path."""

    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.dialled: list[tuple[str, str, str, int]] = []
        self.requests: list[tuple[str, str]] = []

    def connector(self, scheme: str, host: str, address: str, port: int, timeout: float) -> Any:
        self.dialled.append((scheme, host, address, port))
        web = self

        class Conn:
            def request(self, method: str, url: str, *, headers: dict[str, str]) -> None:
                web.requests.append((host, url))
                self.key = f"{scheme}://{host}{url}"

            def getresponse(self) -> FakeResponse:
                return web.routes.get(self.key, FakeResponse(404))

            def close(self) -> None:
                pass

        return Conn()


def resolver(table: dict[str, list[str]]) -> Any:
    calls: list[str] = []

    def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        return table[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def test_a_public_page_is_fetched_through_the_checked_address() -> None:
    web = FakeWeb(
        {"https://jobs.example.com/ad/1": FakeResponse(headers={"Content-Type": "text/html"})}
    )
    got = f.fetch(
        "https://jobs.example.com/ad/1",
        resolver=resolver({"jobs.example.com": [PUBLIC]}),
        connector=web.connector,
    )
    assert got.status == 200 and got.content_type == "text/html"
    # Dialled the checked address, while the request still names the original host.
    assert web.dialled == [("https", "jobs.example.com", PUBLIC, 443)]
    assert web.requests == [("jobs.example.com", "/ad/1")]


def test_dns_rebinding_cannot_swap_in_a_private_address() -> None:
    """The host is resolved once per hop; a later answer can never be dialled."""
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    calls: list[str] = []

    def rebinding(host: str, port: int) -> list[str]:
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
    with pytest.raises(f.FetchError, match="private, local or reserved"):
        f.fetch(
            "https://evil.example.com/",
            resolver=resolver({"evil.example.com": [address]}),
            connector=web.connector,
        )
    assert web.dialled == []


def test_any_private_answer_among_several_is_refused() -> None:
    web = FakeWeb({})
    with pytest.raises(f.FetchError):
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
    def no_dns(host: str, port: int) -> list[str]:
        raise AssertionError("a literal address needs no lookup")

    with pytest.raises(f.FetchError):
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
    with pytest.raises(f.FetchError, match="private"):
        f.fetch("https://jobs.example.com/a", resolver=resolve, connector=web.connector)
    assert resolve.calls == ["jobs.example.com", "cdn.example.net", "inside.example.org"]
    assert [d[2] for d in web.dialled] == [PUBLIC, "93.184.216.35"]  # never the private one


def test_a_redirect_to_another_scheme_is_refused() -> None:
    web = FakeWeb(
        {"https://jobs.example.com/a": FakeResponse(302, {"Location": "file:///etc/passwd"})}
    )
    with pytest.raises(f.FetchError, match="only http and https"):
        f.fetch(
            "https://jobs.example.com/a",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
        )


def test_too_many_redirects() -> None:
    web = FakeWeb(
        {"https://loop.example.com/": FakeResponse(302, {"Location": "https://loop.example.com/"})}
    )
    with pytest.raises(f.FetchError, match="more than 5 redirects"):
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
    with pytest.raises(f.FetchError, match=message):
        f.fetch(
            url, resolver=resolver({"jobs.example.com": [PUBLIC]}), connector=FakeWeb({}).connector
        )


def test_bodies_are_capped() -> None:
    big = FakeResponse(body=b"x" * 2048)
    web = FakeWeb({"https://jobs.example.com/big": big})
    with pytest.raises(f.FetchError, match="larger than"):
        f.fetch(
            "https://jobs.example.com/big",
            resolver=resolver({"jobs.example.com": [PUBLIC]}),
            connector=web.connector,
            max_bytes=1024,
        )


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
