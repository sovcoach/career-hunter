"""One normalization for job titles and employer names.

- `canonical(title)` is `filters.title_key` (NFKC, case folding, requisition codes
  dropped, punctuation runs as one space), with one word-order change first worked out
  on the raw title: comma inversion. `Head, Tail` becomes `Tail Head` only when the
  title has exactly one comma, the head ends in a role word, and the tail has no role
  word, so "Analyst, Client Experience" is "client experience analyst". Level words
  anywhere in the head are set aside first and kept at the start or end, so "Senior
  Analyst, Client Experience" is "senior client experience analyst". There is no
  other reordering, and no bag of words.
- `employer_key(name)` folds a company name the same way, drops a leading "The" and
  drops trailing legal suffixes (Inc, Ltd, LLC, Corp, Corporation, Limited, Co), with
  their punctuation: "The Example Robotics Co., Ltd." is "example robotics".
- `equivalent(a, b)` is true when two canonical titles differ only by level words at
  the start or end (junior, senior, I to IV, 1 to 4, "level N", ...) or by role words
  from the one built-in group (specialist, analyst, associate). "Lead", "manager",
  "principal" and "head" are not level words.

The import (`backfill.py`) matches on `employer_key` and `canonical`, and lists titles
that are only `equivalent` for the operator to resolve. Lane matching (`lanes.match`)
tries `loose_match` when no lane title matches literally.
"""

from __future__ import annotations

import re
import unicodedata

from careerhunter.filters import title_key

# The one built-in group of role words that count as the same role. Not configurable.
ROLE_WORDS = frozenset({"specialist", "analyst", "associate"})

# Level words, as they are after `title_key` ("Sr." is "sr", "(Level 2)" is "level 2").
LEVEL_WORDS = frozenset(
    {"junior", "jr", "intermediate", "senior", "sr", "i", "ii", "iii", "iv", "1", "2", "3", "4"}
)
_LEVEL_PAIRS = (re.compile(r"entry level"), re.compile(r"level \d+"))

LEGAL_SUFFIXES = frozenset({"inc", "ltd", "llc", "corp", "corporation", "limited", "co"})

_NOT_WORD = re.compile(r"[\W_]+")


def _fold(text: str) -> str:
    """NFKC, case, then NFKC again (casefolding can leave a string not normalized)."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())


def _words(text: str) -> list[str]:
    return text.split()


def _inverted(title: str) -> str | None:
    """`Tail Head` for a `Head, Tail` title that qualifies, as a title_key; else None.

    Level words anywhere in the head are set aside first, so the head's other words are
    what must end in a role word. Those before the head's first other word go in front
    of the inverted title, and the rest at its end: "Senior Analyst II, Client
    Experience" is "senior client experience analyst ii".
    """
    if title.count(",") != 1:
        return None
    head, tail = title.split(",")
    head_key, tail_key = title_key(head), title_key(tail)
    if not head_key or not tail_key:
        return None
    if title_key(title) == head_key:
        return None  # the tail was a requisition code, which title_key drops
    front, role, end = _split_levels(_words(head_key))
    if not role or role[-1] not in ROLE_WORDS:
        return None
    if any(word in ROLE_WORDS for word in _words(tail_key)):
        return None
    return " ".join([*front, tail_key, *role, *end])


def _split_levels(words: list[str]) -> tuple[list[str], list[str], list[str]]:
    """The level words before the first other word, the other words, and the rest."""
    front: list[str] = []
    role: list[str] = []
    end: list[str] = []
    index = 0
    while index < len(words):
        if index + 1 < len(words) and _is_level_pair(words[index : index + 2]):
            size = 2
        elif _is_level_word(words[index : index + 1]):
            size = 1
        else:
            role.append(words[index])
            index += 1
            continue
        (end if role else front).extend(words[index : index + size])
        index += size
    return front, role, end


def canonical(title: str) -> str:
    """The title as one comparable form: comma inversion, then `title_key`, folded."""
    return _fold(_inverted(title) or title_key(title))


def employer_key(name: str) -> str:
    """The company name folded, without a leading "The" or trailing legal suffixes."""
    folded = _fold(name).replace(".", "")  # "L.L.C." is "llc", "Co." is "co"
    words = _words(_NOT_WORD.sub(" ", folded))
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words = words[:-1]
    return " ".join(words)


def _strip_levels(words: list[str]) -> list[str]:
    """Level words dropped from the start and the end, keeping at least one word."""
    changed = True
    while changed and len(words) > 1:
        changed = False
        for size, test in ((2, _is_level_pair), (1, _is_level_word)):
            if len(words) > size and test(words[:size]):
                words, changed = words[size:], True
                break
            if len(words) > size and test(words[-size:]):
                words, changed = words[:-size], True
                break
    return words


def _is_level_word(words: list[str]) -> bool:
    return words[0] in LEVEL_WORDS


def _is_level_pair(words: list[str]) -> bool:
    joined = " ".join(words)
    return any(pattern.fullmatch(joined) for pattern in _LEVEL_PAIRS)


def core_words(title: str) -> list[str]:
    """The canonical title without its level words, with every role word as `analyst`."""
    words = _strip_levels(_words(canonical(title)))
    return ["analyst" if word in ROLE_WORDS else word for word in words]


def equivalent(a: str, b: str) -> bool:
    """True when the titles differ only by level words or by role words of the group."""
    return core_words(a) == core_words(b)


# Where a posting title splits into segments for loose lane matching: a comma, a dash
# with a space on at least one side, an en or em dash, or a bracket.
_SEGMENT_BREAK = re.compile(",|\\s+-+\\s*|-+\\s+|[\u2013\u2014]|[()\\[\\]{}]")


def lane_core(variant: str) -> tuple[str, ...] | None:
    """The core of a lane title variant that may match loosely, or None.

    The variant, canonical and without level words, must be a core of two or more
    words followed by one role word of the group. A one-word core never matches loosely.
    """
    words = _strip_levels(_words(canonical(variant)))
    if len(words) < 3 or words[-1] not in ROLE_WORDS:
        return None
    return tuple(words[:-1])


def _segments(title: str) -> list[list[str]]:
    """The posting title's segments as words, each without its level words.

    A title that comma inversion applies to is one segment, its inverted form.
    """
    inverted = _inverted(title)
    parts = [inverted] if inverted is not None else _SEGMENT_BREAK.split(title)
    segments = []
    for part in parts:
        words = _strip_levels(_words(_fold(title_key(part))))
        if words:
            segments.append(words)
    return segments


def loose_match(title: str, variant: str) -> bool:
    """True when a posting title matches a lane title variant by the normalized rule.

    The posting must contain the variant's core (`lane_core`) as whole words, followed
    at once by a role word of the group, then the end of the title or a separator
    (comma, dash, bracket). Level words at the start or end of a segment are ignored,
    and comma inversion is the only change of word order. So "Senior IT Support
    Associate II" and "IT Support Associate - Night Shift" match "IT Support
    Specialist", and "IT Support Associate Manager" does not.
    """
    core = lane_core(variant)
    if core is None:
        return False
    size = len(core)
    return any(
        len(words) > size and tuple(words[-size - 1 : -1]) == core and words[-1] in ROLE_WORDS
        for words in _segments(title)
    )
