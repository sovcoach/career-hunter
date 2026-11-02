"""Score a kept posting with a local OpenAI-compatible model.

The client is not `fetch.py`. That client refuses loopback, which is right for a job
ad and wrong for a model on this Mac. This client allows only `http` to `127.0.0.1`,
`localhost` or `::1`, follows no redirects, and refuses any other host. The URL is a
constant, not a config field.

The prompt is built from lanes, location rules, hard gates and `gap_tiers`, plus the
posting fields. The model lists gaps. This module applies `gap_tiers` and stores the
tier name. `fit_rating` is left null. There is no mapping onto 0-10.

A down server, a redirect, or a reply that does not parse leaves the posting unscored.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from careerhunter.filters import PostingText

DEFAULT_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_NAME = "local-model"
TIMEOUT = 30.0
MAX_OUTPUT = 20_000
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_GATE_KEYS = (
    "min_salary",
    "required_languages",
    "credentials",
    "shift_patterns",
    "pay_structures",
    "seniority_words",
    "title_exceptions",
    "never_gated_families",
)

Transport = Callable[[str, bytes], tuple[int, bytes]]


class ScoreError(Exception):
    """The local model URL is not loopback, or the server could not be reached."""


@dataclass(frozen=True)
class Score:
    """Parsed learning fields. `tier` is None when the gap count excludes the posting."""

    llm_output: str
    gaps: str
    tier: str | None
    excluded: bool = False


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


def default_transport(url: str, payload: bytes) -> tuple[int, bytes]:
    """POST once. A redirect status is returned as-is and is not followed."""
    host, port, path = require_loopback(url)
    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        conn.request(
            "POST",
            path,
            body=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        response = conn.getresponse()
        return response.status, response.read(MAX_OUTPUT + 1)
    except OSError as exc:
        raise ScoreError("the local model could not be reached") from exc
    finally:
        conn.close()


def score_posting(
    posting: PostingText,
    config: Mapping[str, Any],
    *,
    url: str = DEFAULT_URL,
    transport: Transport | None = None,
) -> Score | None:
    """The score, or None when the model is down or the reply does not parse.

    None leaves the posting unscored. The caller does not alert it.
    """
    rubric = _rubric(config)
    if rubric is None:
        return None
    send = default_transport if transport is None else transport
    try:
        require_loopback(url)
        status, body = send(url, _request_body(posting, config))
    except (ScoreError, OSError):
        return None
    content = _content(status, body)
    if content is None:
        return None
    parsed = _parse_gaps(content)
    if parsed is None:
        return None
    counted, tier, excluded = _apply_tiers(parsed, rubric)
    if tier is None and not excluded:
        return None
    return Score(
        llm_output=content[:MAX_OUTPUT],
        gaps=json.dumps([_gap_dict(gap) for gap in counted], separators=(",", ":")),
        tier=tier,
        excluded=excluded,
    )


def _request_body(posting: PostingText, config: Mapping[str, Any]) -> bytes:
    view = {
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
    payload = {
        "model": MODEL_NAME,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Reply with one JSON object only, of the form "
                    '{"gaps":[{"name":"...","kind":"minor"|"core","type":"..."}]}. '
                    "kind is minor or core. List missing requirements. "
                    "Use an empty gaps array when nothing is missing. No prose."
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


def _content(status: int, body: bytes) -> str | None:
    if status != 200:
        return None
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
