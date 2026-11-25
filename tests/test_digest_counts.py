"""The digest row's gap counts: "N core, M minor", exempt types left out (Batch R2).
Synthetic postings only."""

from __future__ import annotations

import json

import pytest
import yaml

from tests import test_bot
from tests.test_bot import Env, _poll, _rows
from tests.test_bot_digest import _post, _set

env = test_bot.env  # the bot fixture, shared with test_bot


def _gaps(*gaps: tuple[str, str] | tuple[str, str, str]) -> str:
    return json.dumps(
        [{"name": g[0], "kind": g[1], **({"type": g[2]} if len(g) > 2 else {})} for g in gaps]
    )


def _exempt(env: Env, *types: str) -> None:
    doc = yaml.safe_load(env.datadir.config_path.read_text())
    tiers = doc.setdefault("fit_rubric", {}).setdefault("requirement_tiers", {})
    tiers["exempt_types"] = list(types)
    env.datadir.config_path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _second_line(env: Env) -> str:
    (row,) = _rows(env)
    return row[1]


# --- "N core, M minor" --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gaps", "shown"),
    [
        (_gaps(("Example tool", "core"), ("Example skill", "minor"), ("Other", "minor")),
         "2 minor"),
        (_gaps(("Example tool", "minor")), "0 core, 1 minor"),
        (_gaps(), "0 core, 0 minor"),
    ],
)  # fmt: skip
def test_the_row_counts_core_and_minor(env: Env, keep_umask: None, gaps: str, shown: str) -> None:
    posting = _post(env, "Coordinator")
    _set(env, posting, location="Example City", gaps=gaps)
    assert _poll(env) == 0
    assert _second_line(env).endswith(shown)
    assert "gap" not in _second_line(env)


def test_exempt_types_are_left_out(env: Env, keep_umask: None) -> None:
    _exempt(env, "education")
    posting = _post(env, "Coordinator")
    gaps = _gaps(("Example degree", "core", "education"), ("Example tool", "minor", "tool"))
    _set(env, posting, location="Example City", gaps=gaps)
    assert _poll(env) == 0
    assert _second_line(env).endswith("| 0 core, 1 minor")


def test_gaps_with_no_kind_keep_the_old_count(env: Env, keep_umask: None) -> None:
    posting = _post(env, "Coordinator")
    _set(env, posting, location="Example City", gaps=json.dumps(["Example tool"]))
    assert _poll(env) == 0
    assert _second_line(env).endswith("| 1 gap")
