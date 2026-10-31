"""The records career-hunter writes, as plain values.

These describe what happened (a posting seen, an application sent, an activity done)
without saying where it is stored. The ledger decides which layer gets which fields:
search.db gets all of an Application, and the EI record gets only the sanitized subset
in ei_store.EiApplication.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dating:
    """A date with honest precision: exact (day), week (week_of, a Monday) or unknown."""

    precision: str
    day: str | None = None
    week_of: str | None = None

    @property
    def sort_date(self) -> str | None:
        return self.day or self.week_of


@dataclass(frozen=True)
class Posting:
    company: str
    title: str
    first_seen: str
    location: str = ""
    work_model: str | None = None
    schedule: str | None = None
    url: str | None = None
    requisition_id: str | None = None
    source: str = "manual"
    fit_rating: int | None = None
    gaps: str | None = None
    llm_output: str | None = None
    scout_metadata: str | None = None
    snapshot_path: str | None = None


@dataclass(frozen=True)
class Application:
    """The full decision to apply, as search.db records it."""

    posting: Posting
    dating: Dating
    method: str
    resume_file: str | None = None
    reasoning: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Activity:
    dating: Dating
    type: str
    description: str
    related_company: str | None = None
