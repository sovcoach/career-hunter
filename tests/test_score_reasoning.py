"""A reasoning model's thinking is stripped before the reply is parsed or stored."""

from __future__ import annotations

import json

import pytest

from careerhunter import loopback
from careerhunter import score as scoring
from careerhunter.score import score_posting, strip_reasoning
from tests.fakes import FakeServer, Reply
from tests.test_score import RUBRIC, posting, reply

ANSWER = '{"gaps":[],"requirements":["Synthetic scheduling tool"]}'
THINKING = 'The candidate lacks nothing obvious. {"gaps": [{"name": "draft"}]} Let me answer.'


def _scored(content: str) -> scoring.Score | None:
    return score_posting(posting(), RUBRIC, transport=lambda _a, _p: reply(content))


@pytest.mark.parametrize(
    "content",
    [
        f"<think>{THINKING}</think>{ANSWER}",
        f"<think>\n{THINKING}\n</think>\n\n{ANSWER}\n",
        f"<THINK>{THINKING}</THINK>{ANSWER}",
        f"<thinking>{THINKING}</thinking>{ANSWER}",
        f"<reasoning>{THINKING}</reasoning>{ANSWER}",
        f"<think>one</think><think>two</think>{ANSWER}",
        f"{THINKING}</think>{ANSWER}",  # the template opened the tag itself
        f"<think>{THINKING}</think >{ANSWER}",
    ],
)
def test_thinking_is_stripped_before_parsing(content: str) -> None:
    result = _scored(content)
    assert result is not None
    assert result.gaps == "[]" and result.tier is not None
    assert result.llm_output == ANSWER  # the stored text is the answer alone
    assert "think" not in result.llm_output.casefold()
    assert result.key_requirements == ("Synthetic scheduling tool",)


@pytest.mark.parametrize(
    "content",
    [
        f"<think>{THINKING}",  # cut off while thinking
        f"<think>{THINKING}</think>",  # thinking and no answer
        f"<think>{THINKING}</think>   \n",
        f"<think>one</think><think>{THINKING}{ANSWER}",
    ],
)
def test_a_reply_whose_thinking_never_ends_does_not_parse(content: str) -> None:
    assert _scored(content) is None


def test_reasoning_content_is_never_read() -> None:
    body = {"choices": [{"message": {"content": "", "reasoning_content": ANSWER}}]}
    result = score_posting(
        posting(), RUBRIC, transport=lambda _a, _p: (200, json.dumps(body).encode())
    )
    assert result is None
    body = {"choices": [{"message": {"content": ANSWER, "reasoning_content": "<think>x"}}]}
    result = score_posting(
        posting(), RUBRIC, transport=lambda _a, _p: (200, json.dumps(body).encode())
    )
    assert result is not None and result.llm_output == ANSWER


def test_a_reply_without_thinking_is_unchanged() -> None:
    assert strip_reasoning(f"  {ANSWER}\n") == ANSWER
    assert strip_reasoning("   ") is None


def test_a_long_thinking_reply_fits_under_the_reply_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    long_thinking = "x" * 100_000  # far past what is stored, well under MAX_REPLY
    status, body = reply(f"<think>{long_thinking}</think>{ANSWER}")
    server = FakeServer(Reply(status, body))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    result = score_posting(posting(), RUBRIC)
    assert result is not None and result.llm_output == ANSWER
    assert len(body) > scoring.MAX_OUTPUT and len(body) < scoring.MAX_REPLY


def test_the_stored_text_is_capped_after_stripping() -> None:
    padded = ANSWER + " " * 30_000 + "trailing"
    result = _scored(f"<think>{THINKING}</think>{padded}")
    assert result is not None and len(result.llm_output) == scoring.MAX_OUTPUT
