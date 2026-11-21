"""A location read from job-description text is kept only when it looks like a place
(0.11.1). Structured JSON-LD and ATS locations are unchanged. Made-up text only; the
first case has the shape of a work-environment line that was mistaken for a place."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from careerhunter import cli, db, locations, search_store, watch
from careerhunter.models import Dating, Posting
from tests import test_log
from tests.conftest import FakeProbe
from tests.test_log import _rows
from tests.test_watch import Scripted, company, fetched, url

ready = test_log.ready

ENVIRONMENT = "Sorting Hub (SH) / Packing Hub (PH) Operations Environment"


@pytest.mark.parametrize(
    "text",
    [
        ENVIRONMENT,  # the shape of the mistaken line: words and initials, no place
        "Example Town",  # a city on its own names no region
        "Our main office",
        "Example Tower",
        "Example Town, Zz",
        "x" * 130,  # past the limit
        "",
    ],
)
def test_text_that_names_no_place_is_not_one(text: str) -> None:
    assert not locations.looks_like_place(text)


@pytest.mark.parametrize(
    "text",
    [
        "Remote",
        "Hybrid - Remote",
        "Remote, Exampleland",
        "Example Town, Ohio",
        "Example Town, OH",
        "Example Town, OH 43000",
        "Example Town, Nova Scotia",
        "Example Town, NS, Japan",
        "Japan",
        "Example Town (Texas)",
        "District of Columbia",
    ],
)
def test_text_that_names_a_region_a_country_or_remote_is_a_place(text: str) -> None:
    assert locations.looks_like_place(text)


def test_an_abbreviation_counts_only_after_a_comma() -> None:
    assert not locations.looks_like_place("Packing Hub (OH) Operations")
    assert locations.looks_like_place("Example Town, OH")


def test_a_line_that_is_not_a_place_is_returned_for_the_note() -> None:
    assert locations.read_text(f"Duties.\nLocation: {ENVIRONMENT}\nMore.") == (None, ENVIRONMENT)
    assert locations.read_text("Location: Example Town") == (None, "Example Town")
    assert locations.read_text("Location: Remote") == ("Remote", None)
    assert locations.read_text("No location line here.") == (None, None)
    assert locations.from_text(f"Location: {ENVIRONMENT}") is None


def test_structured_locations_are_unchanged() -> None:
    job = {"jobLocation": {"address": {"addressLocality": "Example Town"}}}
    assert locations.from_json_ld(job) == "Example Town"


def test_the_watcher_leaves_it_unknown_and_says_so(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "search.db")
    db.migrate(conn, "search")
    job = {
        "id": 7,
        "title": "Logistics Support Associate",
        "absolute_url": "https://boards.greenhouse.io/examplerobotics/jobs/7",
        "location": {"name": ""},
        "content": f"&lt;p&gt;Duties.&lt;/p&gt;&lt;p&gt;Location: {ENVIRONMENT}&lt;/p&gt;",
    }
    lines = watch.run(
        conn,
        [company("Example Robotics", "greenhouse", "examplerobotics")],
        today="2026-11-02",
        fetcher=Scripted({url("greenhouse", "examplerobotics"): fetched({"jobs": [job]})}),
        config={},
    )
    assert (
        "watch: Example Robotics (greenhouse): 'Logistics Support Associate': the location "
        "line in its description is not a place; location unknown"
    ) in lines
    assert conn.execute("SELECT location FROM postings").fetchone()[0] == ""
    conn.close()


def test_locations_fill_leaves_it_blank_and_says_so(
    ready: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = ready / "snapshots" / "2026"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / "env.txt").write_bytes(f"Duties.\nLocation: {ENVIRONMENT}".encode())
    (folder / "city.txt").write_bytes(b"Duties.\nLocation: Example Town")
    (folder / "ok.txt").write_bytes(b"Duties.\nLocation: Example Town, Ohio")
    for name in ("env.txt", "city.txt", "ok.txt"):
        os.chmod(folder / name, 0o600)
    conn = db.connect(ready / "search.db")
    try:
        for n, name in enumerate(("env.txt", "city.txt", "ok.txt")):
            search_store.find_or_create_posting(
                conn,
                Posting(
                    company="Example Robotics",
                    title=f"Associate {n}",
                    first_seen="2026-11-02",
                    url=f"https://jobs.example.com/place/{n}",
                    source="watcher",
                    snapshot_path=f"snapshots/2026/{name}",
                ),
                Dating("exact", day="2026-11-02"),
            )
    finally:
        conn.close()
    capsys.readouterr()
    assert cli.main(["locations", "fill", "--dry-run"], probe=probe) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(
        line.endswith(f"Associate 0: left blank, its location line is not a place ({ENVIRONMENT})")
        for line in out
    )
    assert any(
        line.endswith("Associate 1: left blank, its location line is not a place (Example Town)")
        for line in out
    )
    assert any(line.endswith("Associate 2: Example Town, Ohio (from its job ad)") for line in out)
    assert out[-1] == "1 blank locations would be filled."
    assert _rows(ready / "search.db", "SELECT location FROM postings ORDER BY id") == [
        ("",),
        ("",),
        ("",),
    ]
