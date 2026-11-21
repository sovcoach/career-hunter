"""`careerhunter locations fill`: blank stored locations from the kept job ads and job
descriptions. Synthetic postings only; nothing is fetched."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from careerhunter import cli, db, search_store
from careerhunter.models import Dating, Posting
from tests import test_log
from tests.conftest import ACTIVE, FakeProbe
from tests.test_log import DAY, Pages, _rows

ready = test_log.ready
pages = test_log.pages


def _snapshot(root: Path, name: str, data: bytes) -> str:
    folder = root / "snapshots" / "2026"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    os.chmod(folder / name, 0o600)
    return f"snapshots/2026/{name}"


def _posting(root: Path, n: int, location: str = "", snapshot: str | None = None) -> int:
    conn = db.connect(root / "search.db")
    try:
        found = search_store.find_or_create_posting(
            conn,
            Posting(
                company="Example Robotics",
                title=f"Analyst {n}",
                first_seen="2026-11-02",
                location=location,
                url=f"https://jobs.example.com/fill/{n}",
                source="watcher",
                snapshot_path=snapshot,
            ),
            Dating("exact", day="2026-11-02"),
        )
        return int(found[1])
    finally:
        conn.close()


def _locations(root: Path) -> list[tuple[object, ...]]:
    return _rows(root / "search.db", "SELECT id, location FROM postings ORDER BY id")


def _fill(probe: FakeProbe, *extra: str) -> int:
    return cli.main(["locations", "fill", *extra], probe=probe)


LD = {
    "@type": "JobPosting",
    "title": "Analyst",
    "jobLocation": {"address": {"addressLocality": "Example Town"}},
}
AD_WITH_LD = f'<script type="application/ld+json">{json.dumps(LD)}</script>'.encode()


def test_a_dry_run_lists_and_writes_nothing(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _posting(ready, 1, snapshot=_snapshot(ready, "a.html", AD_WITH_LD))
    second = _posting(
        ready, 2, snapshot=_snapshot(ready, "b.txt", b"Duties.\nLocation: Sample Village, Ohio")
    )
    kept = _posting(
        ready, 3, location="Example City", snapshot=_snapshot(ready, "c.txt", b"Location: X")
    )
    none = _posting(ready, 4)
    before = _locations(ready)
    capsys.readouterr()
    assert _fill(probe, "--dry-run") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "dry run: nothing is written."
    assert f"{first:6} Example Robotics — Analyst 1: Example Town (from its job ad)" in out
    assert f"{second:6} Example Robotics — Analyst 2: Sample Village, Ohio (from its job ad)" in out
    assert not any(f"{kept:6} " in line or f"{none:6} " in line for line in out)
    assert out[-1] == "2 blank locations would be filled."
    assert _locations(ready) == before


def test_a_fill_writes_only_blank_locations(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _posting(ready, 1, snapshot=_snapshot(ready, "a.html", AD_WITH_LD))
    kept = _posting(
        ready, 2, location="Example City", snapshot=_snapshot(ready, "b.txt", b"Location: X")
    )
    capsys.readouterr()
    assert _fill(probe) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "1 blank locations filled."
    assert dict(_locations(ready)) == {first: "Example Town", kept: "Example City"}


def test_a_snapshot_path_outside_snapshots_is_never_opened(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (ready / "outside.txt").write_text("Location: Example Town", encoding="utf-8")
    _posting(ready, 1, snapshot="snapshots/../outside.txt")
    _posting(ready, 2, snapshot="snapshots/2026/missing.txt")
    capsys.readouterr()
    assert _fill(probe, "--dry-run") == 0
    assert capsys.readouterr().out.splitlines()[-1] == "0 blank locations would be filled."


def test_an_applications_kept_job_description_fills_it(
    ready: Path, probe: FakeProbe, pages: Pages, capsys: pytest.CaptureFixture[str]
) -> None:
    pages.body = b"<p>No structured data and no location.</p>"
    logged = cli.main(
        [
            "log",
            test_log.AD_URL,
            "--method",
            "email",
            "--date",
            DAY,
            "--company",
            "Example Robotics",
            "--title",
            "Analyst",
        ],
        probe=probe,
    )
    assert logged == 0
    assert _rows(ready / "search.db", "SELECT location FROM postings") == [("",)]
    ei = db.connect(ready / f"ei_record_{ACTIVE.label}.db")
    try:
        text = "Duties.\nBased in: Sample Village, Ohio"
        ei.execute(
            "INSERT INTO job_descriptions (application_id, text, text_sha256, jd_source) "
            "VALUES (1, ?, ?, 'pasted')",
            (text, hashlib.sha256(text.encode()).hexdigest()),
        )
    finally:
        ei.close()
    ei_before = (ready / f"ei_record_{ACTIVE.label}.db").read_bytes()
    capsys.readouterr()
    assert _fill(probe) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(line.endswith("Sample Village, Ohio (from its job description)") for line in out)
    stored = _rows(ready / "search.db", "SELECT location FROM postings")
    assert stored == [("Sample Village, Ohio",)]
    assert (ready / f"ei_record_{ACTIVE.label}.db").read_bytes() == ei_before
