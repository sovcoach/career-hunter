"""Required credentials (batch R2): a certification, licence or clearance the posting
requires and the resume doesn't show is a core gap, whatever years or `core` the reply
attached. Experience years keep their own handling. Synthetic postings and replies only."""

from __future__ import annotations

import json

import pytest

from careerhunter import score as scoring
from tests.fakes import requirement
from tests.test_score import _titled


def _gaps(title: str, *items: dict[str, object]) -> tuple[list[dict[str, str]], str | None]:
    result = _titled(title, *items)
    return json.loads(result.gaps), result.tier


@pytest.mark.parametrize(
    ("name", "type"),
    [
        ("CompTIA A+ certification", "certification"),
        ("Microsoft 365 Endpoint Administrator", "certification"),  # by type
        ("Valid driver's licence", "experience"),  # by name
        ("Driver's license and reliable vehicle", "skill"),
        ("Security clearance eligibility", "skill"),
        ("First aid certificate", "other"),
        ("Class 5 licence", "licence"),
    ],
)
def test_a_required_credential_is_core_whatever_the_reply_says(name: str, type: str) -> None:
    gaps, tier = _gaps("IT Support Specialist", requirement(name, type=type, core=False))
    assert [gap["kind"] for gap in gaps] == ["core"]
    assert tier == "medium"


def test_years_attached_to_a_required_credential_are_ignored() -> None:
    one = requirement("CompTIA A+ certification", type="certification", years=1)
    gaps, tier = _gaps("IT Support Specialist", one)
    assert gaps == [{"name": "CompTIA A+ certification", "kind": "core", "type": "certification"}]
    assert tier == "medium"  # before: 1 year made it a minor gap, so High
    five = requirement("Professional engineering licence", type="licence", years=5)
    gaps, tier = _gaps("Analyst", five)
    assert gaps[0]["type"] == "licence" and tier == "medium"  # an ordinary core gap, no seniority


def test_a_junior_title_keeps_a_required_credential_with_years() -> None:
    item = requirement("Network+ certification", type="certification", years=2)
    gaps, tier = _gaps("Junior Network Technician", item)
    assert [gap["kind"] for gap in gaps] == ["core"] and tier == "medium"


def test_a_credential_marked_main_function_does_not_force_low() -> None:
    item = requirement("CCNA certification", type="certification", main_function=True)
    assert _gaps("Network Analyst", item)[1] == "medium"


def test_a_preferred_or_partly_met_credential_stays_minor() -> None:
    preferred = requirement("CompTIA A+", "preferred", type="certification")
    partly = requirement("Security+ certification", type="certification", met="partly")
    with_years = requirement("ITIL certification", "preferred", type="certification", years=2)
    gaps, tier = _gaps("IT Support Specialist", preferred, partly, with_years)
    assert [gap["kind"] for gap in gaps] == ["minor", "minor", "minor"]
    assert tier == "medium"  # three minor gaps: over high_max_minor


@pytest.mark.parametrize(
    ("name", "type"),
    [
        # the wording of a SaaS administration duty and of a document-review duty
        ("License administration", "skill"),
        ("User, group, permission, license administration", "skill"),
        ("SaaS license management", "tool"),
        ("Software license tracking", "licence"),
        ("Reviewing business licenses and IDs", "experience"),
        ("Verifying licenses and proof of address", "skill"),
        ("Business license review", "certification"),
        ("Licensing boards and public records", "skill"),
    ],
)
def test_a_licence_the_job_administers_or_reviews_is_not_a_credential(name: str, type: str) -> None:
    item = requirement(name, type=type, core=False)
    assert not scoring.required_credential(scoring._parse_requirements(_one(item))[0])
    gaps, tier = _gaps("IT Coordinator", item)
    assert [gap["kind"] for gap in gaps] == ["minor"] and tier == "high"


def test_experience_years_keep_todays_handling() -> None:
    five = requirement("5+ years of IT support", type="experience", years=5)
    gaps, tier = _gaps("IT Support Specialist", five)
    assert gaps == [{"name": "5+ years of IT support", "kind": "core", "type": "seniority"}]
    assert tier == "low"
    two = requirement("2+ years of IT support", type="experience", years=2, core=True)
    assert _gaps("IT Support Specialist", two) == (
        [{"name": "2+ years of IT support", "kind": "minor", "type": "experience"}],
        "high",
    )
    assert _gaps("Junior IT Support Specialist", two) == ([], "high")


def _one(item: dict[str, object]) -> str:
    return json.dumps({"gaps": [item], "entry_level": ""})
