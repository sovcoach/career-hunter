"""Allowed values for enumerated columns.

These mirror the CHECK constraints in migrations/0001_init.sql. The database is the
source of truth; these tuples exist so the seed importer can give friendly errors
before anything is written.
"""

from __future__ import annotations

SOURCES = ("grok", "watcher", "discovery", "manual")
ATS_TYPES = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "successfactors",
    "icims",
    "taleo",
    "smartrecruiters",
    "jobvite",
    "bamboohr",
    "other",
)
WORK_MODELS = ("onsite", "hybrid", "remote")
DATE_PRECISIONS = ("exact", "week", "unknown")
METHODS = (
    "company_site",
    "linkedin_easy_apply",
    "email",
    "referral",
    "agency",
    "recruiter_inbound",
    "other",
)
STATUSES = (
    "applied",
    "screening",
    "assessment",
    "interview",
    "offer",
    "rejected",
    "no_response",
    "withdrawn",
)
ACTIVITY_TYPES = (
    "recruiter_contact",
    "networking",
    "agency_registration",
    "job_fair",
    "workshop",
    "course",
    "other",
)

METHOD_LABELS = {
    "company_site": "Company site",
    "linkedin_easy_apply": "LinkedIn Easy Apply",
    "email": "Email",
    "referral": "Referral",
    "agency": "Agency",
    "recruiter_inbound": "Recruiter inbound",
    "other": "Other",
}


def normalize(value: str) -> str:
    """Turn a human label like "LinkedIn Easy Apply" into its stored form."""
    return "_".join(value.strip().lower().replace("-", " ").split())
