"""Local embeddings. A fake transport stands in for the model; no socket is opened."""

from __future__ import annotations

import copy
import http.client
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

import pytest

from careerhunter import db, loopback, search_store, watch
from careerhunter import embed as embedding
from careerhunter import resume as resume_store
from careerhunter.embed import (
    DEFAULT_URL,
    MODEL_NAME,
    EmbedError,
    cosine,
    embed_text,
    posting_text,
)
from careerhunter.filters import PostingText
from careerhunter.models import Posting
from careerhunter.score import Score
from careerhunter.storage_guard import DataDir
from tests.fakes import FakeClock, FakeServer, ManualWatchdog, Reply
from tests.test_score import BROKEN, RUBRIC, broken_connection
from tests.test_watch import DAY, Scripted, company, fetched, one_greenhouse, url

RESUME = "Synthetic resume for operations work."
DESC = "Synthetic posting for an operations coordinator."
RESUME_VEC = [1.0, 0.0]
POST_VEC = [0.0, 1.0]
SAME_VEC = [1.0, 0.0]


class EmbedScript:
    """Returns one canned vector per input text. An unknown input fails the test."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[str] = []

    def __call__(self, url: str, payload: bytes) -> tuple[int, bytes]:
        assert url == DEFAULT_URL
        body = json.loads(payload)
        assert body["model"] == MODEL_NAME
        text = body["input"]
        assert isinstance(text, str)
        self.calls.append(text)
        vector = self.vectors[text]
        return 200, json.dumps({"data": [{"embedding": vector}]}).encode()


def _config(resume: str | None, *, weight: float | None = None) -> dict[str, Any]:
    doc = copy.deepcopy(RUBRIC)
    doc["search"]["resume"] = resume
    if weight is not None:
        doc["fit_rubric"]["embedding_weight"] = weight
    return doc


def _write_resume(datadir: DataDir, search: sqlite3.Connection, text: str = RESUME) -> None:
    """Import `text` the way careerhunter resume import does."""
    resume_store.import_text(datadir.root, search, text, now=lambda: "2026-11-04T12:00:00Z")


def _current(datadir: DataDir) -> Path:
    return datadir.root / resume_store.RESUME_DIR / resume_store.CURRENT


def _kept(text: PostingText) -> Score:
    return Score(llm_output="model text", gaps="[]", tier="strong")


def _greenhouse_text() -> str:
    return posting_text(
        PostingText(
            company="Example Robotics",
            title="Operations Coordinator",
            location="Example City",
            work_model=None,
        )
    )


def _run(
    search: sqlite3.Connection,
    datadir: DataDir,
    transport: EmbedScript | Any,
    *,
    resume: str | None = resume_store.CONFIG_VALUE,
    weight: float | None = None,
    scorer: Any = _kept,
) -> list[str]:
    board = url("greenhouse", "examplerobotics")
    body = one_greenhouse("examplerobotics")
    return watch.run(
        search,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today=DAY,
        fetcher=Scripted({board: fetched(body)}),
        config=_config(resume, weight=weight),
        scorer=scorer,
        data_dir=datadir.root,
        embed_transport=transport,
    )


def _score(search: sqlite3.Connection) -> float | None:
    value = search.execute("SELECT embedding_score FROM postings").fetchone()["embedding_score"]
    if value is None:
        return None
    return float(value)


def _absent(text: str) -> None:
    assert RESUME not in text
    assert DESC not in text
    for vector in (RESUME_VEC, POST_VEC, SAME_VEC):
        assert struct.pack(f"<{len(vector)}f", *vector).hex() not in text


def test_a_kept_posting_stores_the_cosine_beside_the_tier(
    search: sqlite3.Connection, datadir: DataDir, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_resume(datadir, search)
    transport = EmbedScript({RESUME: SAME_VEC, _greenhouse_text(): SAME_VEC})
    lines = _run(search, datadir, transport, weight=0)
    assert transport.calls == [RESUME, _greenhouse_text()]
    row = search.execute(
        "SELECT fit_rating, llm_output, scout_metadata, embedding_score FROM postings"
    ).fetchone()
    assert row["fit_rating"] is None
    assert row["llm_output"] == "model text"
    assert row["scout_metadata"] == '{"tier": "strong"}'
    assert row["embedding_score"] == pytest.approx(1.0)
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0
    assert not any(line.startswith("embedding miss") for line in lines)
    joined = "\n".join(lines)
    _absent(joined)
    captured = capsys.readouterr()
    _absent(captured.out)
    _absent(captured.err)


def test_a_down_server_leaves_the_cosine_null_and_the_run_finishes(
    search: sqlite3.Connection, datadir: DataDir, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_resume(datadir, search)
    calls: list[str] = []

    def down(url: str, payload: bytes) -> tuple[int, bytes]:
        calls.append(json.loads(payload)["input"])
        raise OSError("synthetic")

    board = url("greenhouse", "examplerobotics")
    other = url("lever", "samplelogistics")
    lines = watch.run(
        search,
        [
            company("Example Robotics", "greenhouse", "examplerobotics"),
            company("Sample Logistics", "lever", "samplelogistics"),
        ],
        today=DAY,
        fetcher=Scripted(
            {
                board: fetched(one_greenhouse("examplerobotics")),
                other: fetched(
                    [
                        {
                            "id": "abc-123",
                            "text": "Operations Coordinator",
                            "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
                            "categories": {"location": "Canada"},
                            "workplaceType": "remote",
                            "descriptionPlain": DESC,
                        }
                    ]
                ),
            }
        ),
        config=_config(resume_store.CONFIG_VALUE),
        scorer=_kept,
        data_dir=datadir.root,
        embed_transport=down,
    )
    ids = [int(row["id"]) for row in search.execute("SELECT id FROM postings ORDER BY id")]
    misses = [line for line in lines if line.startswith("embedding miss")]
    assert misses == [f"embedding miss posting {posting_id}: unreachable" for posting_id in ids]
    assert calls == [RESUME]
    rows = search.execute("SELECT llm_output FROM postings").fetchall()
    assert all(row["llm_output"] == "model text" for row in rows)
    assert all(
        row["embedding_score"] is None
        for row in search.execute("SELECT embedding_score FROM postings")
    )
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0
    joined = "\n".join(lines)
    _absent(joined)
    captured = capsys.readouterr()
    _absent(captured.out)
    _absent(captured.err)


def test_a_missing_resume_file_is_a_miss_and_does_not_call_the_model(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    transport = EmbedScript({})
    lines = _run(search, datadir, transport)
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    assert f"embedding miss posting {posting_id}: no-resume" in lines
    assert transport.calls == []
    assert _score(search) is None
    stored = search.execute("SELECT llm_output FROM postings").fetchone()
    assert stored is not None
    assert stored["llm_output"] == "model text"


@pytest.mark.parametrize(
    "resume",
    ["../outside.txt", "/Volumes/ExampleDrive/ExampleFolder/notes.txt"],
)
def test_a_resume_path_outside_the_data_folder_is_not_read(
    search: sqlite3.Connection, datadir: DataDir, resume: str
) -> None:
    outside = datadir.root.parent / "outside.txt"
    outside.write_text(RESUME, encoding="utf-8")
    transport = EmbedScript({RESUME: RESUME_VEC})
    lines = _run(search, datadir, transport, resume=resume)
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    assert f"embedding miss posting {posting_id}: no-resume" in lines
    assert transport.calls == []
    assert RESUME not in "\n".join(lines)


def test_a_dimension_mismatch_leaves_the_cosine_null(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    transport = EmbedScript({RESUME: RESUME_VEC, _greenhouse_text(): [1.0, 0.0, 0.0]})
    lines = _run(search, datadir, transport)
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    assert f"embedding miss posting {posting_id}: dim-mismatch" in lines
    assert _score(search) is None
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0
    stored = search.execute("SELECT dim FROM posting_embeddings").fetchone()
    assert stored["dim"] == 3


def test_a_negative_cosine_does_not_skip(search: sqlite3.Connection, datadir: DataDir) -> None:
    _write_resume(datadir, search)
    transport = EmbedScript({RESUME: RESUME_VEC, _greenhouse_text(): [-1.0, 0.0]})
    lines = _run(search, datadir, transport)
    assert _score(search) == pytest.approx(-1.0)
    assert search.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 0
    assert not any("skipped" in line for line in lines)


def test_a_gap_tier_skip_still_happens_when_the_cosine_is_high(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    transport = EmbedScript({RESUME: SAME_VEC, _greenhouse_text(): SAME_VEC})

    def excluded(text: PostingText) -> Score:
        return Score(llm_output="raw", gaps="[]", tier=None, excluded=True)

    lines = _run(search, datadir, transport, scorer=excluded)
    assert any("skipped (gap_tier)" in line for line in lines)
    assert search.execute("SELECT reason FROM skips").fetchone()["reason"] == "gap_tier"
    assert _score(search) == pytest.approx(1.0)


def test_a_changed_resume_reuses_stored_posting_vectors(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    first = EmbedScript({RESUME: SAME_VEC, _greenhouse_text(): POST_VEC})
    _run(search, datadir, first)
    blob, embedded_at = search.execute(
        "SELECT vector, embedded_at FROM posting_embeddings"
    ).fetchone()
    assert isinstance(blob, bytes)
    second_text = "Synthetic resume after a local edit."
    _write_resume(datadir, search, second_text)
    second = EmbedScript({second_text: POST_VEC})
    lines = _run(search, datadir, second)
    assert second.calls == [second_text]
    again = search.execute("SELECT vector, embedded_at FROM posting_embeddings").fetchone()
    assert again["vector"] == blob
    assert again["embedded_at"] == embedded_at
    assert _score(search) == pytest.approx(1.0)
    joined = "\n".join(lines)
    assert RESUME not in joined
    assert second_text not in joined
    assert not any(line.startswith("embedding miss") for line in lines)


def test_a_later_resume_miss_is_logged_once_for_an_unscored_posting(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    transport = EmbedScript({RESUME: SAME_VEC, _greenhouse_text(): POST_VEC})

    def silent(text: PostingText) -> None:
        return None

    _run(search, datadir, transport, scorer=silent)
    assert _score(search) == pytest.approx(0.0)
    _current(datadir).unlink()
    lines = _run(search, datadir, EmbedScript({}), scorer=silent)
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    misses = [line for line in lines if line.startswith("embedding miss")]
    assert misses == [f"embedding miss posting {posting_id}: no-resume"]
    assert _score(search) is None
    assert RESUME not in "\n".join(lines)


def test_an_unchanged_resume_does_not_call_the_model_again(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    transport = EmbedScript({RESUME: SAME_VEC, _greenhouse_text(): POST_VEC})
    _run(search, datadir, transport)
    assert len(transport.calls) == 2
    _run(search, datadir, transport)
    assert len(transport.calls) == 2
    assert _score(search) == pytest.approx(0.0)


def test_the_description_is_embedded_once_and_stays_out_of_the_log(
    search: sqlite3.Connection, datadir: DataDir, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_resume(datadir, search)
    seen = posting_text(
        PostingText(
            company="Sample Logistics",
            title="Operations Coordinator",
            location="Canada",
            work_model="remote",
            description=DESC,
        )
    )
    later = posting_text(
        PostingText(
            company="Sample Logistics",
            title="Operations Coordinator",
            location="Canada",
            work_model="remote",
        )
    )
    transport = EmbedScript({RESUME: RESUME_VEC, seen: POST_VEC, later: POST_VEC})
    board = url("lever", "samplelogistics")
    payload = [
        {
            "id": "abc-123",
            "text": "Operations Coordinator",
            "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
            "categories": {"location": "Canada"},
            "workplaceType": "remote",
            "descriptionPlain": DESC,
        }
    ]
    lines = watch.run(
        search,
        [company("Sample Logistics", "lever", "samplelogistics")],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config=_config(resume_store.CONFIG_VALUE),
        scorer=_kept,
        data_dir=datadir.root,
        embed_transport=transport,
    )
    assert seen in transport.calls
    assert DESC in seen
    _absent("\n".join(lines))
    _absent(capsys.readouterr().out)
    search.execute("DELETE FROM posting_embeddings")
    search.execute("UPDATE postings SET llm_output = NULL")
    again = watch.run(
        search,
        [company("Sample Logistics", "lever", "samplelogistics")],
        today=DAY,
        fetcher=Scripted({board: fetched(payload)}),
        config=_config(resume_store.CONFIG_VALUE),
        scorer=_kept,
        data_dir=datadir.root,
        embed_transport=transport,
    )
    assert transport.calls[-1] == later
    assert DESC not in later
    _absent("\n".join(again))


def test_null_resume_skips_embeddings(search: sqlite3.Connection, datadir: DataDir) -> None:
    transport = EmbedScript({})
    lines = _run(search, datadir, transport, resume=None)
    assert transport.calls == []
    assert not any(line.startswith("embedding miss") for line in lines)
    assert _score(search) is None


def test_an_emptied_resume_is_a_miss(search: sqlite3.Connection, datadir: DataDir) -> None:
    """Emptied after its import, so it no longer matches its recorded hash."""
    _write_resume(datadir, search)
    _current(datadir).write_bytes(b"")
    transport = EmbedScript({})
    lines = _run(search, datadir, transport)
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    assert f"embedding miss posting {posting_id}: no-resume" in lines
    assert transport.calls == []


def test_a_non_text_resume_is_a_miss(search: sqlite3.Connection, datadir: DataDir) -> None:
    _write_resume(datadir, search)
    _current(datadir).write_bytes(b"\xff")
    transport = EmbedScript({})
    lines = _run(search, datadir, transport)
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    assert f"embedding miss posting {posting_id}: no-resume" in lines
    assert transport.calls == []


def test_a_zero_vector_is_unreachable(search: sqlite3.Connection, datadir: DataDir) -> None:
    _write_resume(datadir, search)
    transport = EmbedScript({RESUME: [0.0, 0.0]})
    lines = _run(search, datadir, transport)
    posting_id = int(search.execute("SELECT id FROM postings").fetchone()["id"])
    assert f"embedding miss posting {posting_id}: unreachable" in lines
    assert search.execute("SELECT count(*) AS n FROM resume_embedding").fetchone()["n"] == 0
    assert RESUME not in "\n".join(lines)


def test_embed_text_refuses_anything_but_loopback() -> None:
    def boom(url: str, payload: bytes) -> tuple[int, bytes]:
        raise AssertionError(url)

    with pytest.raises(EmbedError) as exc:
        embed_text(RESUME, url="http://example.com/v1/embeddings", transport=boom)
    assert str(exc.value) == "unreachable"
    assert "example.com" not in str(exc.value)
    assert RESUME not in str(exc.value)


def test_a_redirect_and_a_bad_body_are_unreachable() -> None:
    with pytest.raises(EmbedError) as redirect:
        embed_text(RESUME, transport=lambda url, payload: (302, b"go away"))
    assert str(redirect.value) == "unreachable"
    assert RESUME not in str(redirect.value)
    with pytest.raises(EmbedError) as bad:
        embed_text(RESUME, transport=lambda url, payload: (200, b"not-json"))
    assert str(bad.value) == "unreachable"
    assert RESUME not in str(bad.value)


def test_the_default_transport_posts_once_and_does_not_follow_a_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(Reply(302, b"go away"))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    with pytest.raises(EmbedError) as exc:
        embed_text(RESUME)
    assert str(exc.value) == "unreachable"
    assert [(host, address, port) for host, address, port, _t in server.opened] == [
        ("127.0.0.1", "127.0.0.1", 1234)
    ]
    assert [(method, path) for method, path, _b, _h in server.requests] == [
        ("POST", "/v1/embeddings")
    ]
    assert server.closes == 1
    assert RESUME not in str(exc.value)


def test_embedding_tables_hold_vectors_not_text(search: sqlite3.Connection) -> None:
    resume_cols = [row["name"] for row in search.execute("PRAGMA table_info(resume_embedding)")]
    posting_embed_cols = [
        row["name"] for row in search.execute("PRAGMA table_info(posting_embeddings)")
    ]
    posting_cols = [row["name"] for row in search.execute("PRAGMA table_info(postings)")]
    assert resume_cols == ["id", "content_sha256", "dim", "vector", "embedded_at"]
    assert posting_embed_cols == ["posting_id", "dim", "vector", "embedded_at"]
    assert "embedding_score" in posting_cols
    assert "description" not in posting_cols
    assert [item.version for item in db.load_migrations("ei_record")] == [1]
    company_id, _ = search_store.upsert_company(search, "Example Org", source="watcher")
    posting_id = search_store.create_posting(
        search,
        company_id,
        Posting(
            company="Example Org",
            title="Operations Coordinator",
            first_seen=DAY,
            source="watcher",
        ),
    )
    search.execute("UPDATE postings SET embedding_score = -1 WHERE id = ?", (posting_id,))
    search.execute("UPDATE postings SET embedding_score = 1 WHERE id = ?", (posting_id,))
    with pytest.raises(sqlite3.IntegrityError):
        search.execute("UPDATE postings SET embedding_score = 1.1 WHERE id = ?", (posting_id,))
    stamp = "2026-11-02T00:00:00Z"
    sha = "ab" * 32
    with pytest.raises(sqlite3.IntegrityError):
        search.execute(
            "INSERT INTO resume_embedding (id, content_sha256, dim, vector, embedded_at) "
            "VALUES (2, ?, 1, ?, ?)",
            (sha, struct.pack("<1f", 1.0), stamp),
        )
    with pytest.raises(sqlite3.IntegrityError):
        search.execute(
            "INSERT INTO resume_embedding (id, content_sha256, dim, vector, embedded_at) "
            "VALUES (1, ?, 1, ?, ?)",
            (sha, b"short", stamp),
        )


def test_cosine_of_known_vectors() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    with pytest.raises(EmbedError):
        cosine([1.0], [1.0, 0.0])
    with pytest.raises(EmbedError):
        cosine([0.0, 0.0], [0.0, 0.0])


@pytest.mark.parametrize("error", BROKEN, ids=lambda e: type(e).__name__)
def test_a_broken_reply_from_the_default_transport_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(loopback, "LoopbackConnection", broken_connection(error))
    with pytest.raises(EmbedError) as exc:
        embed_text(RESUME)
    assert str(exc.value) == "unreachable"
    assert RESUME not in str(exc.value)


def test_an_injected_transport_that_breaks_is_unreachable() -> None:
    def broken(url: str, payload: bytes) -> tuple[int, bytes]:
        raise http.client.RemoteDisconnected("synthetic")

    with pytest.raises(EmbedError) as exc:
        embed_text(RESUME, transport=broken)
    assert str(exc.value) == "unreachable"
    assert RESUME not in str(exc.value)


def _vector(value: object) -> bytes:
    return json.dumps({"data": [{"embedding": value}]}).encode()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(json.dumps({"error": "model not loaded"}).encode(), id="error-object"),
        pytest.param(json.dumps({"data": []}).encode(), id="no-data"),
        pytest.param(json.dumps({"data": ["x"]}).encode(), id="data-not-object"),
        pytest.param(json.dumps({"data": [{}]}).encode(), id="no-vector"),
        pytest.param(_vector([]), id="empty-vector"),
        pytest.param(_vector("x"), id="vector-not-list"),
        pytest.param(_vector([1, "a"]), id="non-numeric"),
        pytest.param(_vector([True, 1.0]), id="boolean"),
        pytest.param(b'{"data": [{"embedding": [1e400, 1.0]}]}', id="infinite"),
    ],
)
def test_a_reply_of_the_wrong_shape_is_unreachable(body: bytes) -> None:
    with pytest.raises(EmbedError) as exc:
        embed_text(RESUME, transport=lambda url, payload: (200, body))
    assert str(exc.value) == "unreachable"
    assert RESUME not in str(exc.value)


def test_a_bug_in_our_own_parser_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def bug(status: int, body: bytes) -> list[float]:
        raise RuntimeError("synthetic bug")

    monkeypatch.setattr(embedding, "_values", bug)
    with pytest.raises(RuntimeError, match="synthetic bug"):
        embed_text(RESUME, transport=lambda url, payload: (200, _vector([1.0, 0.0])))


def test_a_1536_dimension_reply_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    vector = [round(-0.0123456789 + i * 1e-7, 10) for i in range(1536)]
    body = json.dumps({"data": [{"embedding": vector}]}).encode()
    assert len(body) > 20_000  # larger than the scoring cap this client used to share
    monkeypatch.setattr(loopback, "LoopbackConnection", FakeServer(Reply(body=body)))
    assert len(embed_text(RESUME)) == 1536


def test_a_reply_over_the_embedding_cap_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"x" * (embedding.MAX_REPLY + 1)
    server = FakeServer(Reply(body=body, step=65536))
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    with pytest.raises(EmbedError) as exc:
        embed_text(RESUME)
    assert str(exc.value) == "unreachable"
    assert server.reads == 17  # 16 full chunks, then the one that passes the cap


def test_embedding_has_its_own_60_second_deadline(
    monkeypatch: pytest.MonkeyPatch, loopback_watchdog: ManualWatchdog
) -> None:
    clock = FakeClock()
    server = FakeServer(
        Reply(body=b'{"data": [{"embedding": [1.0, 0.0]}]}', clock=clock, header_cost=61.0)
    )
    monkeypatch.setattr(loopback, "_clock", clock)
    monkeypatch.setattr(loopback, "LoopbackConnection", server)
    with pytest.raises(EmbedError) as exc:
        embed_text(RESUME)
    assert str(exc.value) == "unreachable"
    assert [a.seconds for a in loopback_watchdog.armed] == [60.0]
    assert [timeout for *_rest, timeout in server.opened] == [30.0]
    assert RESUME not in str(exc.value)


# A server that is down stops embedding for the run; a bad reply is one posting's miss.


def _two_postings(search: sqlite3.Connection, datadir: DataDir, transport: Any) -> list[str]:
    slug = "examplerobotics"
    jobs = [
        one_greenhouse(slug, title, ident)["jobs"][0]  # type: ignore[index]
        for title, ident in (("Operations Coordinator", 1001), ("Support Specialist", 1002))
    ]
    lines = watch.run(
        search,
        [company("Example Robotics", "greenhouse", slug)],
        today=DAY,
        fetcher=Scripted({url("greenhouse", slug): fetched({"jobs": jobs})}),
        config=_config(resume_store.CONFIG_VALUE),
        scorer=_kept,
        data_dir=datadir.root,
        embed_transport=transport,
    )
    return lines


class ByText:
    """Answers by input text: a vector, a raw body, or an exception to raise."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, address: str, payload: bytes) -> tuple[int, bytes]:
        text = json.loads(payload)["input"]
        self.calls.append(text)
        answer = self.answers[text]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, bytes):
            return 200, answer
        return 200, json.dumps({"data": [{"embedding": answer}]}).encode()


def _misses(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("embedding miss")]


def _scores(search: sqlite3.Connection) -> dict[str, float | None]:
    return {
        str(row["title"]): row["embedding_score"]
        for row in search.execute("SELECT title, embedding_score FROM postings ORDER BY id")
    }


def test_a_wrong_shape_reply_is_one_postings_miss_and_the_next_is_embedded(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    first, second = (
        posting_text(PostingText("Example Robotics", t, "Example City", work_model=None))
        for t in ("Operations Coordinator", "Support Specialist")
    )
    transport = ByText({RESUME: SAME_VEC, first: b'{"data": []}', second: SAME_VEC})
    lines = _two_postings(search, datadir, transport)
    assert transport.calls == [RESUME, first, second]
    scores = _scores(search)
    assert scores["Operations Coordinator"] is None
    assert scores["Support Specialist"] == pytest.approx(1.0)  # refresh did not clear it
    assert len(_misses(lines)) == 1
    assert _misses(lines)[0].endswith(": unreachable")  # the log text is unchanged


def test_a_refused_connection_stops_embedding_for_the_rest_of_the_run(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    first, second = (
        posting_text(PostingText("Example Robotics", t, "Example City", work_model=None))
        for t in ("Operations Coordinator", "Support Specialist")
    )
    transport = ByText(
        {RESUME: SAME_VEC, first: ConnectionRefusedError("synthetic"), second: SAME_VEC}
    )
    lines = _two_postings(search, datadir, transport)
    assert transport.calls == [RESUME, first]  # no call for the second posting or refresh
    assert set(_scores(search).values()) == {None}
    misses = _misses(lines)
    assert len(misses) == len(set(misses)) == 2  # one line per posting, never repeated


def test_a_resume_the_server_cannot_embed_is_asked_once_per_run(
    search: sqlite3.Connection, datadir: DataDir
) -> None:
    _write_resume(datadir, search)
    transport = ByText({RESUME: ConnectionRefusedError("synthetic")})
    lines = _two_postings(search, datadir, transport)
    assert transport.calls == [RESUME]  # the refresh reused the failure
    misses = _misses(lines)
    assert len(misses) == len(set(misses)) == 2
