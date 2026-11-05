"""The test suite never reaches the network (conftest.no_network)."""

from __future__ import annotations

import socket

import pytest

from careerhunter import fetch
from tests.conftest import RealNetworkBlocked


def test_lookups_and_connections_are_blocked_and_recorded(no_network: list[str]) -> None:
    with pytest.raises(RealNetworkBlocked, match="getaddrinfo"):
        socket.getaddrinfo("boards.example.com", 443)
    with pytest.raises(RealNetworkBlocked, match="create_connection"):
        socket.create_connection(("93.184.216.34", 443), timeout=1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RealNetworkBlocked, match=r"93\.184\.216\.34"):
            sock.connect(("93.184.216.34", 443))
        with pytest.raises(RealNetworkBlocked):
            sock.connect_ex(("127.0.0.1", 9))  # loopback too: fakes stand in for servers
    finally:
        sock.close()
    assert len(no_network) == 4
    no_network.clear()  # a recorded attempt would fail this test at teardown


def test_a_client_that_swallows_the_error_still_leaves_a_record(no_network: list[str]) -> None:
    """The bounded resolver catches every exception in its lookup thread."""
    with pytest.raises(fetch.FetchFailed):
        fetch.fetch("https://boards.example.com/jobs")
    assert no_network == ["getaddrinfo ('boards.example.com',)"]
    no_network.clear()


def test_the_block_is_not_mistaken_for_a_network_failure() -> None:
    assert not issubclass(RealNetworkBlocked, OSError)


@pytest.mark.allow_network
def test_the_marker_opts_out(no_network: list[str]) -> None:
    assert socket.getaddrinfo.__module__ == "socket"  # the real function, not the guard
    assert no_network == []
