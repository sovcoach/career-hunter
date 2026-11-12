"""SETUP matches the code: schema numbers, the release checkout and the upgrade order (#44).

The upgrade walkthrough keeps the numbers of the release it was written for."""

from __future__ import annotations

import re
from pathlib import Path

from careerhunter import db
from careerhunter.config import DEFAULT_CHAT_MODEL, DEFAULT_EMBEDDING_MODEL
from careerhunter.embed import DEFAULT_URL as EMBED_URL
from careerhunter.score import DEFAULT_URL as CHAT_URL

ROOT = Path(__file__).parents[1]
SETUP = (ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")


def _section(heading: str) -> str:
    return SETUP.split(heading, 1)[1].split("\n## ", 1)[0]


UPGRADE = "## Upgrading to a new release"
# The walkthrough under UPGRADE is a record of one release's upgrade: it keeps the
# schema numbers that release had. Everything else in SETUP describes the current code.
WALKTHROUGH_RELEASE = {"search": 19, "ei_record": 1}


# Notes for an earlier upgrade keep that release's numbers too, as released.
PAST_UPGRADES = {"## Upgrading from 0.4.0 to 0.5.0": {"search": 21, "ei_record": 2}}


def _outside_walkthrough() -> str:
    head, _, rest = SETUP.partition(UPGRADE)
    text = head + rest.split("\n## ", 1)[1]
    for heading in PAST_UPGRADES:
        text = text.replace(heading + _section(heading), "")
    return text


def _search_numbers(text: str) -> list[int]:
    found = re.findall(r"search\.db\S*\s+(?:OK  |at |applied nothing; )schema version (\d+)", text)
    found += re.findall(r"search\.db: applied \[[\d, ]+\]; schema version (\d+)", text)
    found += re.findall(r"expected (\d+)", text)
    return [int(n) for n in found]


def _ei_numbers(text: str) -> list[int]:
    found = re.findall(r"ei_record_\S+: \S.*?; schema version (\d+)", text)
    found += re.findall(r"claim \d{4}-\d{2}\s+OK  schema version (\d+)", text)
    return [int(n) for n in found]


def test_every_current_schema_number_is_the_latest() -> None:
    text = _outside_walkthrough()
    search, ei = _search_numbers(text), _ei_numbers(text)
    assert search and set(search) == {db.latest_version("search")}
    assert ei and set(ei) == {db.latest_version("ei_record")}


def test_the_upgrade_walkthrough_keeps_its_release_numbers() -> None:
    walkthrough = _section(UPGRADE)
    search, ei = _search_numbers(walkthrough), _ei_numbers(walkthrough)
    assert search and set(search) == {WALKTHROUGH_RELEASE["search"]}
    assert ei and set(ei) == {WALKTHROUGH_RELEASE["ei_record"]}
    applied = re.search(r"search\.db: applied \[([\d, ]+)\]", walkthrough)
    assert applied is not None
    assert [int(n) for n in applied.group(1).split(", ")] == list(
        range(3, WALKTHROUGH_RELEASE["search"] + 1)
    )


def test_past_upgrade_notes_keep_their_release_numbers() -> None:
    for heading, release in PAST_UPGRADES.items():
        notes = _section(heading)
        search, ei = _search_numbers(notes), _ei_numbers(notes)
        assert search and set(search) == {release["search"]}
        assert ei and set(ei) == {release["ei_record"]}


def test_lm_studio_is_required_and_the_addresses_are_the_constants() -> None:
    needs = _section("## What you need")
    assert "LM Studio" in needs and "It is required" in needs
    models = _section("### Run the local models").split("\n### ", 1)[0]
    assert CHAT_URL in models and EMBED_URL in models
    assert "scoring:\n  chat_model:" in models
    assert f"`{DEFAULT_CHAT_MODEL}` and `{DEFAULT_EMBEDDING_MODEL}`" in models
    assert "Memory" in models and "16 GB" in models


def test_services_are_installed_from_the_release_checkout() -> None:
    assert "git worktree add ../career-hunter-release v0.5.0" in SETUP
    assert "cd ~/Projects/career-hunter-release && uv sync --locked" in SETUP
    install = [line for line in SETUP.splitlines() if "careerhunter service install" in line]
    commands = [line for line in install if line.startswith(("cd ", "   cd "))]
    assert commands and all("career-hunter-release" in line for line in commands)


def test_the_upgrade_steps_come_in_the_agreed_order() -> None:
    upgrade = _section("## Upgrading to a new release")
    steps = re.findall(r"^(\d)\. \*\*(.+?)\*\*", upgrade, flags=re.M)
    assert [title for _, title in steps] == [
        "Stop the services",
        "Back up the whole data folder",
        "Move the release checkout to the new tag",
        "Add a fit to every gap tier.",
        "Run `doctor` before migrating",
        "Migrate",
        "Import your resume, then point the config at it.",
        "Run `doctor` again",
        "Install the services from the release checkout",
    ]
    backup = upgrade.split("2. **", 1)[1].split("3. **", 1)[0]
    assert 'sqlite3 "$f" ".backup \'$B/$f\'"' in backup
    assert "rsync -a --exclude '/backups/'" in backup
    assert 'find "$B" -perm +077 -print' in backup
    assert "/Volumes/" not in upgrade and "/Users/" not in upgrade
