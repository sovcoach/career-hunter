"""scoring.chat_model and scoring.embedding_model carry the model ids; the URLs stay constant."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from careerhunter import embed, score
from careerhunter.config import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBEDDING_MODEL,
    chat_model,
    embedding_model,
    validate_config,
)
from careerhunter.filters import PostingText
from tests.test_score import RUBRIC

ROOT = Path(__file__).parents[1]


def _example() -> dict[str, Any]:
    doc = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert isinstance(doc, dict)
    return doc


def _posting() -> PostingText:
    return PostingText(
        company="Example Robotics",
        title="Operations Coordinator",
        location="Example City",
        work_model=None,
        description="A synthetic posting.",
    )


def test_the_template_leaves_both_ids_unset_and_valid() -> None:
    doc = _example()
    assert doc["scoring"] == {"chat_model": None, "embedding_model": None}
    assert validate_config(doc) == []
    assert chat_model(doc) == DEFAULT_CHAT_MODEL == "local-model"
    assert embedding_model(doc) == DEFAULT_EMBEDDING_MODEL == "local-embedding"


@pytest.mark.parametrize("section", [None, {}, {"chat_model": None}])
def test_missing_ids_use_the_defaults(section: object) -> None:
    doc = _example()
    doc["scoring"] = section
    assert validate_config(doc) == []
    assert chat_model(doc) == "local-model"
    assert embedding_model(doc) == "local-embedding"
    doc.pop("scoring")
    assert validate_config(doc) == []
    assert chat_model(doc) == "local-model"


def test_set_ids_are_read() -> None:
    doc = _example()
    doc["scoring"] = {
        "chat_model": "example-org/example-chat-8b",
        "embedding_model": "example-embed v1.5",
    }
    assert validate_config(doc) == []
    assert chat_model(doc) == "example-org/example-chat-8b"
    assert embedding_model(doc) == "example-embed v1.5"


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("example-chat", "scoring must be a mapping"),
        ({"chat_model": ""}, "scoring.chat_model must be a model identifier"),
        ({"chat_model": 7}, "scoring.chat_model must be a model identifier"),
        ({"chat_model": ["a"]}, "scoring.chat_model must be a model identifier"),
        ({"embedding_model": " padded"}, "scoring.embedding_model must be a model identifier"),
        ({"embedding_model": "padded "}, "scoring.embedding_model must be a model identifier"),
        ({"embedding_model": "x" * 201}, "scoring.embedding_model must be a model identifier"),
        ({"chat_model": "line\nbreak"}, "scoring.chat_model must be a model identifier"),
        ({"chat_model": "tab\there"}, "scoring.chat_model must be a model identifier"),
        ({"url": "http://127.0.0.1:1234/v1/chat/completions"}, "scoring has unknown keys"),
        ({"chat_url": "http://127.0.0.1:9/"}, "scoring has unknown keys"),
    ],
)
def test_bad_scoring_sections_are_refused(section: object, message: str) -> None:
    doc = _example()
    doc["scoring"] = section
    assert any(message in problem for problem in validate_config(doc))


def test_an_id_of_exactly_200_characters_is_accepted() -> None:
    doc = _example()
    doc["scoring"] = {"chat_model": "m" * 200}
    assert validate_config(doc) == []


def test_an_invalid_id_never_reaches_a_request() -> None:
    """Accessors on an unvalidated document fall back to the default."""
    assert chat_model({"scoring": {"chat_model": "bad\nid"}}) == "local-model"
    assert embedding_model({"scoring": {"embedding_model": ""}}) == "local-embedding"
    assert chat_model({"scoring": "x"}) == "local-model"
    assert chat_model(None) == "local-model"


def test_the_scorer_sends_the_configured_chat_model_to_the_constant_url() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen.append((url, json.loads(payload)))
        reply = {"choices": [{"message": {"content": '{"gaps":[]}'}}]}
        return 200, json.dumps(reply).encode()

    doc = copy.deepcopy(RUBRIC)
    doc["scoring"] = {"chat_model": "example-chat-model"}
    assert score.score_posting(_posting(), doc, transport=transport) is not None
    assert score.score_posting(_posting(), RUBRIC, transport=transport) is not None
    assert [url for url, _ in seen] == [score.DEFAULT_URL, score.DEFAULT_URL]
    assert seen[0][1]["model"] == "example-chat-model"
    assert seen[1][1]["model"] == "local-model"
    assert "scoring" not in json.dumps(seen[0][1]["messages"])


def test_the_embedder_sends_the_configured_embedding_model() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        seen.append((url, json.loads(payload)))
        return 200, json.dumps({"data": [{"embedding": [1.0, 0.0]}]}).encode()

    assert embed.embed_text("synthetic", transport=transport) == [1.0, 0.0]
    assert embed.embed_text("synthetic", transport=transport, model="example-embed") == [1.0, 0.0]
    assert seen[0] == (embed.DEFAULT_URL, {"model": "local-embedding", "input": "synthetic"})
    assert seen[1] == (embed.DEFAULT_URL, {"model": "example-embed", "input": "synthetic"})


def test_the_embedder_reads_its_model_from_config(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with_id = embed.Embedder(conn, tmp_path, {"scoring": {"embedding_model": "example-embed"}})
        without = embed.Embedder(conn, tmp_path, {})
    finally:
        conn.close()
    assert with_id.model == "example-embed"
    assert without.model == "local-embedding"
