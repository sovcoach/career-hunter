"""SETUP matches the code: schema numbers, the release checkout and the upgrade order (#44)."""

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


def test_every_search_schema_number_is_the_latest() -> None:
    latest = len(db.load_migrations("search"))
    found = re.findall(r"search\.db\S*\s+(?:OK  |at |applied nothing; )schema version (\d+)", SETUP)
    found += re.findall(r"search\.db: applied \[[\d, ]+\]; schema version (\d+)", SETUP)
    found += re.findall(r"expected (\d+)", SETUP)
    assert found and {int(n) for n in found} == {latest}
    applied = re.search(r"search\.db: applied \[([\d, ]+)\]", SETUP)
    assert applied is not None
    assert [int(n) for n in applied.group(1).split(", ")] == list(range(3, latest + 1))


def test_lm_studio_is_required_and_the_addresses_are_the_constants() -> None:
    needs = _section("## What you need")
    assert "LM Studio" in needs and "It is required" in needs
    models = _section("### Run the local models").split("\n### ", 1)[0]
    assert CHAT_URL in models and EMBED_URL in models
    assert "scoring:\n  chat_model:" in models
    assert f"`{DEFAULT_CHAT_MODEL}` and `{DEFAULT_EMBEDDING_MODEL}`" in models
    assert "Memory" in models and "16 GB" in models


def test_services_are_installed_from_the_release_checkout() -> None:
    assert "git worktree add ../career-hunter-release v0.4.0" in SETUP
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
