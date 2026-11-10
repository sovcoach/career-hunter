"""The learning loop (#43): the operator's labels refine the scoring prompt, nothing else.

Synthetic postings and companies only; the model is a fake transport.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any

import pytest

from careerhunter import ledger, search_store
from careerhunter import watch as w
from careerhunter.config import learning_labelled_examples, validate_config
from careerhunter.filters import PostingText
from careerhunter.models import Application, Dating, Posting
from careerhunter.score import score_posting
from tests.test_config import example
from tests.test_score import RUBRIC, posting, reply
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

EXAMPLE_TITLE = "Synthetic Labelled Dispatcher"


def _posting(search: sqlite3.Connection, title: str) -> int:
    """A labelled posting. Self-sourced, so a watch run in the test never touches it."""
    with search:
        _, posting_id, _, _ = search_store.find_or_create_posting(
            search,
            Posting("Example Gadgets", title, DAY, source="self_sourced"),
            Dating("exact", day=DAY),
        )
        search.execute(
            "UPDATE postings SET gaps = ?, lane = 'operations', lane_checked = 1, "
            "embedding_score = 0.123456 WHERE id = ?",
            (json.dumps([{"name": "SENTINEL-gap"}, {"name": "x"}]), posting_id),
        )
    return posting_id


def _apply(search: sqlite3.Connection, title: str, *statuses: str) -> None:
    posting_id = _posting(search, title)
    found = search_store.posting_by_id(search, posting_id)
    assert found is not None
    recorded = ledger.record_application(
        search, None, Application(found, Dating("exact", day=DAY), "email", notes="SENTINEL-note")
    )
    for status in statuses:
        ledger.record_status(search, None, recorded.correlation_id, status, DAY)


def _skip(search: sqlite3.Connection, title: str, *, by_operator: bool = True) -> None:
    posting_id = _posting(search, title)
    with search:
        search_store.insert_skip(
            search, posting_id, DAY, "SENTINEL-reason", by_operator=by_operator
        )


def _stamp_order(search: sqlite3.Connection) -> None:
    """Give each label its own second, in the order the rows were written."""
    with search:
        search.execute(
            "UPDATE decision_history SET recorded_at = "
            "strftime('%Y-%m-%dT%H:%M:%SZ', '2026-10-01', '+' || id || ' minutes')"
        )
        search.execute(
            "UPDATE skips SET created_at = "
            "strftime('%Y-%m-%dT%H:%M:%SZ', '2026-10-01', '+' || (100 + id) || ' minutes')"
        )


def test_labels_come_only_from_the_operator(search: sqlite3.Connection) -> None:
    _apply(search, "Applied Role")
    _apply(search, "Rejected Role", "screening", "rejected")
    _skip(search, "Skipped Role")
    _skip(search, "Filtered Role", by_operator=False)  # a watcher skip is not a label
    _posting(search, "Unlabelled Role")
    _stamp_order(search)
    found = search_store.labelled_examples(search, 10)
    assert [(e.title, e.label) for e in found] == [
        ("Skipped Role", "skipped"),
        ("Rejected Role", "rejected"),
        ("Applied Role", "applied"),
    ]
    assert {(e.lane, e.gap_count) for e in found} == {("operations", 2)}
    assert [e.title for e in search_store.labelled_examples(search, 2)] == [
        "Skipped Role",
        "Rejected Role",
    ]
    assert search_store.labelled_examples(search, 0) == []


def _sent(
    examples: list[search_store.Example], scored: PostingText | None = None
) -> dict[str, Any]:
    payloads: list[bytes] = []

    def transport(address: str, payload: bytes) -> tuple[int, bytes]:
        payloads.append(payload)
        return reply('{"gaps": []}')

    score_posting(scored or posting(), RUBRIC, transport=transport, examples=examples)
    view: dict[str, Any] = json.loads(json.loads(payloads[0])["messages"][1]["content"])
    return view


def test_the_request_carries_title_lane_gap_count_and_label_only(
    search: sqlite3.Connection,
) -> None:
    _apply(search, EXAMPLE_TITLE, "screening")
    view = _sent(search_store.labelled_examples(search, 5))
    assert view["labelled_examples"] == [
        {"title": EXAMPLE_TITLE, "lane": "operations", "gaps": 2, "label": "screening"}
    ]
    text = json.dumps(view)
    for absent in ("Example Gadgets", "SENTINEL-note", "SENTINEL-gap", "0.123456"):
        assert absent not in text  # no company, notes, gap names or cosine


def test_the_posting_being_scored_is_not_its_own_example(search: sqlite3.Connection) -> None:
    _apply(search, "Operations Coordinator")
    examples = search_store.labelled_examples(search, 5)
    scored = PostingText("Example Gadgets", "Operations Coordinator", "Example City")
    assert "labelled_examples" not in _sent(examples, scored)
    assert "labelled_examples" in _sent(examples)  # another company: it is an example


def test_no_examples_means_no_key(search: sqlite3.Connection) -> None:
    assert "labelled_examples" not in _sent([])


def test_examples_appear_only_in_the_loopback_payload(search: sqlite3.Connection) -> None:
    """A watch run with examples sends them to the model; nothing it stores or logs has
    them, and the rules it was given are unchanged."""
    _apply(search, EXAMPLE_TITLE, "rejected")
    examples = search_store.labelled_examples(search, 5)
    rules = copy.deepcopy(RUBRIC)
    payloads: list[bytes] = []

    def transport(address: str, payload: bytes) -> tuple[int, bytes]:
        payloads.append(payload)
        return reply('{"gaps": [], "requirements": ["Forklift licence"]}')

    def scorer(item: PostingText) -> Any:
        return score_posting(item, rules, transport=transport, examples=examples)

    slug = "examplerobotics"
    lines = w.run(
        search,
        [company("Example Robotics", "greenhouse", slug)],
        today=DAY,
        fetcher=Scripted({url("greenhouse", slug): fetched(one_greenhouse(slug))}),
        config=rules,
        scorer=scorer,
    )
    assert any(EXAMPLE_TITLE in payload.decode() for payload in payloads)
    assert not any(EXAMPLE_TITLE in line for line in lines)
    row = search.execute(
        "SELECT llm_output, gaps, key_requirements, scout_metadata FROM postings "
        "WHERE title = 'Operations Coordinator'"
    ).fetchone()
    assert row["llm_output"] is not None
    assert not any(EXAMPLE_TITLE in str(value) for value in tuple(row))
    assert rules == RUBRIC  # rules and thresholds never change


def test_embeddings_stay_out_of_the_scoring_decision(search: sqlite3.Connection) -> None:
    """ADR-019 stands: a cosine is not in the request, and changing it changes nothing."""
    _apply(search, EXAMPLE_TITLE)
    first = _sent(search_store.labelled_examples(search, 5))
    with search:
        search.execute("UPDATE postings SET embedding_score = -0.987654")
    second = _sent(search_store.labelled_examples(search, 5))
    assert first == second
    assert "0.123456" not in json.dumps(first) and "-0.987654" not in json.dumps(second)


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("5", "learning must be a mapping"),
        ({"labelled_examples": -1}, "learning.labelled_examples must be an integer from 0 to 20"),
        ({"labelled_examples": 21}, "learning.labelled_examples"),
        ({"labelled_examples": True}, "learning.labelled_examples"),
        ({"labelled_example": 3}, "learning has unknown keys"),
    ],
)
def test_the_learning_section_is_checked(section: object, message: str) -> None:
    doc = example()
    doc["learning"] = section
    assert any(message in problem for problem in validate_config(doc))


def test_labelled_examples_default_to_five_and_zero_sends_none() -> None:
    doc = example()
    assert validate_config({**doc, "learning": {"labelled_examples": 0}}) == []
    assert learning_labelled_examples({}) == 5
    assert learning_labelled_examples({"learning": {"labelled_examples": 0}}) == 0
    assert learning_labelled_examples({"learning": {"labelled_examples": 20}}) == 20
