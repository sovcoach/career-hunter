"""Ranking stored cosines. The report stays in the data folder; no socket is opened."""

from __future__ import annotations

import math
import sqlite3
import stat
from pathlib import Path

import pytest
import yaml

from careerhunter import cli, db, embed_eval, search_store
from careerhunter.models import Application, Dating, Posting
from careerhunter.platform_checks import SystemProbe

COMPANY = "Example Robotics"
TITLE = "Operations Coordinator"
PLACE = "Example City"
NOTES = "DO-NOT-LEAK-NOTES"
MODEL = "DO-NOT-LEAK-MODEL"
GAPS = "DO-NOT-LEAK-GAPS"
HIDDEN = "DO-NOT-LEAK-HIDDEN"
DAY = "2026-11-01"


def test_midranks_average_ties() -> None:
    assert embed_eval.midranks([1.0, 1.0, 0.0]) == [2.5, 2.5, 1.0]


def test_spearman_of_perfect_and_opposite_orders() -> None:
    assert embed_eval.spearman([1.0, 0.0, -1.0], [1.0, 0.5, 0.0]) == pytest.approx(1.0)
    assert embed_eval.spearman([1.0, -1.0], [-1.0, 1.0]) == pytest.approx(-1.0)


def test_spearman_is_undefined_without_two_rows_or_spread() -> None:
    assert embed_eval.spearman([1.0], [1.0]) is None
    assert embed_eval.spearman([0.5, 0.5], [1.0, 0.0]) is None
    assert embed_eval.spearman([1.0, 0.0], [1.0, 1.0]) is None


def test_precision_uses_the_top_of_the_shorter_list() -> None:
    assert embed_eval.precision_at([True, False, False], 5) == pytest.approx(1 / 3)
    assert embed_eval.precision_at([True, False, False], 1) == pytest.approx(1.0)
    assert embed_eval.precision_at([], 5) is None
    longer = [True, False, False, False, False, True]
    assert embed_eval.precision_at(longer, 5) == pytest.approx(0.2)
    assert embed_eval.precision_at(longer, 10) == pytest.approx(2 / 6)


def test_tier_unit_puts_the_best_tier_at_one() -> None:
    names = ["strong", "partial", "possible"]
    assert embed_eval.tier_unit("strong", names) == pytest.approx(1.0)
    assert embed_eval.tier_unit("partial", names) == pytest.approx(0.5)
    assert embed_eval.tier_unit("possible", names) == pytest.approx(0.0)
    assert embed_eval.tier_unit("other", names) is None
    assert embed_eval.tier_unit("only", ["only"]) == pytest.approx(1.0)


def test_a_tier_name_with_whitespace_is_refused() -> None:
    with pytest.raises(embed_eval.EvalError):
        embed_eval.tier_names({"fit_rubric": {"gap_tiers": {"tiers": [{"name": "two words"}]}}})


def test_render_omits_the_blend_until_a_weight_is_set() -> None:
    rows = [
        embed_eval.Row(1, "strong", 1.0, True, False),
        embed_eval.Row(2, "possible", -1.0, False, True),
    ]
    text = embed_eval.render(rows, ["strong", "possible"], None)
    assert "embedding_weight:" not in text
    assert "blend" not in text
    assert _metric(text, "spearman") == "1.000000"
    weighted = embed_eval.render(rows, ["strong", "possible"], 0.0)
    assert _metric(weighted, "blend_spearman") == "1.000000"
    assert weighted.endswith("2 possible -1.000000 skipped 0.000000\n")


def _metric(text: str, name: str) -> str:
    prefix = f"{name}: "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(name)


def _insert(
    conn: sqlite3.Connection,
    *,
    company: str,
    title: str,
    url: str,
    cosine: float | None,
    tier: str | None,
    outcome: str,
    correlation: str,
) -> int:
    company_id, _new = search_store.upsert_company(conn, company, source="watcher")
    posting = Posting(
        company=company,
        title=title,
        first_seen=DAY,
        location=PLACE,
        url=url,
        source="watcher",
        llm_output=MODEL if cosine is not None else HIDDEN,
        gaps=GAPS,
        scout_metadata=None if tier is None else f'{{"tier": "{tier}"}}',
    )
    posting_id = search_store.create_posting(conn, company_id, posting)
    if cosine is not None:
        conn.execute(
            "UPDATE postings SET embedding_score = ? WHERE id = ?",
            (cosine, posting_id),
        )
    if outcome == "applied":
        search_store.insert_application(
            conn,
            posting_id,
            Application(
                posting=posting,
                dating=Dating("exact", day=DAY),
                method="company_site",
                resume_file="notes/sample.txt",
                notes=NOTES,
            ),
            correlation,
            None,
        )
    elif outcome == "skipped":
        search_store.insert_skip(conn, posting_id, DAY, "gap_tier")
    return posting_id


def _scores(conn: sqlite3.Connection) -> list[tuple[int, float | None]]:
    return [
        (int(row["id"]), row["embedding_score"])
        for row in conn.execute("SELECT id, embedding_score FROM postings ORDER BY id")
    ]


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    return data_env


def _prepare(root: Path, *, weight: float | None) -> None:
    path = root / "config.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["telegram"] = {"chat_id": 424242}
    doc["notes"] = NOTES
    doc["fit_rubric"]["embedding_weight"] = weight
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    conn = db.connect(root / "search.db")
    try:
        _insert(
            conn,
            company=COMPANY,
            title=TITLE,
            url="https://jobs.example.com/ads/1",
            cosine=1.0,
            tier="strong",
            outcome="applied",
            correlation="11" * 16,
        )
        _insert(
            conn,
            company=COMPANY,
            title="Support Coordinator",
            url="https://jobs.example.com/ads/2",
            cosine=0.0,
            tier="strong",
            outcome="undecided",
            correlation="22" * 16,
        )
        _insert(
            conn,
            company=COMPANY,
            title="Warehouse Coordinator",
            url="https://jobs.example.com/ads/3",
            cosine=-1.0,
            tier="possible",
            outcome="skipped",
            correlation="33" * 16,
        )
        _insert(
            conn,
            company=COMPANY,
            title="Fleet Coordinator",
            url="https://jobs.example.com/ads/4",
            cosine=0.5,
            tier=None,
            outcome="skipped",
            correlation="44" * 16,
        )
        _insert(
            conn,
            company="Sample Logistics",
            title="Hidden Role",
            url="https://jobs.example.com/ads/5",
            cosine=None,
            tier="strong",
            outcome="applied",
            correlation="55" * 16,
        )
    finally:
        conn.close()


def test_the_report_ranks_cosine_and_keeps_the_blend_out_of_the_database(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(inited, weight=0.0)
    before = db.connect(inited / "search.db")
    stored = _scores(before)
    before.close()
    assert cli.main(["embed-eval"], probe=probe) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("evals/embed-eval-")
    assert captured.out.endswith(".txt\n")
    assert str(inited) not in captured.out
    report = inited / captured.out.strip()
    text = report.read_text(encoding="utf-8")
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert stat.S_IMODE(report.parent.stat().st_mode) == 0o700
    assert _metric(text, "spearman") == f"{(1.5 / math.sqrt(3)):.6f}"
    assert _metric(text, "precision_at_5") == f"{(1 / 3):.6f}"
    assert _metric(text, "precision_at_10") == f"{(1 / 3):.6f}"
    assert _metric(text, "embedding_weight") == "0.000000"
    assert _metric(text, "blend_spearman") == "1.000000"
    assert _metric(text, "blend_precision_at_5") == "0.500000"
    assert "1 strong 1.000000 applied 1.000000" in text
    assert "3 possible -1.000000 skipped 0.000000" in text
    assert "4 - 0.500000 skipped -" in text
    secrets = (COMPANY, TITLE, PLACE, NOTES, MODEL, GAPS, HIDDEN, "Sample Logistics", "424242")
    for secret in secrets:
        assert secret not in text
        assert secret not in captured.out
    after = db.connect(inited / "search.db")
    assert _scores(after) == stored
    ratings = [row["fit_rating"] for row in after.execute("SELECT fit_rating FROM postings")]
    after.close()
    assert ratings == [None, None, None, None, None]
    assert list((inited / "exports").iterdir()) == []


def test_fewer_than_two_comparable_rows_writes_undefined(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(inited, weight=None)
    conn = db.connect(inited / "search.db")
    conn.execute(
        "DELETE FROM applications WHERE posting_id IN "
        "(SELECT id FROM postings WHERE embedding_score IS NULL OR embedding_score != 1)"
    )
    conn.execute("DELETE FROM skips")
    conn.execute("DELETE FROM postings WHERE embedding_score IS NULL OR embedding_score != 1")
    conn.close()
    assert cli.main(["embed-eval"], probe=probe) == 0
    captured = capsys.readouterr()
    text = (inited / captured.out.strip()).read_text(encoding="utf-8")
    assert _metric(text, "spearman") == "undefined"
    assert _metric(text, "precision_at_5") == "1.000000"
    assert "embedding_weight:" not in text
    assert COMPANY not in text
    assert str(inited) not in captured.out


def test_a_missing_config_writes_nothing(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (inited / "config.yaml").unlink()
    assert cli.main(["embed-eval"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: config.yaml is missing\n"
    assert not (inited / "evals").exists()


def test_missing_gap_tiers_write_nothing(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = inited / "config.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    del doc["fit_rubric"]["gap_tiers"]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert cli.main(["embed-eval"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fit_rubric.gap_tiers is not set" in captured.err
    assert str(inited) not in captured.err
    assert not (inited / "evals").exists()


def test_a_database_without_the_column_is_an_error(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = inited / "search.db"
    for extra in db.db_files(path):
        if extra.exists():
            extra.unlink()
    empty = db.connect(path)
    empty.close()
    assert cli.main(["embed-eval"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "embedding_score column" in captured.err
    assert not (inited / "evals").exists()
