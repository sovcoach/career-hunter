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


def test_claude_md_only_points_to_agents_md() -> None:
    text = _text(CLAUDE)
    assert "@AGENTS.md" in text.splitlines()
    assert "## " not in text
    assert len([line for line in text.splitlines() if line.strip()]) <= 4
