"""The watcher acts on a criteria veto (ADR-028): a skip with its rule, and a withdrawal
for a posting already alerted. Synthetic boards, postings and replies only."""

from __future__ import annotations

import json
import sqlite3

from careerhunter import search_store
from careerhunter.score import Cap, Score, Veto
from tests.test_rescore import NEW, OLD, Recorder, board, lever, row, run, seed

SLUG = "examplerobotics"
SKIP = "criteria: 2. No contract or term under 12"
VETOED = Score(llm_output="{}", gaps="[]", tier=None, veto=Veto(2, skip=SKIP))
REJECTED = Score(llm_output='{"gaps":[]}', gaps="[]", tier="strong", veto=Veto(2, "not-found"))


def _boards(*jobs: tuple[str, str]) -> dict[str, object]:
    return {board(SLUG): lever(SLUG, *jobs)}


def _skips(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    return [
        (str(r[0]), str(r[1]), int(r[2]))
        for r in conn.execute(
            "SELECT p.title, s.reason, s.by_operator FROM skips s "
            "JOIN postings p ON p.id = s.posting_id ORDER BY s.id"
        )
    ]


def _logged(lines: list[str], ending: str) -> bool:
    return any(
        line.startswith("watch: Examplerobotics") and line.endswith(ending) for line in lines
    )


def _outcomes(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT outcome FROM score_calls ORDER BY id")]


def test_a_veto_on_a_new_posting_is_a_skip_and_never_an_alert(
    search: sqlite3.Connection,
) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = VETOED
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    assert _skips(search) == [("Coordinator", SKIP, 0)]
    assert _logged(lines, ": 'Coordinator' skipped (criteria 2)")
    assert search_store.pending_alerts(search) == []
    assert row(search, "Coordinator")["llm_output"] is None  # nothing from the reply stored
    assert _outcomes(search) == ["vetoed"]
    assert search.execute("SELECT count(*) FROM withdrawals").fetchone()[0] == 0


def test_a_veto_on_an_alerted_re_score_is_withdrawn_and_keeps_the_old_score(
    search: sqlite3.Connection,
) -> None:
    seed(search, SLUG, ("r1", "Coordinator"), resume=OLD)
    search.execute("UPDATE postings SET alerted_at = '2026-11-01T10:00:00Z', fit = 'high'")
    search.commit()
    before = search.execute(
        "SELECT llm_output, gaps, fit, scout_metadata, alerted_at FROM postings"
    ).fetchone()
    scorer = Recorder()
    scorer.replies["Coordinator"] = VETOED
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=NEW)
    assert _logged(lines, ": 'Coordinator' skipped (criteria 2)")
    after = search.execute(
        "SELECT llm_output, gaps, fit, scout_metadata, alerted_at FROM postings"
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert _skips(search) == [("Coordinator", SKIP, 0)]
    [pending] = search_store.pending_withdrawals(search, 10)
    assert (pending.company, pending.title, pending.reason) == (
        "Examplerobotics",
        "Coordinator",
        SKIP,
    )
    assert _outcomes(search)[-1] == "vetoed"
    # Skipped now: the next run neither re-scores nor alerts it.
    again = Recorder()
    run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=again, resume=NEW)
    assert again.calls == []


def test_a_rejected_veto_is_scored_as_usual(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = REJECTED
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    assert _skips(search) == []
    assert row(search, "Coordinator")["gaps"] == "[]"
    assert _outcomes(search) == ["veto_rejected"]
    assert _logged(lines, ": 'Coordinator' veto rejected (quote not found, rule 2); scored")
    assert row(search, "Coordinator")["llm_output"] == '{"gaps":[]}'


def test_an_unknown_rule_is_logged_without_a_number(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(
        llm_output="{}", gaps="[]", tier="strong", veto=Veto(None, "unknown-rule")
    )
    lines = run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD)
    assert _logged(lines, ": 'Coordinator' veto rejected (unknown rule); scored")


def test_veto_calls_count_as_answered_in_the_call_stats(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = VETOED
    scorer.replies["Planner"] = REJECTED
    run(
        search,
        _boards(("r1", "Coordinator"), ("r2", "Planner")),
        (SLUG,),
        scorer=scorer,
        resume=OLD,
    )
    stats = search_store.score_call_stats(search, "2000-01-01T00:00:00Z")
    assert (len(stats.answered_ms), stats.not_answered) == (2, 0)


def test_withdrawals_are_marked_sent_once(search: sqlite3.Connection) -> None:
    seed(search, SLUG, ("r1", "Coordinator"), resume=OLD)
    search.execute("UPDATE postings SET alerted_at = '2026-11-01T10:00:00Z'")
    search.commit()
    scorer = Recorder()
    scorer.replies["Coordinator"] = VETOED
    run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=NEW)
    [pending] = search_store.pending_withdrawals(search, 10)
    search_store.mark_withdrawals_sent(search, [pending.skip_id], "2026-11-02T10:00:00Z")
    search_store.mark_withdrawals_sent(search, [pending.skip_id], "2026-11-03T10:00:00Z")
    assert search_store.pending_withdrawals(search, 10) == []
    assert search.execute("SELECT sent_at FROM withdrawals").fetchone()[0] == (
        "2026-11-02T10:00:00Z"
    )


# --- caps (ADR-028) ------------------------------------------------------------------------

FITS = {
    "fit_rubric": {
        "gap_tiers": {
            "tiers": [
                {"name": "strong", "max_gaps": 1, "fit": "high"},
                {"name": "weak", "max_gaps": 3, "fit": "low"},
            ]
        }
    }
}


def _capped(tier: str, level: str = "medium") -> Score:
    return Score(llm_output="{}", gaps="[]", tier=tier, cap=Cap(3, level=level))


def _stored(conn: sqlite3.Connection, title: str) -> tuple[str | None, dict[str, object]]:
    found = conn.execute(
        "SELECT fit, scout_metadata FROM postings WHERE title = ?", (title,)
    ).fetchone()
    return found[0], json.loads(found[1])


def test_a_cap_lowers_a_high_fit_and_keeps_a_lower_one(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = _capped("strong")
    scorer.replies["Planner"] = _capped("weak")
    lines = run(
        search,
        _boards(("r1", "Coordinator"), ("r2", "Planner")),
        (SLUG,),
        scorer=scorer,
        resume=OLD,
        config=FITS,
    )
    assert _stored(search, "Coordinator") == ("medium", {"tier": "strong", "cap_rule": 3})
    assert _stored(search, "Planner") == ("low", {"tier": "weak", "cap_rule": 3})
    assert _logged(lines, ": 'Coordinator' capped at medium (criteria 3)")
    assert _outcomes(search) == ["capped", "capped"]
    assert _skips(search) == []


def test_a_cap_on_an_alerted_re_score_lowers_the_fit_and_withdraws_nothing(
    search: sqlite3.Connection,
) -> None:
    seed(search, SLUG, ("r1", "Coordinator"), resume=OLD)
    search.execute("UPDATE postings SET alerted_at = '2026-11-01T10:00:00Z', fit = 'high'")
    search.commit()
    scorer = Recorder()
    scorer.replies["Coordinator"] = _capped("strong")
    run(
        search,
        _boards(("r1", "Coordinator")),
        (SLUG,),
        scorer=scorer,
        resume=NEW,
        config=FITS,
    )
    assert _stored(search, "Coordinator")[0] == "medium"
    assert search.execute("SELECT count(*) FROM withdrawals").fetchone()[0] == 0
    assert _skips(search) == []


def test_a_rejected_cap_scores_as_usual(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(
        llm_output="{}", gaps="[]", tier="strong", cap=Cap(3, "not-found")
    )
    lines = run(
        search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD, config=FITS
    )
    assert _stored(search, "Coordinator") == ("high", {"tier": "strong"})
    assert _outcomes(search) == ["cap_rejected"]
    assert _logged(lines, ": 'Coordinator' cap rejected (quote not found, rule 3); scored")


def test_a_veto_beats_a_cap_in_the_watcher(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = Score(
        llm_output="{}", gaps="[]", tier="strong", veto=Veto(2, skip=SKIP), cap=Cap(3, level="low")
    )
    run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD, config=FITS)
    assert _skips(search) == [("Coordinator", SKIP, 0)]
    assert _outcomes(search) == ["vetoed"]


def test_cap_calls_count_as_answered(search: sqlite3.Connection) -> None:
    scorer = Recorder()
    scorer.replies["Coordinator"] = _capped("strong")
    run(search, _boards(("r1", "Coordinator")), (SLUG,), scorer=scorer, resume=OLD, config=FITS)
    stats = search_store.score_call_stats(search, "2000-01-01T00:00:00Z")
    assert (len(stats.answered_ms), stats.not_answered) == (1, 0)
