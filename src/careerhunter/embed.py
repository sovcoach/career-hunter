"""Embed a resume and one posting on a local OpenAI-compatible server.

The client is not `fetch.py`. That client refuses loopback, which is right for a job
ad and wrong for a model on this Mac. This client allows only `http` to `127.0.0.1`,
`localhost` or `::1`, follows no redirects, and refuses any other host. The URL and
the model id are constants, not config fields.

A cosine is stored beside the LLM tier. It does not cause a skip, and
`fit_rubric.embedding_weight` is not read. A missing resume file, a down server, or a
different dimension leaves that posting's cosine null. The watch log records the
posting id and a reason, and nothing else.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import math
import sqlite3
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from careerhunter import db, loopback, search_store
from careerhunter.deadline import Deadline
from careerhunter.filters import PostingText
from careerhunter.score import ScoreError, Transport, require_loopback

DEFAULT_URL = "http://127.0.0.1:1234/v1/embeddings"
MODEL_NAME = "local-embedding"
TOTAL = 60.0
TIMEOUT = 30.0
# A 1536-dimension vector is about 21 KB of JSON; the cap leaves room for larger models.
MAX_REPLY = 1_048_576
_REASONS = frozenset({"unreachable", "no-resume", "dim-mismatch"})


class EmbedError(Exception):
    """A closed reason: unreachable, no-resume, or dim-mismatch. The text is that reason."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASONS:
            reason = "unreachable"
        self.reason = reason
        super().__init__(reason)


def miss_line(posting_id: int, reason: str) -> str:
    """The only watch line for a failed embedding. No resume text, posting text, or vector."""
    if reason not in _REASONS:
        reason = "unreachable"
    return f"embedding miss posting {posting_id}: {reason}"


def posting_text(posting: PostingText) -> str:
    """The in-memory posting fields sent to the model. Description is included only when set."""
    lines = [
        f"Company: {posting.company}",
        f"Title: {posting.title}",
        f"Location: {posting.location}",
        f"Work model: {posting.work_model or ''}",
    ]
    if posting.description and posting.description.strip():
        lines.append(f"Description: {posting.description}")
    return "\n".join(lines)


def default_transport(
    url: str, payload: bytes, *, budget: Deadline | None = None
) -> tuple[int, bytes]:
    """POST once, within TOTAL. A redirect status is returned as-is and is not followed."""
    host, port, path = require_loopback(url)
    try:
        return loopback.exchange(
            "POST",
            host,
            port,
            path,
            body=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            total=TOTAL,
            per_op=TIMEOUT,
            max_bytes=MAX_REPLY,
            budget=budget,
        )
    except loopback.LoopbackError:
        raise EmbedError("unreachable") from None


def embed_text(
    text: str,
    *,
    url: str = DEFAULT_URL,
    transport: Transport | None = None,
) -> list[float]:
    """One embedding. Any failure becomes EmbedError('unreachable')."""
    send = default_transport if transport is None else transport
    try:
        require_loopback(url)
        status, body = send(url, _request(text))
    except (ScoreError, OSError, http.client.HTTPException):
        raise EmbedError("unreachable") from None
    return _values(status, body)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine clamped to [-1, 1]. A length mismatch or a zero vector fails closed."""
    if len(left) != len(right) or not left:
        raise EmbedError("dim-mismatch")
    dot = left_sq = right_sq = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_sq += a * a
        right_sq += b * b
    if left_sq == 0.0 or right_sq == 0.0:
        raise EmbedError("unreachable")
    score = dot / math.sqrt(left_sq * right_sq)
    if not math.isfinite(score):
        raise EmbedError("unreachable")
    if score > 1.0:
        return 1.0
    if score < -1.0:
        return -1.0
    return score


class Embedder:
    """Resume vector for one watch run, then a cosine for each posting.

    The resume is embedded at most once per distinct file contents. Stored posting
    vectors are compared locally. A posting with no vector yet is embedded once.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        data_dir: Path | None,
        config: Mapping[str, Any],
        *,
        transport: Transport | None = None,
        url: str = DEFAULT_URL,
    ) -> None:
        self.conn = conn
        self.data_dir = data_dir
        self.config = config
        self.transport = transport
        self.url = url
        self._cached: tuple[str, int, list[float]] | None = None
        self._failed: str | None = None
        self._reported: set[int] = set()

    def refresh(self) -> list[str]:
        """Recompute stored cosines when the resume is configured. No posting text is embedded."""
        if not self._enabled():
            return []
        try:
            resume = self._resume_vector()
        except EmbedError as exc:
            self._failed = exc.reason
            return self._clear_stored(exc.reason)
        return self._recompute(resume)

    def attach(self, posting_id: int, posting: PostingText) -> str | None:
        """Store this posting's cosine. Returns a reason when the cosine stays null."""
        if not self._enabled():
            return None
        if self._failed is not None:
            return self._miss(posting_id, self._failed)
        try:
            resume = self._resume_vector()
        except EmbedError as exc:
            self._failed = exc.reason
            return self._miss(posting_id, exc.reason)
        try:
            return self._store(posting_id, posting, resume)
        except EmbedError as exc:
            return self._miss(posting_id, exc.reason)

    def _enabled(self) -> bool:
        if self.data_dir is None:
            return False
        search = self.config.get("search")
        if not isinstance(search, Mapping):
            return False
        resume = search.get("resume")
        return isinstance(resume, str) and bool(resume.strip())

    def _resume_file(self) -> Path:
        if self.data_dir is None:
            raise EmbedError("no-resume")
        search = self.config.get("search")
        raw = search.get("resume") if isinstance(search, Mapping) else None
        if not isinstance(raw, str) or not raw.strip():
            raise EmbedError("no-resume")
        relative = Path(raw.strip())
        if relative.is_absolute() or ".." in relative.parts:
            raise EmbedError("no-resume")
        root = self.data_dir.resolve()
        full = (root / relative).resolve()
        if full != root and root not in full.parents:
            raise EmbedError("no-resume")
        if not full.is_file():
            raise EmbedError("no-resume")
        return full

    def _resume_vector(self) -> tuple[str, int, list[float]]:
        path = self._resume_file()
        try:
            raw = path.read_bytes()
        except OSError:
            raise EmbedError("no-resume") from None
        if not raw:
            raise EmbedError("no-resume")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise EmbedError("no-resume") from None
        if not text.strip():
            raise EmbedError("no-resume")
        digest = hashlib.sha256(raw).hexdigest()
        if self._cached is not None and self._cached[0] == digest:
            return self._cached
        stored = search_store.resume_embedding(self.conn)
        if stored is not None and stored[0] == digest:
            try:
                vec = unpack(stored[1], stored[2])
            except EmbedError:
                vec = []
            if vec and len(vec) == stored[1]:
                self._cached = (digest, len(vec), vec)
                return self._cached
        values = embed_text(text, url=self.url, transport=self.transport)
        cosine(values, values)
        dim, blob = pack(values)
        with db.transaction(self.conn):
            search_store.save_resume_embedding(self.conn, digest, dim, blob, _stamp())
        self._cached = (digest, dim, values)
        return self._cached

    def _store(
        self,
        posting_id: int,
        posting: PostingText,
        resume: tuple[str, int, list[float]],
    ) -> str | None:
        _digest, dim, resume_vec = resume
        stored = search_store.posting_embedding(self.conn, posting_id)
        if stored is None:
            values = embed_text(posting_text(posting), url=self.url, transport=self.transport)
            return self._save_new(posting_id, dim, resume_vec, values)
        pdim, pblob = stored
        try:
            posting_vec = unpack(pdim, pblob)
            if len(posting_vec) != dim:
                raise EmbedError("dim-mismatch")
            score = cosine(resume_vec, posting_vec)
        except EmbedError as exc:
            return self._miss(posting_id, exc.reason)
        self._write_score(posting_id, score)
        return None

    def _save_new(
        self,
        posting_id: int,
        dim: int,
        resume_vec: list[float],
        values: list[float],
    ) -> str | None:
        blob_dim, blob = pack(values)
        if blob_dim != dim:
            with db.transaction(self.conn):
                search_store.save_posting_embedding(self.conn, posting_id, blob_dim, blob, _stamp())
                search_store.set_embedding_score(self.conn, posting_id, None)
            return "dim-mismatch"
        score = cosine(resume_vec, values)
        with db.transaction(self.conn):
            search_store.save_posting_embedding(self.conn, posting_id, blob_dim, blob, _stamp())
            search_store.set_embedding_score(self.conn, posting_id, score)
        return None

    def _recompute(self, resume: tuple[str, int, list[float]]) -> list[str]:
        _digest, dim, resume_vec = resume
        lines: list[str] = []
        updates: list[tuple[int, float | None]] = []
        for posting_id, pdim, blob in search_store.posting_embeddings(self.conn):
            try:
                vec = unpack(pdim, blob)
                if len(vec) != dim:
                    raise EmbedError("dim-mismatch")
                updates.append((posting_id, cosine(resume_vec, vec)))
            except EmbedError as exc:
                updates.append((posting_id, None))
                self._reported.add(posting_id)
                lines.append(miss_line(posting_id, exc.reason))
        self._write_scores(updates)
        return lines

    def _clear_stored(self, reason: str) -> list[str]:
        rows = search_store.posting_embeddings(self.conn)
        self._reported.update(posting_id for posting_id, _dim, _blob in rows)
        self._write_scores([(posting_id, None) for posting_id, _dim, _blob in rows])
        return [miss_line(posting_id, reason) for posting_id, _dim, _blob in rows]

    def _miss(self, posting_id: int, reason: str) -> str | None:
        """Null the cosine. One log line per posting, including when refresh already wrote it."""
        self._wipe_score(posting_id)
        if posting_id in self._reported:
            return None
        self._reported.add(posting_id)
        return reason

    def _write_score(self, posting_id: int, score: float | None) -> None:
        self._write_scores([(posting_id, score)])

    def _write_scores(self, updates: list[tuple[int, float | None]]) -> None:
        if not updates:
            return
        with db.transaction(self.conn):
            for posting_id, score in updates:
                search_store.set_embedding_score(self.conn, posting_id, score)

    def _wipe_score(self, posting_id: int) -> None:
        self._write_score(posting_id, None)


def pack(values: Sequence[float]) -> tuple[int, bytes]:
    """Little-endian float32. An empty vector is a bad response."""
    dim = len(values)
    if dim < 1:
        raise EmbedError("unreachable")
    return dim, struct.pack(f"<{dim}f", *[float(value) for value in values])


def unpack(dim: int, blob: bytes) -> list[float]:
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if dim < 1 or not isinstance(blob, bytes) or len(blob) != dim * 4:
        raise EmbedError("dim-mismatch")
    return [float(value) for value in struct.unpack(f"<{dim}f", blob)]


def _request(text: str) -> bytes:
    return json.dumps({"model": MODEL_NAME, "input": text}).encode()


def _values(status: int, body: bytes) -> list[float]:
    if status != 200:
        raise EmbedError("unreachable")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise EmbedError("unreachable") from None
    if not isinstance(payload, dict):
        raise EmbedError("unreachable")
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise EmbedError("unreachable")
    raw = data[0].get("embedding")
    if not isinstance(raw, list) or not raw:
        raise EmbedError("unreachable")
    values: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise EmbedError("unreachable")
        number = float(item)
        if not math.isfinite(number):
            raise EmbedError("unreachable")
        values.append(number)
    return values


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
