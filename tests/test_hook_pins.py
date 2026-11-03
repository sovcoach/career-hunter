"""The ruff and mypy pre-commit hooks run the versions uv.lock pins (ADR-006, #29).

A hook and the locked tool drifting apart means a commit is checked by one version and
CI by another, so a pass in one place can fail in the other.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
HOOKS = {
    "https://github.com/astral-sh/ruff-pre-commit": "ruff",
    "https://github.com/pre-commit/mirrors-mypy": "mypy",
}


def _hook_revs() -> dict[str, str]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    return {
        HOOKS[repo["repo"]]: str(repo["rev"])
        for repo in config["repos"]
        if repo.get("repo") in HOOKS
    }


def _locked() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in lock["package"]}


@pytest.mark.parametrize("tool", sorted(HOOKS.values()))
def test_the_hook_runs_the_locked_version(tool: str) -> None:
    revs = _hook_revs()
    assert tool in revs, f"no pre-commit hook pins {tool}"
    assert revs[tool] == f"v{_locked()[tool]}"
