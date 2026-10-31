"""Allowed values for enumerated columns.

These mirror the CHECK constraints in the migrations under migrations/search/ and
migrations/ei_record/. The databases are the source of truth; these tuples exist so the
seed importer can give friendly errors before anything is written.
"""

from __future__ import annotations

SOURCES = ("grok", "watcher", "discovery", "manual", "self_sourced")
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
# The status pipeline, shared by the EI status history and the search decision history.
STATUSES = (
    "applied",
    "screening",
    "assessment_submitted",
    "interview_scheduled",
    "offer",
    "hired",  # offer accepted
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
