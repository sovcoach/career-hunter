"""AGENTS.md is the single source of the agent rules, and CLAUDE.md only points to it (#44).

Claude Code reads CLAUDE.md, and other agents read AGENTS.md. CLAUDE.md imports
AGENTS.md with `@AGENTS.md`, so both load the same rules from one file.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
AGENTS = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_agents_md_holds_the_rules() -> None:
    text = _text(AGENTS)
    assert text.startswith("# Working rules for agent sessions\n")
    for heading in ("## Approval", "## Git commands", "## Data", "## Privacy in public files"):
        assert heading in text


def test_merges_are_local_and_in_utc() -> None:
    text = _text(AGENTS)
    assert "## Merging" in text
    assert "TZ=UTC git merge --no-ff" in text
    assert "never through GitHub's web UI" in text
    assert "Create a merge commit" not in text
    assert "merges on GitHub" not in text


def test_the_operator_push_to_main_is_defined() -> None:
    merging = _text(AGENTS).split("## Merging", 1)[1]
    assert "CAREERHUNTER_ALLOW_PUSH_MAIN=1 git push origin main" in merging
    assert "Bypassed rule violations" in merging
    assert "Agents never merge and never push `main`" in merging
    assert "Never force-push `main`" in merging


def test_adrs_010_to_012_record_the_local_merge() -> None:
    decisions = _text(ROOT / "docs" / "DECISIONS.md")
    for number in ("010", "011", "012"):
        section = decisions.split(f"## ADR-{number}:", 1)[1].split("\n## ADR-", 1)[0]
        assert "> **Amended: local merges (#44).**" in section
        row = next(line for line in decisions.splitlines() if line.startswith(f"| {number} |"))
        assert row.endswith("amended (local merges) |")


def test_contributing_describes_the_local_merge() -> None:
    workflow = _text(ROOT / "CONTRIBUTING.md").split("## Workflow", 1)[1].split("\n## ", 1)[0]
    assert "TZ=UTC git merge --no-ff" in workflow
    assert "(AGENTS.md#merging)" in workflow
    assert "never through GitHub's web UI" in workflow
    assert "Merge through the PR" not in workflow


def test_claude_md_only_points_to_agents_md() -> None:
    text = _text(CLAUDE)
    assert "@AGENTS.md" in text.splitlines()
    assert "## " not in text
    assert len([line for line in text.splitlines() if line.strip()]) <= 4
