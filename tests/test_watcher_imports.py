"""The watcher never reaches the EI record (#34, ADR-014): an AST check, no imports run.

`watch`, `score`, `embed`, `discover` and every careerhunter module they import, now or
later, directly or through another module, must not import `ei_store`. A new watcher
module is covered without being listed, because it is reached from `watch.py`. Neither
may name `ei_store` in a string, so a dynamic import can't get round the check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import careerhunter

SRC = Path(careerhunter.__file__).parent
WATCHER = ("watch", "score", "embed", "discover")


def _imports(module: str) -> set[str]:
    """careerhunter modules `module` imports, by name, at any depth of the file."""
    found = _names(ast.parse((SRC / f"{module}.py").read_text()))
    return {name for name in found if (SRC / f"{name}.py").is_file()}


def _names(tree: ast.AST) -> set[str]:
    """Every careerhunter module name an import in `tree` names."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "careerhunter":
                found.update(alias.name for alias in node.names)
            elif node.module.startswith("careerhunter."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("careerhunter."):
                    found.add(alias.name.split(".")[1])
    return found


def _reached(roots: tuple[str, ...]) -> dict[str, str]:
    """Every module reached from `roots`, with the module that first imported it."""
    seen: dict[str, str] = {root: root for root in roots}
    pending = list(roots)
    while pending:
        module = pending.pop()
        for name in sorted(_imports(module)):
            if name not in seen:
                seen[name] = module
                pending.append(name)
    return seen


def test_no_watcher_module_imports_ei_store() -> None:
    reached = _reached(WATCHER)
    assert "ei_store" not in reached, f"ei_store is imported by {reached.get('ei_store')}"
    assert {"watch", "score", "embed", "discover", "ats", "search_store"} <= set(reached)


@pytest.mark.parametrize("module", sorted(_reached(WATCHER)))
def test_no_watcher_module_names_ei_store(module: str) -> None:
    tree = ast.parse((SRC / f"{module}.py").read_text())
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef)
    }
    assert not [s for s in strings if "ei_store" in s and s not in docstrings]


@pytest.mark.parametrize(
    "source",
    [
        "from careerhunter import db, ei_store\n",
        "from careerhunter.ei_store import EiApplication\n",
        "import careerhunter.ei_store\n",
        "def f():\n    from careerhunter import ei_store\n",
    ],
)
def test_the_check_would_catch_a_module_that_imports_it(source: str) -> None:
    """The walk itself: an import of ei_store, in any form or place, is found."""
    assert "ei_store" in _names(ast.parse(source))
