"""The supply-chain rules are written down where contributors look for them (#29)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
DECISIONS = ROOT / "docs" / "DECISIONS.md"


def _adr_006() -> str:
    text = DECISIONS.read_text(encoding="utf-8")
    return text.split("## ADR-006:", 1)[1].split("\n## ADR-", 1)[0]


def test_adr_006_covers_the_lockfile_and_every_pin() -> None:
    section = _adr_006()
    assert "> **Amended: the lockfile (#29).**" in section
    assert "every version in `uv.lock`" in section
    assert "every pre-commit hook revision and GitHub Action pin" in section
    assert "the commit that first put that version in `uv.lock`" in section


def test_adr_006_names_how_the_rule_is_enforced() -> None:
    section = _adr_006()
    assert "`scripts/check_lock_age.py`" in section
    assert "`make check-lock-age`" in section
    assert "`[tool.uv] exclude-newer`" in section
    assert "fails closed" in section


def test_adr_006_limits_the_exception_to_listed_security_fixes() -> None:
    section = _adr_006()
    assert "allowed only when it fixes a known advisory" in section
    assert (
        "`scripts/lock_age_allowlist.toml` lists its package, exact version and advisory IDs"
        in section
    )
    assert "fails on any entry whose package and version are no longer in `uv.lock`" in section


def test_adr_006_names_the_per_package_cutoff() -> None:
    assert "`[tool.uv] exclude-newer-package`" in _adr_006()


def test_the_index_row_records_the_amendment() -> None:
    rows = DECISIONS.read_text(encoding="utf-8").splitlines()
    row = next(line for line in rows if line.startswith("| 006 |"))
    assert row.endswith("amended (lockfile) |")
