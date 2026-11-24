"""Where a posting is, as far as its text says (Batch R).

A location is *unknown* when it gives no place to check against the location rules:

- it is blank;
- it names only a country ("Japan", "U.S.A."), with no city, and neither the
  location, the title nor the board says the job is remote or hybrid;
- it names only a building ("Example Tower", as Workday sends some), with no city:
  one part (no comma, semicolon, slash, pipe or " - "), with a building word.

An unknown location is never a reason to skip a posting: it is scored and alerted
as usual (`filters._location`). A country-only or building-only location is shown in
the alert as it was given; a blank one as `Location unknown`.

At intake a location is read, in order, from the page's JSON-LD (`jobLocation`, with
`jobLocationType: TELECOMMUTE` read as Remote and `applicantLocationRequirements` as
the country it names), then the ATS's JSON (the same request the job description
already makes), then a `Location:` line in the job description (`from_text`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from careerhunter.countries import is_country

MAX_LOCATION = 120  # characters kept from a location found in text or given by hand
log = logging.getLogger(__name__)

# Whole words that name a building, not a place. Words that are also parts of town
# names ("Park", "Place", "Square") are left out, so a town is never taken for one.
_BUILDING = re.compile(
    r"\b(?:tower|towers|building|bldg|centre|center|plaza|campus|complex|headquarters|hq"
    r"|offices?)\b",
    re.IGNORECASE,
)
_PARTS = re.compile("[,;/|]|\\s[-\u2013\u2014]\\s")
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
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


def country_only(location: str) -> bool:
    return is_country(location)


def building_only(location: str) -> bool:
    text = location.strip()
    if not text or _PARTS.search(text) or is_country(text):
        return False
    return _BUILDING.search(text) is not None and len(text.split()) <= 6


def stated_remote_or_hybrid(location: str, title: str, work_model: str | None) -> bool:
    if work_model in ("remote", "hybrid"):
        return True
    return any(says_remote(t) or _HYBRID.search(t) for t in (location, title))


# What is left of a bare "Remote" once the word is gone: separators, brackets, dashes.
_NOT_A_PLACE = re.compile("[\\s,;/|()\\[\\].:\u2013\u2014-]*")
REGION_UNKNOWN = "Remote (region unknown)"  # how the alert shows a bare Remote (R2)


def bare_remote(location: str) -> bool:
    """A location that says Remote and names no place: "Remote", "REMOTE (", "Remote -".
    Its region is unknown, so the location rules never drop it (R2)."""
    if not says_remote(location):
        return False
    return _NOT_A_PLACE.fullmatch(_REMOTE.sub("", location)) is not None


def unknown(location: str, *, title: str = "", work_model: str | None = None) -> bool:
    """Whether the location gives no place the location rules can check."""
    if not location.strip():
        return True
    if building_only(location) or bare_remote(location):
        return True
    return country_only(location) and not stated_remote_or_hybrid(location, title, work_model)


# Regions a location from free text must name to be kept (Batch R, 0.11.1): Canada's
# provinces and territories and the US states and DC, by name, or by postal
# abbreviation after a comma ("Example City, AB").
_REGION_NAMES = frozenset(
    name.casefold()
    for name in (
        "Alberta", "British Columbia", "Manitoba", "New Brunswick", "Newfoundland",
        "Newfoundland and Labrador", "Nova Scotia", "Northwest Territories", "Nunavut",
        "Ontario", "Prince Edward Island", "Quebec", "Qu\u00e9bec", "Saskatchewan", "Yukon",
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
        "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
        "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri",
        "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
        "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
        "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee",
        "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin",
        "Wyoming",
    )
)  # fmt: skip
_REGION_CODES = frozenset(
    (
        *("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"),
        *("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID"),
        *("IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO"),
        *("MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA"),
        *("RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"),
    )
)
_PART = re.compile(r"[,;/|()\[\]]|\s[-\u2013\u2014]\s")


def configured_places(config: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The places named in the config's `search.location_rules` (`onsite_hybrid` and
    `remote`), as they are written there. Read at runtime, never built in."""
    search = config.get("search") if isinstance(config, Mapping) else None
    rules = search.get("location_rules") if isinstance(search, Mapping) else None
    if not isinstance(rules, Mapping):
        return ()
    found: list[str] = []
    for key in ("onsite_hybrid", "remote"):
        values = rules.get(key)
        for value in values if isinstance(values, list) else []:
            if isinstance(value, str) and value.strip():
                found.append(value)
    return tuple(found)


def _folded(text: str) -> str:
    return " ".join(text.split()).casefold()


def looks_like_place(text: str, known: Sequence[str] = ()) -> bool:
    """Whether location text read from a job description names a place: it says
    Remote, or one of its parts (split at commas, semicolons, slashes, brackets and
    spaced dashes) is a country from the built-in list or a Canadian province or
    territory or US state by name, or a postal abbreviation of one stands after a
    comma, or the whole text is one of `known` (the places in the config's location
    rules, `configured_places`), ignoring case and spacing. At most MAX_LOCATION
    characters."""
    line = " ".join(text.split())
    if not line or len(line) > MAX_LOCATION:
        return False
    if says_remote(line):
        return True
    if _folded(line.strip(" .")) in {_folded(place) for place in known}:
        return True
    parts = [p.strip(" .") for p in _PART.split(line)]
    if any(p and (is_country(p) or p.casefold() in _REGION_NAMES) for p in parts):
        return True
    after_comma = [p.strip(" .") for p in line.split(",")[1:]]
    return any(p.split(" ")[0] in _REGION_CODES for p in after_comma if p)


def read_text(text: str | None, known: Sequence[str] = ()) -> tuple[str | None, str | None]:
    """A `Location:` (or `Based in:`) line of a job description: (the place, None) when
    it looks like a place (`looks_like_place`), else (None, the line as found) so the
    caller can say it was left unknown. (None, None) when there is no such line."""
    if not text:
        return None, None
    match = _LINE.search(text)
    if match is None:
        return None, None
    raw = " ".join(match.group("place").split()).strip(" ,;.-")
    if not raw:
        return None, None
    if looks_like_place(raw, known):
        return raw, None
    return None, raw[:MAX_LOCATION]


def from_text(text: str | None, known: Sequence[str] = ()) -> str | None:
    """The place a `Location:` (or `Based in:`) line of a job description names, when it
    looks like a place (`looks_like_place`); otherwise None, and the location stays
    unknown."""
    place, rejected = read_text(text, known)
    if rejected is not None:
        log.info("a location line in a job description is not a place; left unknown")
    return place


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
