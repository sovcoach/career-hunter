"""Score a kept posting with a local OpenAI-compatible model.

The client is not `fetch.py`. That client refuses loopback, which is right for a job
ad and wrong for a model on this Mac. This client allows only `http` to `127.0.0.1`,
`localhost` or `::1`, follows no redirects, and refuses any other host. The URL is a
constant, not a config field. The model id is `scoring.chat_model` (default
`local-model`).

The prompt is built from lanes and location rules, the posting fields,
and the resume when one is in use. The model lists every item the posting states, each
classed (`required`, `preferred`, `duty`, `trained` or `soft`), with whether the resume
meets it. The gaps are derived here (`derive_gaps`): only an unmet required or
preferred item, or a soft one not met at all, is a gap; a duty, or anything the
posting says it will teach, never is. Gap names are put in one style (`gap_name`) and
gap types come from a fixed list (GAP_TYPES). With a resume, a `yes` or `partly` whose
evidence is not found in the resume counts as `no`. An `entry_level` flag counts only
with a quote found in the posting. The tier (high, medium or low) is computed here from
the core and minor gaps (`_apply_tiers`, `fit_rubric.requirement_tiers`), and no
threshold is ever sent to the model. This module stores the tier name. `fit_rating` is
left null. There is no mapping onto 0-10.

The hard gates are not sent (ADR-027). `filters.decide` has applied them before any
posting is scored, and a model shown `required_languages` next to a posting reported
those languages as the posting's own requirement. As a guard, a gap or a key
requirement that names a `hard_gates.required_languages` term is dropped unless the
posting text sent (title, location and description) contains that term, matched the
way the filter matches.

Two kinds of failure are told apart. A server that cannot be reached, a timeout, a
refused URL, or any HTTP status other than 200 (a redirect included) raises
ScoreError: the model did not answer. Its `kind` says which: `timeout` when the call
reached its time limit (the model may still have been working, on this posting only),
`unauthorized` for a refused token, else `unreachable`; the watcher carries on after a
timeout and stops asking the model for the rest of the run after the others. A 200
whose reply does not parse returns None: that posting stays unscored and is tried
again on a later run.

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
are kept. A `key_requirements` field that is missing or the wrong shape gives none. It
never makes the reply unparseable: the score stands either way.

Criteria vetoes (ADR-028). When the operator's criteria file is in use, its numbered
rules go in the request, and the model may answer with a veto: a rule number and a
quote copied from the posting that shows the match. The quote is checked here, never
trusted: after `normalized` it must be at least MIN_QUOTE characters and appear in the
title and description sent in this call, and the rule must exist. A veto that passes
is returned as `Score.veto`. One that fails is a rejected veto with a fixed reason
(REJECTED), and the posting is scored from the same reply's gaps, with no second call.
A veto is never honoured when no criteria were sent. The quote is never stored, logged
or returned: a reply with a veto is stored as its canonical gaps, as with a resume.

Caps work the same way. A cap rule names a fit level; the reply's `cap` names the rule
and quotes the posting, checked as a veto's quote is, and the rule must be a cap rule.
A cap that passes is `Score.cap`, with the level, and the watcher stores the lower of
the tier's fit and that level. One that fails is rejected and the score stands. A veto
that passes wins over a cap. A reply with a cap is stored as canonical gaps too.

Structured output and sampling. Every request asks for a reply that follows a JSON
schema (`response_format` of type `json_schema`, strict), which LM Studio enforces as it
generates. A reply that breaks it anyway does not parse, as before. The sampling
settings and the seed are the fixed SAMPLING and SEED: a thinking model's preset, never
temperature 0, and never configurable.

Reasoning models. The chat model may think before it answers. Its thinking is removed
before anything is parsed or stored (`strip_reasoning`), and a reply whose thinking
never closes does not parse. The body may be up to MAX_REPLY bytes, since the thinking
can be long; at most MAX_OUTPUT characters of what is left are stored.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import re
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from careerhunter import loopback
from careerhunter.config import DEFAULT_CHAT_MODEL, GAP_TYPE_NAMES, chat_model, requirement_tiers
from careerhunter.criteria import Criteria, Rule
from careerhunter.deadline import Deadline
from careerhunter.filters import PostingText, mentions
from careerhunter.resume import Resume
from careerhunter.search_store import Example

DEFAULT_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_NAME = DEFAULT_CHAT_MODEL  # what is sent when scoring.chat_model is unset
# The request does not stream, so no byte arrives until generation ends. One step has
# to cover that wait, so the step limit is the whole total. A watch run uses
# `scoring.timeout_seconds` instead; TOTAL is the limit when none is given.
TOTAL = 120.0
TIMEOUT = TOTAL
# The reply as it arrives. A reasoning model's thinking can run far past what is kept,
# so the cap on the body is well above MAX_OUTPUT.
MAX_REPLY = 262_144
MAX_OUTPUT = 20_000  # characters of the stripped reply kept, without a resume
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
UNAUTHORIZED = frozenset({401, 403})  # LM Studio refused the API token, or none was sent
MAX_GAP_TEXT = 120
COPY_RUN = 40
MAX_REQUIREMENT = 60
MAX_REQUIREMENTS = 3
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_SPACES = re.compile(r"\s+")
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
# A reasoning model's thinking, in the tags such models use. Matched without regard to
# case. A reply whose thinking never closes is treated as one that does not parse.
_THINK_TAGS = "think|thinking|reasoning"
_THINK_BLOCK = re.compile(rf"<({_THINK_TAGS})>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(rf"<(?:{_THINK_TAGS})>", re.IGNORECASE)
_THINK_CLOSE = re.compile(rf"</(?:{_THINK_TAGS})\s*>", re.IGNORECASE)
Transport = Callable[[str, bytes], tuple[int, bytes]]
# Sampling, sent with every scoring request and not configurable. The scorer is a
# thinking model: greedy decoding (temperature 0) can make one repeat itself until the
# timeout, so the request uses the thinking-mode preset for precise tasks (temperature
# 0.6, top_p 0.95, top_k 20, min_p 0) with a fixed seed, so that the same request gives
# the same reply where the server honours the seed. That preset alone still looped in its
# thinking on some postings (calibration, Batch Q), so presence_penalty is 1.5, the
# model maker's setting against endless repetition.
SAMPLING: dict[str, float | int] = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repeat_penalty": 1.0,
}
SEED = 20261119
SCHEMA_NAME = "posting_fit"
MAX_QUOTE = 200  # what the model is asked for; a longer quote is still checked
MIN_QUOTE = 8  # characters after `normalized`: shorter shows no match
# The fixed reasons a veto is not honoured, by key.
REJECTED = {
    "not-found": "quote not found",
    "too-short": "quote too short",
    "unknown-rule": "unknown rule",
}
SYSTEM_PROMPT = (
    "Reply with one JSON object only, following the schema. "
    "requirements: every item the posting states, each once, in its own words: each "
    "qualification it requires or prefers, each duty or responsibility of the job, "
    "each thing it says it will teach or train, and each soft skill. kind: "
    '"required" or "preferred" for a qualification the posting asks the candidate to '
    'have; "duty" for what the job does (a responsibility, not a qualification); '
    '"trained" for anything the posting says it will teach, train or help the '
    'candidate learn; "soft" for a soft skill or personal quality. core: true when the '
    "item is central to the role's main function or is a minimum number of years of "
    "experience, else false. main_function: true only when the item is the role's main "
    "job function itself (for example, years as an analyst in that same function). "
    "years: the minimum years of experience the item asks for, 0 when it asks none. "
    'met: "yes", "partly" or "no", judged only against the resume; evidence: for yes '
    "or partly, a short phrase copied exactly from the resume that shows it, else an "
    "empty string. type: one of the listed types. entry_level: when the posting says "
    "it is junior or entry level, or welcomes candidates without every qualification "
    '(such as recent graduates, "we\'ll train you", or "apply anyway"), a short '
    "quote copied exactly from the posting that says so, else an empty string. "
    "key_requirements: up to three key requirements the posting itself states, each a "
    f"short phrase of at most {MAX_REQUIREMENT} characters. No prose."
)
# Added to the system prompt when the operator's criteria file is sent.
CRITERIA_INSTRUCTION = (
    " criteria, when present, are numbered rules in the candidate's own words, each of "
    'kind "veto" (postings the candidate won\'t take) or "cap" (postings whose fit is '
    "at most the rule's cap level). If this posting clearly meets a veto rule, set "
    '"veto" to {"rule":N,"quote":"..."}; if it clearly meets a cap rule, set "cap" to '
    '{"rule":N,"quote":"..."}. N is that rule\'s number, and quote is copied '
    f"exactly from the posting's title or description, at most {MAX_QUOTE} characters, "
    "and shows the match. Apply each rule exactly as written: whatever the rule names "
    "counts, a preference, a nice-to-have or an asset included when the rule names it; "
    "an exception the rule itself allows that the posting states, or the allowed side "
    "of a limit the rule sets, is not a match. Without a clear match, set veto and cap "
    "to null. Criteria are not requirements of the posting: never list them as "
    "requirements."
)


KINDS = ("unreachable", "unauthorized", "timeout")
TIMED_OUT = "the local model took longer than the timeout"
UNREACHED = "the local model could not be reached"


class ScoreError(Exception):
    """The model did not answer: a refused URL, no connection, a timeout, or not HTTP 200.

    The message is fixed; it never carries the request, the reply or the token. `kind`
    is `timeout` when the call reached its time limit, `unauthorized` for a 401 or 403
    (LM Studio refused the API token), else `unreachable`. `type_name` is the class name
    of the exception behind it, from `loopback.TYPE_NAMES`, or None when there was none
    (a refused URL or an HTTP status).
    """

    def __init__(
        self, message: str, *, kind: str = "unreachable", type_name: str | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind if kind in KINDS else "unreachable"
        self.type_name = type_name


def _transport_failure(exc: BaseException) -> ScoreError:
    """A ScoreError for what a transport raised. One the transport raised itself keeps
    its kind and type; a socket timeout is a timeout; anything else is unreachable."""
    if isinstance(exc, ScoreError):
        return exc
    if isinstance(exc, loopback.LoopbackError):
        timed_out = exc.cause == "timeout"
        return ScoreError(
            TIMED_OUT if timed_out else UNREACHED,
            kind="timeout" if timed_out else "unreachable",
            type_name=exc.type_name,
        )
    name = type(exc).__name__
    type_name = name if name in loopback.TYPE_NAMES else "other"
    if isinstance(exc, TimeoutError):
        return ScoreError(TIMED_OUT, kind="timeout", type_name=type_name)
    return ScoreError(UNREACHED, type_name=type_name)


VETO_WORDS = 6  # words of an unlabelled rule in the skip reason


@dataclass(frozen=True)
class Veto:
    """A veto in the reply. `rejected` is a key of REJECTED when it was not honoured;
    `rule` is the rule number named, when the reply named one as a number. `skip` is
    the skip reason for an honoured one, built from the criteria file, never the reply:
    `criteria: N. <label>`, or for a rule with no label `criteria: N. <the rule's first
    VETO_WORDS words>`, ending in `…` when the rule goes on."""

    rule: int | None
    rejected: str | None = None
    skip: str = dataclasses.field(default="", compare=False)


@dataclass(frozen=True)
class Cap:
    """A cap in the reply. `rejected` is a key of REJECTED when it was not applied;
    `level` is the cap rule's fit level (from the file) for one that was."""

    rule: int | None
    rejected: str | None = None
    level: str | None = None


@dataclass(frozen=True)
class Score:
    """Parsed learning fields. `tier` is None when the gap count excludes the posting.

    With an honoured veto (`veto.rejected` None), the posting is skipped for that rule
    and the other fields are not used. A rejected veto leaves the score as scored."""

    llm_output: str
    gaps: str
    tier: str | None
    excluded: bool = False
    resume_sha256: str | None = None  # the resume this score was made with
    key_requirements: tuple[str, ...] = ()  # checked phrases from the posting, at most 3
    veto: Veto | None = None
    cap: Cap | None = None
    # The reply flagged the posting as junior or entry level with a quote found in it.
    entry_level: bool = False

    @property
    def vetoed(self) -> bool:
        """True when a criteria rule vetoed the posting and the quote checked out."""
        return self.veto is not None and self.veto.rejected is None

    @property
    def capped(self) -> str | None:
        """The fit level a checked cap holds the posting to, or None. No cap applies
        to a vetoed posting."""
        if self.vetoed or self.cap is None or self.cap.rejected is not None:
            return None
        return self.cap.level


@dataclass(frozen=True)
class _Gap:
    name: str
    kind: str
    type: str | None
    # Used for the tier only, never stored: the gap is the role's main job function.
    main_function: bool = dataclasses.field(default=False, compare=False)


# What the reply classes each stated item as. Only an unmet `required` or `preferred`
# item can be a gap, and a `soft` one only when it is not met at all. A `duty` (what
# the job does) or a `trained` item (what the posting says it will teach) never is.
ITEM_KINDS = ("required", "preferred", "duty", "trained", "soft")
GAP_KINDS = frozenset({"required", "preferred"})
# The fixed gap types. A reply's type outside this list is stored as `other`.
GAP_TYPES = tuple(name for name in GAP_TYPE_NAMES if name != "seniority")
MET = ("yes", "partly", "no")


@dataclass(frozen=True)
class Requirement:
    """One item the posting states, as the reply classed it."""

    text: str
    kind: str
    type: str
    core: bool
    main_function: bool
    years: int
    met: str
    evidence: str


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


def model_headers(token: str | None) -> dict[str, str]:
    """The JSON headers for a call to the local model, with `Authorization: Bearer` when
    `token` (the Keychain's `lm_studio_token`) is set. Never log the result."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def default_transport(
    url: str,
    payload: bytes,
    *,
    budget: Deadline | None = None,
    token: str | None = None,
    timeout: float | None = None,
) -> tuple[int, bytes]:
    """POST once, within `timeout` (`scoring.timeout_seconds`; TOTAL if None). A redirect
    status is returned as-is and is not followed.

    `token` is sent as a Bearer header when set; it is never logged or put in an error.
    """
    limit = TOTAL if timeout is None else timeout
    host, port, path = require_loopback(url)
    try:
        return loopback.exchange(
            "POST",
            host,
            port,
            path,
            body=payload,
            headers=model_headers(token),
            total=limit,
            per_op=limit,
            max_bytes=MAX_REPLY,
            budget=budget,
        )
    except loopback.LoopbackError as exc:
        raise _transport_failure(exc) from exc


MODELS_URL = "http://127.0.0.1:1234/v1/models"
PROBE_SECONDS = 5.0  # doctor's check: a list of models, which loads none
PROBE_MAX = 1_048_576


def probe_models(token: str | None, *, url: str = MODELS_URL) -> int:
    """The HTTP status of one `GET /v1/models` with the token, for doctor.

    It asks for the list of models, so no model is loaded. Raises ScoreError when the
    server can't be reached within PROBE_SECONDS. The body is not read beyond the cap
    and is not returned.
    """
    host, port, path = require_loopback(url)
    headers = {k: v for k, v in model_headers(token).items() if k != "Content-Type"}
    try:
        status, _body = loopback.exchange(
            "GET",
            host,
            port,
            path,
            body=None,
            headers=headers,
            total=PROBE_SECONDS,
            per_op=PROBE_SECONDS,
            max_bytes=PROBE_MAX,
        )
    except loopback.LoopbackError as exc:
        raise ScoreError("the local model could not be reached") from exc
    return status


def score_posting(
    posting: PostingText,
    config: Mapping[str, Any],
    *,
    url: str = DEFAULT_URL,
    transport: Transport | None = None,
    resume: Resume | None = None,
    examples: Sequence[Example] = (),
    criteria: Criteria | None = None,
) -> Score | None:
    """The score, or None when the reply does not parse.

    `criteria` is the operator's criteria file (search.criteria_file): its numbered
    rules are sent, and a veto in the reply is checked against them and the posting
    text. None sends no criteria at all, and a veto is then ignored.

    `examples` are postings the operator labelled (#43): each goes in the request only,
    as its title, lane, gap count and label, and an operator skip's reason. Not the
    company, the description, a cosine or a note. The one being scored (same company
    and title) is left out.

    Raises ScoreError when the model does not answer, with kind `timeout` when the call
    reached its time limit. None leaves the posting unscored. The caller does not alert
    it.
    """
    rubric = _rubric(config)
    if rubric is None:
        return None
    send = default_transport if transport is None else transport
    require_loopback(url)
    try:
        status, body = send(url, _request_body(posting, config, resume, examples, criteria))
    except (ScoreError, OSError, http.client.HTTPException) as exc:
        raise _transport_failure(exc) from None
    if status in UNAUTHORIZED:
        raise ScoreError("the local model refused the API token", kind="unauthorized")
    if status != 200:
        raise ScoreError(f"the local model answered HTTP {int(status)}")
    raw = _content(body)
    content = None if raw is None else strip_reasoning(raw)
    if content is None:
        return None
    items = _parse_requirements(content)
    if items is None:
        return None
    items = [
        item
        for item in items
        if not unstated_language(item.text, posting, config)
        and not unstated_language(item.evidence, posting, config)
    ]
    if resume is not None:
        items = [_evidenced(item, resume.text) for item in items]
    parsed = derive_gaps(items)
    if resume is not None and _copies_resume(parsed, resume.text):
        return None
    veto = None if criteria is None else _veto(content, posting, criteria)
    cap = None if criteria is None else _cap(content, posting, criteria)
    entry_level = _entry_level(content, posting)
    counted, tier, excluded = _apply_tiers(parsed, rubric, entry_level=entry_level)
    if tier is None and not excluded and not (veto is not None and veto.rejected is None):
        return None
    # D-A1: with a resume in the prompt, the reply itself is never stored. Nor is a
    # reply with a veto or a cap, so its quote is never kept (ADR-028).
    plain = resume is None and veto is None and cap is None
    stored = content[:MAX_OUTPUT] if plain else canonical_gaps(parsed)
    return Score(
        llm_output=stored,
        gaps=json.dumps([_gap_dict(gap) for gap in counted], separators=(",", ":")),
        tier=tier,
        excluded=excluded,
        resume_sha256=None if resume is None else resume.sha256,
        key_requirements=tuple(
            phrase
            for phrase in _requirements(
                content, parsed, None if resume is None else resume.text, limit=None
            )
            if not unstated_language(phrase, posting, config)
        )[:MAX_REQUIREMENTS],
        veto=veto,
        cap=cap,
        entry_level=entry_level,
    )


def _grounded(
    content: str, key: str, posting: PostingText, criteria: Criteria
) -> tuple[int | None, str | None] | None:
    """The rule number the reply's `key` ("veto" or "cap") names and why it was not
    honoured (a key of REJECTED), or None when the reply has none. The rule must be of
    that kind; the quote must be MIN_QUOTE characters or more and appear in the title
    and description sent. The quote itself is not kept."""
    raw = _json_object(content)
    item = raw.get(key) if isinstance(raw, dict) else None
    if item is None:
        return None
    rule = item.get("rule") if isinstance(item, dict) else None
    number = rule if isinstance(rule, int) and not isinstance(rule, bool) else None
    if number is None or criteria.rule(number, key) is None:
        return number, "unknown-rule"
    quote = item.get("quote")
    needle = normalized(_CONTROL.sub(" ", quote)) if isinstance(quote, str) else ""
    if len(needle) < MIN_QUOTE:
        return number, "too-short"
    sent = normalized("\n".join(part for part in (posting.title, posting.description) if part))
    if needle not in sent:
        return number, "not-found"
    return number, None


def _veto(content: str, posting: PostingText, criteria: Criteria) -> Veto | None:
    """The reply's veto, checked; None when the reply has none."""
    found = _grounded(content, "veto", posting, criteria)
    if found is None:
        return None
    number, rejected = found
    if rejected is not None or number is None:
        return Veto(number, rejected)
    return Veto(number, skip=f"criteria: {number}. {_rule_name(criteria.rules[number - 1])}")


def _rule_name(rule: Rule) -> str:
    """The rule's label, else its first VETO_WORDS words, with `…` when it goes on."""
    if rule.label:
        return rule.label
    words = rule.text.split()
    excerpt = " ".join(words[:VETO_WORDS])
    return f"{excerpt}…" if len(words) > VETO_WORDS else excerpt


def _cap(content: str, posting: PostingText, criteria: Criteria) -> Cap | None:
    """The reply's cap, checked; None when the reply has none."""
    found = _grounded(content, "cap", posting, criteria)
    if found is None:
        return None
    number, rejected = found
    if rejected is not None or number is None:
        return Cap(number, rejected)
    return Cap(number, level=criteria.rules[number - 1].cap)


def strip_reasoning(content: str) -> str | None:
    """The reply without a reasoning model's thinking, or None when nothing is left.

    Every `<think>…</think>` block (also `<thinking>` and `<reasoning>`) goes. When a
    closing tag is left over (a chat template that opened the tag itself), everything
    up to the last one goes too. A tag that opens and never closes means the thinking
    was cut off: None, so the posting stays unscored. A separate `reasoning_content`
    field is never read: only `message.content` is.
    """
    text = _THINK_BLOCK.sub("", content)
    closes = list(_THINK_CLOSE.finditer(text))
    if closes:
        text = text[closes[-1].end() :]
    if _THINK_OPEN.search(text):
        return None
    text = text.strip()
    return text or None


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
    criteria: Criteria | None = None,
) -> bytes:
    view: dict[str, Any] = {
        "lanes": _section(config, "search", "lanes"),
        "location_rules": _section(config, "search", "location_rules"),
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
        | ({"reason": ex.reason} if ex.reason else {})
        for ex in examples
        if (ex.company.casefold(), ex.title.casefold())
        != (posting.company.casefold(), posting.title.casefold())
    ]
    if shown:
        view["labelled_examples"] = shown
    if criteria is not None:
        view["criteria"] = [
            {"rule": rule.number, "kind": rule.kind, "text": rule.text}
            | ({"cap": rule.cap} if rule.cap else {})
            for rule in criteria.rules
        ]
    payload = {
        "model": chat_model(config),
        **SAMPLING,
        "seed": SEED,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": SCHEMA_NAME,
                "strict": True,
                "schema": reply_schema(criteria is not None),
            },
        },
        "messages": [
            {
                "role": "system",
                "content": (
                    SYSTEM_PROMPT
                    + " labelled_examples, when present, are other postings and what the "
                    "candidate decided about them (applied and its latest status, or "
                    "skipped, with the candidate's reason when given); use them only to "
                    "calibrate, never as requirements."
                    + (CRITERIA_INSTRUCTION if criteria is not None else "")
                ),
            },
            {"role": "user", "content": json.dumps(view)},
        ],
    }
    return json.dumps(payload).encode()


def _quoted_rule() -> dict[str, Any]:
    """A veto or a cap in the reply: null, or a rule number and a quote."""
    return {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["rule", "quote"],
                "properties": {"rule": {"type": "integer"}, "quote": {"type": "string"}},
            },
        ]
    }


def reply_schema(with_criteria: bool) -> dict[str, Any]:
    """The JSON schema the reply must follow (`response_format`, strict). With criteria,
    `veto` and `cap` are required and null when the posting meets no rule."""
    item = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "text",
            "kind",
            "type",
            "core",
            "main_function",
            "years",
            "met",
            "evidence",
        ],
        "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": list(ITEM_KINDS)},
            "type": {"type": "string", "enum": list(GAP_TYPES)},
            "core": {"type": "boolean"},
            "main_function": {"type": "boolean"},
            "years": {"type": "integer", "minimum": 0},
            "met": {"type": "string", "enum": list(MET)},
            "evidence": {"type": "string"},
        },
    }
    properties: dict[str, Any] = {
        "requirements": {"type": "array", "items": item},
        "key_requirements": {"type": "array", "items": {"type": "string"}},
        "entry_level": {"type": "string"},
    }
    if with_criteria:
        properties["veto"] = _quoted_rule()
        properties["cap"] = _quoted_rule()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def unstated_language(phrase: str, posting: PostingText, config: Mapping[str, Any]) -> bool:
    """True when `phrase` names a `required_languages` term the posting text lacks."""
    terms = [
        term
        for term in _strings(_mapping(config.get("hard_gates")).get("required_languages"))
        if mentions(phrase, term)
    ]
    if not terms:
        return False
    text = "\n".join(
        part for part in (posting.title, posting.location, posting.description) if part
    )
    return not any(mentions(text, term) for term in terms)


def _section(config: Mapping[str, Any], parent: str, key: str) -> Any:
    return _mapping(config.get(parent)).get(key)


def _rubric(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return requirement_tiers(config)


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


def _parse_requirements(content: str) -> list[Requirement] | None:
    """The reply's stated items, or None when the reply does not parse. One malformed
    item makes the whole reply one that does not parse, as a malformed gap did."""
    raw = _json_object(content)
    if not isinstance(raw, dict):
        return None
    items = raw.get("requirements")
    if not isinstance(items, list):
        return None
    parsed: list[Requirement] = []
    for item in items:
        one = _one_requirement(item)
        if one is None:
            return None
        parsed.append(one)
    return parsed


def _one_requirement(item: object) -> Requirement | None:
    if not isinstance(item, dict):
        return None
    text, kind, met = item.get("text"), item.get("kind"), item.get("met")
    core, main, years = item.get("core"), item.get("main_function"), item.get("years")
    evidence, item_type = item.get("evidence", ""), item.get("type", "other")
    if not isinstance(text, str) or not text.strip() or kind not in ITEM_KINDS or met not in MET:
        return None
    if not isinstance(core, bool) or not isinstance(main, bool):
        return None
    if isinstance(years, bool) or not isinstance(years, int) or years < 0:
        return None
    if not isinstance(evidence, str) or not isinstance(item_type, str):
        return None
    folded = _fold(item_type)
    return Requirement(
        text=" ".join(_CONTROL.sub(" ", text).split()),
        kind=kind,
        type=folded if folded in GAP_TYPES else "other",
        core=core,
        main_function=main,
        years=years,
        met=met,
        evidence=" ".join(_CONTROL.sub(" ", evidence).split()),
    )


def _evidenced(item: Requirement, resume_text: str) -> Requirement:
    """The item, with `met` turned to `no` when it claims yes or partly and its
    evidence is not in the resume: too short (MIN_QUOTE after `normalized`), or not
    found there. The evidence itself is never stored."""
    if item.met == "no":
        return item
    needle = normalized(item.evidence)
    if len(needle) >= MIN_QUOTE and needle in normalized(resume_text):
        return item
    return dataclasses.replace(item, met="no")


def derive_gaps(items: Sequence[Requirement]) -> list[_Gap]:
    """The gaps, in code: an unmet `required` or `preferred` item, or a `soft` one not
    met at all. A `core` item not met at all is a core gap; anything else, a partly met
    item included, is minor. Duties and trained items are never gaps."""
    gaps: list[_Gap] = []
    seen: set[str] = set()
    for item in items:
        if item.met == "yes":
            continue
        if item.kind in GAP_KINDS:
            kind = "core" if item.core and item.met == "no" else "minor"
        elif item.kind == "soft" and item.met == "no":
            kind = "minor"
        else:
            continue
        name = gap_name(item.text)
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        gaps.append(_Gap(name, kind, item.type, main_function=item.main_function))
    return gaps


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z][a-z])")


def gap_name(text: str) -> str:
    """A gap name in one style: `_` and camelCase become words (in lower case), runs of
    whitespace one space, trimmed, and the first letter upper case. Plain wording keeps
    its own case otherwise, so "SQL basics" stays as it is."""
    cleaned = " ".join(_CONTROL.sub(" ", text).split())
    if "_" in cleaned or _CAMEL.search(cleaned):
        cleaned = " ".join(_CAMEL.sub(" ", cleaned.replace("_", " ")).split()).lower()
    return cleaned[:1].upper() + cleaned[1:]


def _entry_level(content: str, posting: PostingText) -> bool:
    """True when the reply's `entry_level` quote is MIN_QUOTE characters or more and is
    found in the title and description sent. The quote itself is not kept."""
    raw = _json_object(content)
    quote = raw.get("entry_level") if isinstance(raw, dict) else None
    if not isinstance(quote, str):
        return False
    needle = normalized(_CONTROL.sub(" ", quote))
    if len(needle) < MIN_QUOTE:
        return False
    sent = normalized("\n".join(part for part in (posting.title, posting.description) if part))
    return needle in sent


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


def _requirements(
    content: str,
    gaps: list[_Gap],
    resume_text: str | None,
    *,
    limit: int | None = MAX_REQUIREMENTS,
) -> tuple[str, ...]:
    """Up to `limit` checked key requirements from the reply; () when there are none."""
    raw = _json_object(content)
    items = raw.get("key_requirements") if isinstance(raw, dict) else None
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
        if limit is not None and len(kept) == limit:
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
    gaps: list[_Gap], rubric: Mapping[str, Any], *, entry_level: bool = False
) -> tuple[list[_Gap], str | None, bool]:
    """The gaps that count, the tier, and whether the core gaps exclude the posting.

    In code, never sent to the model (`fit_rubric.requirement_tiers`):
    - `exclude_at_core` or more core gaps: excluded (a `gap_tier` skip);
    - two or more core gaps, or a core gap in the role's main function: low;
    - one other core gap: medium;
    - no core gap: high with at most `high_max_minor` minor gaps (plus
      `entry_level_extra_minor` when the posting is grounded as entry level), else
      medium. Only minor gaps never make a posting low.
    """
    exempt = {_fold(item) for item in _strings(rubric.get("exempt_types"))}
    counted = [gap for gap in gaps if not (gap.type and _fold(gap.type) in exempt)]
    core = [gap for gap in counted if gap.kind == "core"]
    minor = len(counted) - len(core)
    if len(core) >= rubric["exclude_at_core"]:
        return counted, None, True
    if len(core) >= 2 or any(gap.main_function for gap in core):
        return counted, "low", False
    if core:
        return counted, "medium", False
    allowed = rubric["high_max_minor"] + (rubric["entry_level_extra_minor"] if entry_level else 0)
    return counted, "high" if minor <= allowed else "medium", False


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
