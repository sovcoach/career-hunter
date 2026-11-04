"""The scout prompt is printed from validated files and writes nothing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from careerhunter import cli
from careerhunter.platform_checks import SystemProbe
from careerhunter.scout_prompt import render

ROOT = Path(__file__).parents[1]


def test_the_template_prompt_lists_only_skip_rules() -> None:
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    exclusions = yaml.safe_load((ROOT / "exclusions.example.yaml").read_text())
    text = render(config, exclusions)
    assert "Example Staffing Mill" in text
    assert "Senior Director" in text
    assert "Commission Only" in text
    assert "unpaid" in text
    assert "Skip this one role: Example Robotics, title Night Shift Supervisor." in text
    assert "already applied" in text
    assert "overnight" in text
    assert "commission_only" in text
    assert "Senior, Director" in text
    assert "Example City" not in text
    assert "Operations Coordinator" not in text
    assert "onsite" not in text
    assert "40" not in text
    assert "max_commute" not in text
    assert "UTC" not in text
    assert "chat" not in text


def test_empty_lists_and_unused_gates_are_omitted() -> None:
    text = render(
        {
            "hard_gates": {
                "min_salary": 60000,
                "max_commute_km": 40,
                "exclude_work_models": ["onsite"],
                "required_languages": [],
                "seniority_words": ["Senior"],
                "title_exceptions": ["Senior Customer Advocate"],
                "never_gated_families": ["support"],
            },
            "telegram": {"chat_id": 424242},
            "notes": "DO-NOT-LEAK-NOTES",
        },
        {
            "companies": ["  "],
            "roles": [
                "nope",
                {"company": 1, "title": "Night Clerk"},
                {"company": "  ", "title": "Night Clerk"},
                {
                    "company": "Sample Logistics",
                    "title": "Night Clerk",
                    "location": "Sample Town",
                    "requisition_id": "N-1",
                },
            ],
            "exclude_applied": False,
        },
    )
    assert "below 60000" in text
    assert (
        "These titles stay allowed, as whole words, ignoring case: Senior Customer Advocate."
        in text
    )
    assert "are gated for seniority only when the title itself" in text
    assert "support" in text
    assert "Sample Logistics, title Night Clerk, location Sample Town, requisition N-1." in text
    assert "already applied" not in text
    assert "424242" not in text
    assert "DO-NOT-LEAK-NOTES" not in text
    assert "onsite" not in text
    assert "40" not in text
    assert "languages" not in text


def test_a_float_salary_and_a_bool_are_not_invented() -> None:
    whole = render({"hard_gates": {"min_salary": 60000.0}}, {})
    assert "below 60000." in whole
    assert "60000.0" not in whole
    skipped = render({"hard_gates": {"min_salary": True, "seniority_words": []}}, {})
    assert "yearly pay" not in skipped
    assert "Hard gates" not in skipped
    assert "Hard gates" not in render({"hard_gates": "unused"}, {})


def _seniority_sentence(text: str) -> str:
    found = [line for line in text.splitlines() if "seniority words" in line]
    assert len(found) == 1
    return found[0]


def test_the_seniority_rule_names_the_title_only_by_default() -> None:
    text = render({"hard_gates": {"seniority_words": ["Senior", "Director"]}}, {})
    sentence = _seniority_sentence(text)
    assert (
        "Skip a title with any of these seniority words as whole words, ignoring case: "
        "Senior, Director." in sentence
    )
    assert "description" not in sentence


@pytest.mark.parametrize(("option", "where"), [(False, "title"), (True, "title or description")])
def test_the_seniority_rule_follows_the_description_option(option: bool, where: str) -> None:
    gates = {
        "seniority_words": ["Senior"],
        "seniority_in_description": option,
        "never_gated_families": ["support"],
    }
    text = render({"hard_gates": gates}, {})
    assert (
        f"Skip a {where} with any of these seniority words as whole words, "
        "ignoring case: Senior." in _seniority_sentence(text)
    )
    # The never-gated wording does not change with the option.
    assert (
        "These families, as whole words, ignoring case, are gated for seniority only "
        "when the title itself contains one of those words: support." in text
    )


def _snapshot(root: Path) -> list[tuple[str, int, int]]:
    found = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        found.append((str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size))
    return found


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    return data_env


def test_the_command_prints_the_prompt_and_writes_nothing(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = inited / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["telegram"] = {"chat_id": 424242}
    doc["notes"] = "DO-NOT-LEAK-NOTES"
    path.write_text(yaml.safe_dump(doc))
    before = _snapshot(inited)
    database = (inited / "search.db").read_bytes()
    assert cli.main(["scout-prompt"], probe=probe) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Example Staffing Mill" in captured.out
    assert "424242" not in captured.out
    assert "DO-NOT-LEAK-NOTES" not in captured.out
    assert str(inited) not in captured.out
    assert _snapshot(inited) == before
    assert (inited / "search.db").read_bytes() == database


def test_a_missing_config_prints_nothing(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (inited / "config.yaml").unlink()
    assert cli.main(["scout-prompt"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: config.yaml is missing\n"


def test_a_missing_file_prints_nothing(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    (inited / "exclusions.yaml").unlink()
    assert cli.main(["scout-prompt"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: exclusions.yaml is missing\n"


def test_an_invalid_file_prints_nothing(
    inited: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    path = inited / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["hard_gates"]["min_salary"] = "lots"
    path.write_text(yaml.safe_dump(doc))
    assert cli.main(["scout-prompt"], probe=probe) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "min_salary" in captured.err
