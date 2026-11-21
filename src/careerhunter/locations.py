"""Where a posting is, as far as its text says (Batch R).

A location is *unknown* when it is blank: it gives no place to check against the
location rules. An unknown location is never a reason to skip a posting: it is scored
and alerted as usual (`filters._location`), and its alert says `Location unknown`.

At intake a location is read, in order, from the page's JSON-LD (`jobLocation`, with
`jobLocationType: TELECOMMUTE` read as Remote and `applicantLocationRequirements` as
the country it names), then the ATS's JSON (the same request the job description
already makes), then a `Location:` line in the job description (`from_text`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MAX_LOCATION = 120  # characters kept from a location found in text or given by hand

_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
# A line that states the job's location: "Location: ...", "Job location - ...",
# "Based in: ...". Only at the start of a line, so a sentence never counts.
_LINE = re.compile(
    r"^[ \t]*(?:job[ \t]+|work[ \t]+|primary[ \t]+)?(?:locations?|based[ \t]+in)"
    "(?:[ \t]*:|[ \t]+[-\u2013\u2014])[ \t]*"
    r"(?P<place>[^\n]{2,})$",
    re.IGNORECASE | re.MULTILINE,
)


def _clean(text: str) -> str:
    return " ".join(text.split())[:MAX_LOCATION].strip(" ,;.-")


def says_remote(text: str) -> bool:
    """The whole word "remote", ignoring case."""
    return _REMOTE.search(text) is not None


def unknown(location: str, *, title: str = "", work_model: str | None = None) -> bool:
    """Whether the location gives no place the location rules can check."""
    del title, work_model  # read by the rules for a location that names no place
    return not location.strip()


def from_text(text: str | None) -> str | None:
    """The place a `Location:` (or `Based in:`) line of a job description names, or None."""
    if not text:
        return None
    match = _LINE.search(text)
    if match is None:
        return None
    place = _clean(match.group("place"))
    return place or None


def _text(value: Any) -> str | None:
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def _countries(value: Any) -> list[str]:
    """Country names in JSON-LD `applicantLocationRequirements` (a Country, or a list)."""
    items = value if isinstance(value, list) else [value]
    found: list[str] = []
    for item in items:
        name = _text(item.get("name")) if isinstance(item, Mapping) else _text(item)
        if name and name not in found:
            found.append(name)
    return found


def from_json_ld(job: Mapping[str, Any]) -> str | None:
    """A JSON-LD `JobPosting`'s location: its address's locality and region, then
    "Remote" for `jobLocationType: TELECOMMUTE`, then the countries an applicant must be
    in. None when it gives none of these."""
    place = job.get("jobLocation")
    place = place[0] if isinstance(place, list) and place else place
    address = place.get("address") if isinstance(place, Mapping) else None
    bits: list[str] = []
    if isinstance(address, Mapping):
        bits = [
            b for b in (_text(address.get(k)) for k in ("addressLocality", "addressRegion")) if b
        ]
    kinds = job.get("jobLocationType")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    remote = any(isinstance(k, str) and k.strip().upper() == "TELECOMMUTE" for k in kinds)
    countries = _countries(job.get("applicantLocationRequirements")) if remote else []
    if remote:
        bits = ["Remote", *countries, *bits]
    if not bits and isinstance(address, Mapping):
        country = address.get("addressCountry")
        named = _text(country.get("name")) if isinstance(country, Mapping) else _text(country)
        bits = [named] if named else []
    found = ", ".join(dict.fromkeys(bits))
    return _clean(found) or None


def with_work_model(location: str, work_model: str | None) -> str:
    """The location with "Remote" in front when the board says remote and the text
    doesn't, so the location rules see it."""
    if work_model == "remote" and not says_remote(location):
        return _clean(f"Remote, {location}" if location.strip() else "Remote")
    return location
