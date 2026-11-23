"""The lazy ``biff.__getattr__`` table must match the TYPE_CHECKING imports."""

from __future__ import annotations

import ast
from pathlib import Path

INIT = Path(__file__).resolve().parents[1] / "src/biff/__init__.py"


def _type_checking_imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Return ``{public name: (module, attribute)}`` from the TYPE_CHECKING block."""
    block = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )
    return {
        alias.asname or alias.name: (node.module or "", alias.name)
        for node in block.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def _lazy_table(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Return the ``attrs`` literal assigned inside ``__getattr__``."""
    getattr_def = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
    )
    table = next(
        node.value
        for node in ast.walk(getattr_def)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "attrs"
        and isinstance(node.value, ast.Dict)
    )
    return {
        ast.literal_eval(key): ast.literal_eval(value)
        for key, value in zip(table.keys, table.values, strict=True)
        if key is not None
    }


def test_lazy_entries_load_from_the_module_type_checkers_see() -> None:
    tree = ast.parse(INIT.read_text())
    declared = _type_checking_imports(tree)
    lazy = _lazy_table(tree)
    declared.pop("commands")  # a submodule, handled by the ``submodules`` set
    assert lazy == declared
