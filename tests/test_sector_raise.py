"""The sector raise (Q11, ADR-032): a support or noc_network posting at a watchlist company
whose sector is listed goes up one tier, never above high, never off-lane, vetoed,
excluded or over a cap. Synthetic companies, postings and replies only."""

from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any

import pytest

from careerhunter import lanes, search_store
from careerhunter.config import SECTORS, validate_config, watchlist_sector
from careerhunter.filters import PostingText
from careerhunter.score import Cap, Score, Veto, _raised, alerts, score_posting, sector_raise
from careerhunter.search_store import PendingAlert
from tests.fakes import fit_reply, requirement
from tests.test_criteria_veto_watch import SLUG, _boards, _logged
from tests.test_rescore import OLD, Recorder, run
from tests.test_score import RUBRIC, reply

LANES = [
    {"name": "support", "priority": 1, "group": "stopgap", "titles": ["IT Support Specialist"]},
    {"name": "noc_network", "priority": 2, "group": "stopgap", "titles": ["NOC Analyst"]},
    {"name": "operations", "priority": 3, "titles": ["Operations Coordinator"]},
]


def _doc(sector: object = "mssp", company: str = "Example Shield") -> dict[str, Any]:
    doc = copy.deepcopy(RUBRIC)
    doc["search"] = {"target_titles": ["IT Support Specialist"], "lanes": LANES}
    entry: dict[str, Any] = {"name": company, "ats_type": "lever", "ats_slug": "exampleshield"}
    if sector is not None:
        entry["sector"] = sector
    doc["watchlist"] = [entry]
    return doc


def _posting(title: str = "IT Support Specialist", company: str = "Example Shield") -> PostingText:
    return PostingText(company, title, "Example City", description="A synthetic posting.")


def _score(tier: str | None, **fields: Any) -> Score:
    return Score(llm_output="{}", gaps="[]", tier=tier, **fields)


# --- config -------------------------------------------------------------------------------


@pytest.mark.parametrize("sector", sorted(SECTORS))
def test_each_listed_sector_is_accepted(sector: str) -> None:
    assert not [p for p in validate_config(_doc(sector)) if "sector" in p]


@pytest.mark.parametrize("sector", ["other", "", "MSSP", 1, ["mssp"]])
def test_a_sector_not_listed_is_refused(sector: object) -> None:
    assert "watchlist[0].sector must be one of" in " ".join(validate_config(_doc(sector)))


def test_a_null_or_missing_sector_is_accepted_and_means_none() -> None:
    for doc in (_doc(None), {**_doc(), "watchlist": [{"name": "Example Shield", "sector": None}]}):
        assert not [p for p in validate_config(doc) if "sector" in p]
        assert watchlist_sector(doc, "Example Shield") is None


def test_the_sector_is_found_by_company_name_ignoring_case() -> None:
    assert watchlist_sector(_doc(), "example shield") == "mssp"
    assert watchlist_sector(_doc(), "Example Other") is None
    assert watchlist_sector({}, "Example Shield") is None


# --- which postings it applies to ---------------------------------------------------------


@pytest.mark.parametrize("title", ["IT Support Specialist", "NOC Analyst"])
def test_support_and_noc_postings_at_a_listed_sector_get_it(title: str) -> None:
    assert sector_raise(_posting(title), _doc("security_vendor")) == "security_vendor"


def test_another_lane_off_lane_or_no_sector_gets_none() -> None:
    assert sector_raise(_posting("Operations Coordinator"), _doc()) is None
    assert sector_raise(_posting("Example Gardener"), _doc()) is None
    assert sector_raise(_posting(), _doc(None)) is None
    assert sector_raise(_posting(company="Example Other"), _doc()) is None


# --- the raise ----------------------------------------------------------------------------


def test_low_goes_to_medium_and_medium_to_high() -> None:
    low = _raised(_score("low"), "mssp")
    medium = _raised(_score("medium"), "mssp")
    assert low is not None and (low.tier, low.raised_from, low.sector) == ("medium", "low", "mssp")
    assert medium is not None and (medium.tier, medium.raised_from) == ("high", "medium")


def test_high_stays_high_and_still_names_the_sector() -> None:
    high = _raised(_score("high"), "fintech")
    assert high is not None and (high.tier, high.raised_from, high.sector) == (
        "high",
        None,
        "fintech",
    )


def test_no_sector_leaves_the_score_as_it_was() -> None:
    plain = _score("low")
    assert _raised(plain, None) is plain
    assert _raised(None, "mssp") is None


def test_a_vetoed_or_excluded_posting_is_never_raised() -> None:
    vetoed = _score("low", veto=Veto(5, skip="criteria: 5. Example"))
    excluded = _score(None, excluded=True)
    assert _raised(vetoed, "mssp") is vetoed
    assert _raised(excluded, "mssp") is excluded


def test_the_raise_never_lifts_a_posting_above_a_cap() -> None:
    capped = _raised(_score("low", cap=Cap(2, level="low")), "mssp")
    assert capped is not None and capped.tier == "medium" and capped.capped == "low"
    assert alerts(capped) is False  # the cap still holds it at low
    held = _raised(_score("medium", cap=Cap(2, level="medium")), "mssp")
    assert held is not None and held.tier == "high" and alerts(held) is True


def test_score_posting_applies_the_raise_and_never_sends_the_sector() -> None:
    sent: list[bytes] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        sent.append(payload)
        return reply(
            fit_reply(items=(requirement("Example function", core=True, main_function=True),))
        )

    result = score_posting(_posting(), _doc(), transport=transport, sector="mssp")
    assert result is not None and (result.tier, result.raised_from) == ("medium", "low")
    assert alerts(result) is True
    view = json.loads(json.loads(sent[0])["messages"][1]["content"])
    assert "mssp" not in json.dumps(view) and "sector" not in json.dumps(view)
    assert "mssp" not in sent[0].decode()


def test_voting_counts_the_raised_tier() -> None:
    low = fit_reply(items=(requirement("Example function", core=True, main_function=True),))
    calls: list[int] = []

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        calls.append(json.loads(payload)["seed"])
        return reply(low)

    doc = _doc()
    doc["scoring"] = {"voting": True}
    result = score_posting(_posting(), doc, transport=transport, sector="mssp")
    assert result is not None and result.tier == "medium" and alerts(result) is True
    assert len(calls) == 2  # two raised draws agree on alerting


# --- the alert row ------------------------------------------------------------------------


def _pending(lane: str, sector: str | None) -> PendingAlert:
    return PendingAlert(1, "medium", lane, True, "2026-11-23", sector=sector)


def test_the_row_names_the_sector_in_words() -> None:
    (row,) = lanes.alert_order([_pending("support", "security_vendor")], _doc())
    assert row.lane_line == "Lane: support (stopgap, security vendor)"
    (row,) = lanes.alert_order([_pending("noc_network", "mssp")], _doc())
    assert row.lane_line == "Lane: noc_network (stopgap, MSSP)"


def test_a_row_without_a_sector_is_unchanged() -> None:
    (row,) = lanes.alert_order([_pending("support", None)], _doc())
    assert row.lane_line == "Lane: support (stopgap)"


def test_a_primary_lane_row_shows_the_sector_alone() -> None:
    (row,) = lanes.alert_order([_pending("operations", "it_consultancy")], _doc())
    assert row.lane_line == "Lane: operations (IT consultancy)"


# --- the watcher stores it ----------------------------------------------------------------


def test_the_watcher_stores_the_raise_and_logs_it(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = _score("medium", sector="mssp", raised_from="low")
    lines = run(
        search,
        _boards(("r1", "Coordinator")),
        (SLUG,),
        scorer=scorer,
        resume=OLD,
        config={"fit_rubric": {"requirement_tiers": {}}},
    )
    fit, meta = search.execute("SELECT fit, scout_metadata FROM postings").fetchone()
    assert fit == "medium"
    assert json.loads(meta) == {"tier": "medium", "sector": "mssp", "raised_from": "low"}
    assert _logged(lines, ": 'Coordinator' raised from low to medium (sector mssp)")
    (pending,) = search_store.pending_alerts(search)
    assert pending.sector == "mssp"
