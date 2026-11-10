"""The public grant is the MIT license (ADR-021)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_the_license_file_is_mit_and_names_the_public_author() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Permission is hereby granted, free of charge" in text
    assert "Copyright (c) 2026 Daniel" in text
    assert "/Volumes/" not in text
    assert "/Users/" not in text


def test_the_project_metadata_and_readme_point_at_mit() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in project
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "[LICENSE](LICENSE)" in readme
    assert "will be added in Phase 5" not in readme
