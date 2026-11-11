"""Canonical posting URLs, and the hosts that are never fetched.

`canonical(url)` is the form an application's link is stored and compared in:

- the scheme and host are lowercased, and the fragment is dropped;
- tracking parameters are dropped (`utm_*`, LinkedIn's `trk`, `refId`, `trackingId` and
  the like, ad-click ids such as `gclid` and `fbclid`, mail-campaign ids, `gh_src`).
  Every other parameter is kept as it was written, in its order, because some identify
  the posting (Greenhouse's `gh_jid`, for one);
- a LinkedIn job link, `/jobs/view/<slug>-<id>…` or any LinkedIn page with
  `?currentJobId=<id>`, becomes `https://www.linkedin.com/jobs/view/<id>/`.

It is idempotent: `canonical(canonical(u)) == canonical(u)`. The watcher's URLs are not
passed through it; links already stored are left as they were and compared canonically.

`no_fetch(url)` is true for a host whose pages are never fetched: LinkedIn only, for
now (`linkedin.com`, any subdomain of it, and `lnkd.in`). `fetch.py` refuses those hosts
for every caller, on the first URL and on every redirect hop.
"""

from __future__ import annotations

import re
import urllib.parse

# Parameter names compared case-insensitively.
TRACKING_PARAMS = frozenset(
    name.casefold()
    for name in (
        "trk",
        "trkInfo",
        "refId",
        "trackingId",
        "lipi",
        "eBP",
        "gclid",
        "gclsrc",
        "dclid",
        "msclkid",
        "fbclid",
        "igshid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "gh_src",
    )
)
TRACKING_PREFIXES = ("utm_",)

NO_FETCH_DOMAINS = ("linkedin.com",)  # the domain and any subdomain of it
NO_FETCH_HOSTS = frozenset({"lnkd.in"})

_LINKEDIN_VIEW = re.compile(r"^/(?:[a-z]{2}/)?jobs/view/(?:[^/]*?-)?(?P<id>\d+)(?:/.*)?$")
_LINKEDIN_JOB_ID = re.compile(r"^\d+$")


def _is_tracking(name: str) -> bool:
    folded = urllib.parse.unquote_plus(name).casefold()
    return folded in TRACKING_PARAMS or folded.startswith(TRACKING_PREFIXES)


def _host(netloc: str) -> str:
    """The host of a netloc, without user info or port, lowercased."""
    host = netloc.rpartition("@")[2]
    if host.startswith("["):
        return host.partition("]")[0].lower() + "]"
    return host.partition(":")[0].lower().rstrip(".")


def no_fetch_host(host: str) -> bool:
    """True for a host whose pages are never fetched (LinkedIn only, for now)."""
    host = host.lower().rstrip(".")
    if host in NO_FETCH_HOSTS:
        return True
    return any(host == d or host.endswith("." + d) for d in NO_FETCH_DOMAINS)


def no_fetch(url: str) -> bool:
    """True when `url`'s host is never fetched."""
    try:
        host = urllib.parse.urlsplit(url.strip()).hostname
    except ValueError:
        return False
    return host is not None and no_fetch_host(host)


def _is_linkedin(host: str) -> bool:
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _linkedin_job(path: str, pieces: list[str]) -> str | None:
    view = _LINKEDIN_VIEW.match(path)
    if view:
        return view["id"]
    for piece in pieces:
        name, _, value = piece.partition("=")
        if urllib.parse.unquote_plus(name) == "currentJobId":
            value = urllib.parse.unquote_plus(value)
            if _LINKEDIN_JOB_ID.match(value):
                return value
    return None


def canonical(url: str) -> str:
    """The canonical form of a posting link (see the module docstring)."""
    parts = urllib.parse.urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc
    userinfo, at, hostport = netloc.rpartition("@")
    netloc = userinfo + at + hostport.lower()
    pieces = [p for p in parts.query.split("&") if p]
    host = _host(netloc)
    if scheme in ("http", "https") and _is_linkedin(host):
        job = _linkedin_job(parts.path, pieces)
        if job is not None:
            return f"https://www.linkedin.com/jobs/view/{job}/"
    kept = [p for p in pieces if not _is_tracking(p.partition("=")[0])]
    return urllib.parse.urlunsplit((scheme, netloc, parts.path, "&".join(kept), ""))
