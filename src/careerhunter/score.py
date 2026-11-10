"""Score a kept posting with a local OpenAI-compatible model.

The client is not `fetch.py`. That client refuses loopback, which is right for a job
ad and wrong for a model on this Mac. This client allows only `http` to `127.0.0.1`,
`localhost` or `::1`, follows no redirects, and refuses any other host. The URL is a
constant, not a config field. The model id is `scoring.chat_model` (default
`local-model`).

The prompt is built from lanes, location rules, hard gates and `gap_tiers`, the
posting fields, and the resume when one is in use. The model lists gaps. This module
applies `gap_tiers` and stores the tier name. `fit_rating` is left null. There is no
mapping onto 0-10.

Two kinds of failure are told apart. A server that cannot be reached, a timeout, a
refused URL, or any HTTP status other than 200 (a redirect included) raises
ScoreError: the model is not answering, and the caller stops asking it for the rest of
the run. A 200 whose reply does not parse returns None: that posting stays unscored
and is tried again on a later run.

While a resume is in use (D-A1), the stored model text is canonical JSON of the parsed
gaps, never the reply itself. A reply is also treated as one that does not parse when
a gap name or type is longer than 120 characters, or when 40 characters in a row of a
gap name or type also appear in the resume. Both sides are compared after NFKC, case
folding, and turning every run of whitespace into one space. A copied run shorter than
40 characters is not caught.

Key requirements (#39). The model also lists up to three short requirements stated in
the posting itself, separate from the gaps. They are sent to Telegram in the alert
digest, so each is checked here: it must be text; whitespace becomes single spaces,
and control characters go; it is cut to MAX_REQUIREMENT characters at a word, with
"…"; a phrase that contains a gap name (after the same normalizing as above) is
dropped, so gap names never reach the digest; with a resume in use, a phrase that
repeats COPY_RUN characters of the resume is dropped. Duplicates go, and at most three
are kept. A `requirements` field that is missing or the wrong shape gives none. It
never makes the reply unparseable: the score stands either way.
"""

from __future__ import annotations

import http.client
import json
import re
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from careerhunter import loopback
from careerhunter.config import DEFAULT_CHAT_MODEL, chat_model
from careerhunter.deadline import Deadline
from careerhunter.filters import PostingText
from careerhunter.resume import Resume
from careerhunter.search_store import Example

DEFAULT_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_NAME = DEFAULT_CHAT_MODEL  # what is sent when scoring.chat_model is unset
# The request does not stream, so no byte arrives until generation ends. One step has
# to cover that wait, so the step limit is the whole total.
TOTAL = 120.0
TIMEOUT = TOTAL
MAX_OUTPUT = 20_000
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_GAP_TEXT = 120
COPY_RUN = 40
MAX_REQUIREMENT = 60
MAX_REQUIREMENTS = 3
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_SPACES = re.compile(r"\s+")
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_GATE_KEYS = (
    "min_salary",
    "required_languages",
    "credentials",
    "shift_patterns",
    "pay_structures",
    "seniority_words",
    "seniority_in_description",
    "title_exceptions",
    "never_gated_families",
)

Transport = Callable[[str, bytes], tuple[int, bytes]]


class ScoreError(Exception):
    """The model did not answer: a refused URL, no connection, a timeout, or not HTTP 200.

    The message is fixed; it never carries the request or the reply.
    """


@dataclass(frozen=True)
class Score:
    """Parsed learning fields. `tier` is None when the gap count excludes the posting."""

    llm_output: str
    gaps: str
    tier: str | None
    excluded: bool = False
    resume_sha256: str | None = None  # the resume this score was made with
    key_requirements: tuple[str, ...] = ()  # checked phrases from the posting, at most 3


@dataclass(frozen=True)
class _Gap:
    name: str
    kind: str
    type: str | None


def require_loopback(url: str) -> tuple[str, int, str]:
    """Host, port and path. Raises ScoreError unless `url` is plain loopback HTTP."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or host not in _LOOPBACK
    ):
        raise ScoreError("local scoring allows only an http loopback URL")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return host, parts.port or 80, path


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
            max_bytes=MAX_OUTPUT,
            budget=budget,
        )
    except loopback.LoopbackError as exc:
        raise ScoreError("the local model could not be reached") from exc


def score_posting(
    posting: PostingText,
    config: Mapping[str, Any],
    *,
    url: str = DEFAULT_URL,
    transport: Transport | None = None,
    resume: Resume | None = None,
    examples: Sequence[Example] = (),
) -> Score | None:
    """The score, or None when the reply does not parse.

    `examples` are postings the operator labelled (#43): each goes in the request only,
    as its title, lane, gap count and label. Not the company, the description, a cosine
    or a note. The one being scored (same company and title) is left out.

    Raises ScoreError when the model does not answer. None leaves the posting unscored.
    The caller does not alert it.
    """
    rubric = _rubric(config)
    if rubric is None:
        return None
    send = default_transport if transport is None else transport
    require_loopback(url)
    try:
        status, body = send(url, _request_body(posting, config, resume, examples))
    except (ScoreError, OSError, http.client.HTTPException):
        raise ScoreError("the local model could not be reached") from None
    if status != 200:
        raise ScoreError(f"the local model answered HTTP {int(status)}")
    content = _content(body)
    if content is None:
        return None
    parsed = _parse_gaps(content)
    if parsed is None:
        return None
    if resume is not None and _copies_resume(parsed, resume.text):
        return None
    counted, tier, excluded = _apply_tiers(parsed, rubric)
    if tier is None and not excluded:
        return None
    # D-A1: with a resume in the prompt, the reply itself is never stored.
    stored = content[:MAX_OUTPUT] if resume is None else canonical_gaps(parsed)
    return Score(
        llm_output=stored,
        gaps=json.dumps([_gap_dict(gap) for gap in counted], separators=(",", ":")),
        tier=tier,
        excluded=excluded,
        resume_sha256=None if resume is None else resume.sha256,
        key_requirements=_requirements(content, parsed, None if resume is None else resume.text),
    )


def canonical_gaps(gaps: list[_Gap]) -> str:
    """The parsed gaps as canonical JSON: sorted keys, compact, UTF-8 text kept as is."""
    return json.dumps(
        {"gaps": [_gap_dict(gap) for gap in gaps]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def normalized(text: str) -> str:
    """NFKC, case-folded, every run of whitespace one space, stripped (D-A1)."""
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _copies_resume(gaps: list[_Gap], resume_text: str) -> bool:
    """True when a gap is too long, or repeats COPY_RUN characters of the resume."""
    haystack = normalized(resume_text)
    for gap in gaps:
        for field in (gap.name, gap.type):
            if field is None:
                continue
            if len(field) > MAX_GAP_TEXT:
                return True
            needle = normalized(field)
            for start in range(len(needle) - COPY_RUN + 1):
                if needle[start : start + COPY_RUN] in haystack:
                    return True
    return False


def _request_body(
    posting: PostingText,
    config: Mapping[str, Any],
    resume: Resume | None = None,
    examples: Sequence[Example] = (),
) -> bytes:
    view: dict[str, Any] = {
        "lanes": _section(config, "search", "lanes"),
        "location_rules": _section(config, "search", "location_rules"),
        "hard_gates": _gates(config),
        "gap_tiers": _rubric(config),
        "posting": {
            "company": posting.company,
            "title": posting.title,
            "location": posting.location,
            "work_model": posting.work_model,
            "description": posting.description,
        },
    }
    if resume is not None:
        view["resume"] = resume.text
    shown = [
        {"title": ex.title, "lane": ex.lane, "gaps": ex.gap_count, "label": ex.label}
        for ex in examples
        if (ex.company.casefold(), ex.title.casefold())
        != (posting.company.casefold(), posting.title.casefold())
    ]
    if shown:
        view["labelled_examples"] = shown
    payload = {
        "model": chat_model(config),
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Reply with one JSON object only, of the form "
                    '{"gaps":[{"name":"...","kind":"minor"|"core","type":"..."}],'
                    '"requirements":["..."]}. '
                    "kind is minor or core. List missing requirements. "
                    "Use an empty gaps array when nothing is missing. "
                    "requirements: up to three key requirements the posting itself "
                    f"states, each a short phrase of at most {MAX_REQUIREMENT} characters, "
                    "whether or not they are gaps. No prose. "
                    "labelled_examples, when present, are other postings and what the "
                    "candidate decided about them (applied and its latest status, or "
                    "skipped); use them only to calibrate, never as requirements."
                ),
            },
            {"role": "user", "content": json.dumps(view)},
        ],
    }
    return json.dumps(payload).encode()


def _gates(config: Mapping[str, Any]) -> dict[str, Any]:
    gates = _mapping(config.get("hard_gates"))
    return {key: gates[key] for key in _GATE_KEYS if key in gates}


def _section(config: Mapping[str, Any], parent: str, key: str) -> Any:
    return _mapping(config.get(parent)).get(key)


def _rubric(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = _section(config, "fit_rubric", "gap_tiers")
    if isinstance(value, Mapping):
        return value
    return None


def _content(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _parse_gaps(content: str) -> list[_Gap] | None:
    raw = _json_object(content)
    if not isinstance(raw, dict):
        return None
    gaps = raw.get("gaps")
    if not isinstance(gaps, list):
        return None
    parsed: list[_Gap] = []
    for item in gaps:
        gap = _one_gap(item)
        if gap is None:
            return None
        parsed.append(gap)
    return parsed


def _one_gap(item: object) -> _Gap | None:
    if not isinstance(item, dict):
        return None
    name, kind, gap_type = item.get("name"), item.get("kind"), item.get("type")
    if not isinstance(name, str) or not name.strip() or kind not in ("minor", "core"):
        return None
    if gap_type is not None and (not isinstance(gap_type, str) or not gap_type.strip()):
        return None
    return _Gap(name.strip(), kind, gap_type.strip() if isinstance(gap_type, str) else None)


def _json_object(content: str) -> object | None:
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            loaded = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded


def _requirements(content: str, gaps: list[_Gap], resume_text: str | None) -> tuple[str, ...]:
    """Up to three checked key requirements from the reply; () when there are none."""
    raw = _json_object(content)
    items = raw.get("requirements") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return ()
    names = [normalized(gap.name) for gap in gaps]
    haystack = normalized(resume_text) if resume_text is not None else None
    kept: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        phrase = " ".join(_CONTROL.sub(" ", item).split())
        folded = normalized(phrase)
        if not folded or folded in seen:
            continue
        if any(name and name in folded for name in names):
            continue  # it names a gap
        if haystack is not None and _repeats(folded, haystack):
            continue
        seen.add(folded)
        kept.append(_capped(phrase))
        if len(kept) == MAX_REQUIREMENTS:
            break
    return tuple(kept)


def _repeats(needle: str, haystack: str) -> bool:
    return any(
        needle[start : start + COPY_RUN] in haystack for start in range(len(needle) - COPY_RUN + 1)
    )


def _capped(phrase: str) -> str:
    """At most MAX_REQUIREMENT characters, cut at a word, with "…" when cut."""
    if len(phrase) <= MAX_REQUIREMENT:
        return phrase
    cut = phrase[: MAX_REQUIREMENT - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-") + "…"


def _apply_tiers(
    gaps: list[_Gap], rubric: Mapping[str, Any]
) -> tuple[list[_Gap], str | None, bool]:
    """The gaps that count, the tier name, and whether the count excludes the posting."""
    exempt = {_fold(item) for item in _strings(rubric.get("exempt_requirements"))}
    counted = [gap for gap in gaps if not (gap.type and _fold(gap.type) in exempt)]
    exclude_at = rubric.get("exclude_from_gaps")
    if (
        isinstance(exclude_at, int)
        and not isinstance(exclude_at, bool)
        and len(counted) >= exclude_at
    ):
        return counted, None, True
    boundary = rubric.get("boundary")
    if isinstance(boundary, Mapping) and boundary.get("at_gaps") == len(counted):
        key = "all_minor" if all(gap.kind == "minor" for gap in counted) else "any_core"
        name = boundary.get(key)
        if isinstance(name, str):
            return counted, name, False
        return counted, None, False
    for tier in rubric.get("tiers") or []:
        if not isinstance(tier, Mapping):
            continue
        max_gaps = tier.get("max_gaps")
        name = tier.get("name")
        if (
            isinstance(max_gaps, int)
            and not isinstance(max_gaps, bool)
            and isinstance(name, str)
            and len(counted) <= max_gaps
        ):
            return counted, name, False
    return counted, None, False


def _gap_dict(gap: _Gap) -> dict[str, str]:
    stored = {"name": gap.name, "kind": gap.kind}
    if gap.type:
        stored["type"] = gap.type
    return stored


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _fold(value: str) -> str:
    return value.casefold().strip()
