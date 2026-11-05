"""The board client: one byte budget per board, and typed errors for bad replies."""

from __future__ import annotations

import pytest

from careerhunter import ats
from careerhunter.fetch import Fetched, FetchFailed, FetchTooLarge


def reply(body: bytes, status: int = 200, retry_after: str | None = None) -> Fetched:
    return Fetched("https://boards.example.com/x", status, "application/json", body, retry_after)


class Pages:
    """A fetcher that hands out one reply per call, in order."""

    def __init__(self, *replies: Fetched | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def __call__(self, url: str) -> Fetched:
        self.calls.append(url)
        found = self.replies.pop(0)
        if isinstance(found, Exception):
            raise found
        return found


def test_every_reply_of_a_board_counts_against_one_budget() -> None:
    client = ats.Client(Pages(reply(b"[1, 1, 1]"), reply(b"[2, 2, 2]")), max_bytes=16)
    assert client.get_json("https://boards.example.com/a") == [1, 1, 1]
    with pytest.raises(FetchTooLarge, match="board is larger"):
        client.get_json("https://boards.example.com/b")


def test_a_board_at_exactly_the_budget_is_read() -> None:
    client = ats.Client(Pages(reply(b"[1]"), reply(b"[2]")), max_bytes=6)
    assert client.get_json("https://boards.example.com/a") == [1]
    assert client.get_json("https://boards.example.com/b") == [2]
    assert client.used == 6


def test_a_non_200_reply_names_its_status_and_url_and_is_not_counted() -> None:
    client = ats.Client(Pages(reply(b"slow down", 429, "60")))
    with pytest.raises(ats.BoardHTTPError) as caught:
        client.get_json("https://api.example.com/list")
    assert caught.value.status == 429
    assert caught.value.url == "https://api.example.com/list"
    assert caught.value.retry_after == "60"
    assert client.used == 0


def test_a_body_that_is_not_json_is_a_parse_error() -> None:
    with pytest.raises(ats.BoardParseError, match="invalid JSON"):
        ats.Client(Pages(reply(b"<html>"))).get_json("https://boards.example.com/a")


def test_a_transport_failure_passes_through_unchanged() -> None:
    client = ats.Client(Pages(FetchFailed("couldn't fetch boards.example.com: OSError")))
    with pytest.raises(FetchFailed):
        client.get_json("https://boards.example.com/a")


def test_read_refuses_a_board_it_cannot_build_a_url_for() -> None:
    with pytest.raises(ats.BoardParseError):
        ats.read("greenhouse", "two/segments", ats.Client(Pages()))
