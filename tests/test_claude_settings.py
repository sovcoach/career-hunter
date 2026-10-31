"""The committed Claude Code permission rules (ADR-011) cover every form of a guarded command.

The rules match command text, so a guarded git subcommand needs a rule for each way it can
be spelled: plain (`git add`), with a working directory (`git -C <dir> add`), and with a
config override (`git -c <key=val> add`). This test applies the rules the way Claude Code
does: `Bash(x:*)` matches `x` and anything after it, and in any other rule `*` matches any
run of characters.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SETTINGS = Path(__file__).parents[1] / ".claude" / "settings.json"
GUARDED = ("add", "commit", "push", "rm", "tag", "merge", "rebase", "reset")
FORMS = {
    "plain": "git {cmd}",
    "-C": "git -C /tmp/other-checkout {cmd}",
    "-c": "git -c core.hooksPath=/dev/null {cmd}",
    "-C and -c": "git -C /tmp/other-checkout -c user.name=x {cmd}",
}


def rules(kind: str) -> list[str]:
    doc = json.loads(SETTINGS.read_text())
    found: list[str] = doc["permissions"][kind]
    return found


def matches(rule: str, command: str) -> bool:
    assert rule.startswith("Bash(") and rule.endswith(")"), rule
    pattern = rule[len("Bash(") : -1]
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        return command == prefix or command.startswith(prefix + " ")
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, command, re.S) is not None


def asks(command: str) -> bool:
    return any(matches(rule, command) for rule in rules("ask"))


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("cmd", GUARDED)
@pytest.mark.parametrize("args", ["", " --dry-run ."], ids=["no args", "with args"])
def test_every_guarded_git_subcommand_asks_in_every_form(cmd: str, form: str, args: str) -> None:
    command = FORMS[form].format(cmd=cmd) + args
    assert asks(command), f"no ask rule covers: {command}"


@pytest.mark.parametrize("cmd", GUARDED)
def test_each_form_has_its_own_rule(cmd: str) -> None:
    """Each form is covered on purpose, not by accident of a broader pattern."""
    ask = rules("ask")
    assert f"Bash(git {cmd}:*)" in ask
    for flag in ("-C", "-c"):
        assert f"Bash(git {flag} * {cmd})" in ask
        assert f"Bash(git {flag} * {cmd} *)" in ask


@pytest.mark.parametrize(
    "command", ["git status", "git -C /tmp/other-checkout status", "git -c color.ui=never diff"]
)
def test_read_only_git_commands_are_not_asked(command: str) -> None:
    assert not asks(command)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git -C /tmp/other-checkout push origin main",
        "git -c core.hooksPath=/dev/null push origin feat:main",
        "git push --force origin feat",
    ],
)
def test_pushes_to_main_and_force_pushes_stay_denied(command: str) -> None:
    assert any(matches(rule, command) for rule in rules("deny"))
