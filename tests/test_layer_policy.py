"""Pin the DES-088 layer policy so a relabel or a widened import is reviewed.

The gate (``make check-layers``) only sees imports.  Moving a module to
another layer, or widening a layer's ``imports``, changes which imports are
violations without touching any code, so both are asserted here.
"""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path
from typing import cast

import pytest
from oo_ratchet.design.declared_layers import DeclaredLayers

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXPECTED_MAP = Path(__file__).with_name("layer_map.json")

_INNER = ("kernel", "wire", "engine")

# docs/design/package-structure.md, "The packages".  ``claude_code`` is the
# ``plugins/<tool>`` row; ``testing`` holds the test helpers.
DES_088_IMPORTS: dict[str, frozenset[str]] = {
    "kernel": frozenset(),
    "wire": frozenset({"kernel"}),
    "engine": frozenset({"kernel", "wire"}),
    "render": frozenset(_INNER),
    "commands": frozenset({*_INNER, "render"}),
    "server": frozenset({*_INNER, "render", "commands", "client"}),
    "plugins": frozenset(_INNER),
    "service": frozenset({*_INNER, "render", "plugins"}),
    "claude_code": frozenset({*_INNER, "render", "commands", "plugins", "service"}),
    "testing": frozenset({*_INNER, "render", "commands", "server"}),
    # docs/design/biffd.md, "Package layout".  A client holds no engine, so it
    # can hold no relay connection.
    "client": frozenset({"kernel", "wire"}),
    "daemon": frozenset({*_INNER, "render", "commands"}),
    "cli": frozenset(
        {
            *_INNER,
            "render",
            "commands",
            "server",
            "plugins",
            "service",
            "claude_code",
            "client",
            "daemon",
        }
    ),
}


def _layer_table() -> dict[str, object]:
    """Return the raw ``[tool.oo-ratchet.layers]`` table."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return cast("dict[str, object]", config["tool"]["oo-ratchet"]["layers"])


def _specs() -> dict[str, dict[str, list[str]]]:
    """Return ``{layer: {"prefixes": [...], "imports": [...]}}``."""
    return cast("dict[str, dict[str, list[str]]]", _layer_table())


def _modules() -> list[str]:
    """Return every dotted module under ``src/biff`` except the root."""
    names: list[str] = []
    for path in sorted((SRC / "biff").rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if len(parts) > 1:
            names.append(".".join(parts))
    return names


def test_every_module_keeps_its_layer() -> None:
    declared = DeclaredLayers(_layer_table())
    actual = {module: declared.layer(module) for module in _modules()}
    expected = json.loads(EXPECTED_MAP.read_text())
    assert actual == expected, (
        "a module changed layer; review the relabel, then regenerate "
        "tests/layer_map.json"
    )


def test_layer_imports_match_the_des_088_table() -> None:
    actual = {name: frozenset(spec["imports"]) for name, spec in _specs().items()}
    assert actual == DES_088_IMPORTS


def _misplaced_adapters(
    declared: DeclaredLayers, adapters: list[str], modules: list[str]
) -> dict[str, str]:
    """Adapter modules whose layer is not the adapter's own.

    Covers each adapter's root and every module under it, so a submodule a
    sibling's longer prefix claims is caught even when the root is right.
    """
    names = [
        *(f"biff.plugins.{a}" for a in adapters),
        *(m for m in modules if m.startswith("biff.plugins.") and m.count(".") > 2),
    ]
    found = {
        (name, name.split(".")[2]): declared.layer(name)
        for name in names
        if name.split(".")[2] in adapters
    }
    return {name: str(layer) for (name, tool), layer in found.items() if layer != tool}


def test_every_plugin_adapter_has_its_own_layer() -> None:
    """Each ``plugins/<tool>/`` is a layer named for the tool, shared with no one.

    Falling into the contract layer would let ``service`` import it directly;
    sharing a sibling's layer would let the two adapters import each other.
    """
    adapters = [
        path.name
        for path in (SRC / "biff/plugins").iterdir()
        if (path / "__init__.py").is_file()
    ]
    assert adapters
    declared = DeclaredLayers(_layer_table())
    assert _misplaced_adapters(declared, adapters, _modules()) == {}


def test_adapters_sharing_or_missing_a_layer_are_caught() -> None:
    table: dict[str, object] = {
        "plugins": {"prefixes": ["plugins"], "imports": []},
        "claude_code": {
            "prefixes": [
                "plugins.claude_code",
                "plugins.codex",
                "plugins.codex.sender",
            ],
            "imports": [],
        },
        "codex": {"prefixes": ["plugins.codex.reader"], "imports": []},
    }
    misplaced = _misplaced_adapters(
        DeclaredLayers(table),
        ["claude_code", "codex", "devin"],
        ["biff.plugins.codex.sender", "biff.plugins.codex.reader"],
    )
    assert misplaced == {
        "biff.plugins.codex": "claude_code",
        "biff.plugins.devin": "plugins",
        "biff.plugins.codex.sender": "claude_code",
    }


def test_a_submodule_claimed_by_a_siblings_prefix_is_caught() -> None:
    """The adapter root is right; one submodule is claimed by another layer."""
    table: dict[str, object] = {
        "plugins": {"prefixes": ["plugins"], "imports": []},
        "claude_code": {
            "prefixes": ["plugins.claude_code", "plugins.codex.sender"],
            "imports": [],
        },
        "codex": {"prefixes": ["plugins.codex"], "imports": []},
    }
    misplaced = _misplaced_adapters(
        DeclaredLayers(table),
        ["claude_code", "codex"],
        ["biff.plugins.codex.sender"],
    )
    assert misplaced == {"biff.plugins.codex.sender": "claude_code"}


def test_no_layer_may_import_the_test_helpers() -> None:
    importers = [
        name for name, spec in _specs().items() if "testing" in spec["imports"]
    ]
    assert importers == []


def test_prefixes_are_exact_module_names() -> None:
    """A glob would hand every future module that layer's import rights."""
    globs = [
        prefix
        for spec in _specs().values()
        for prefix in spec["prefixes"]
        if prefix.endswith("*")
    ]
    assert globs == []


@pytest.mark.parametrize("module", ["biff.plugins.contract", "biff.plugins"])
def test_plugin_contract_modules_are_in_the_contract_layer(module: str) -> None:
    assert DeclaredLayers(_layer_table()).layer(module) == "plugins"


def _biff_imports(path: Path) -> set[str]:
    """Every absolute ``biff.*`` module a source file imports."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {name for name in found if name.split(".")[0] == "biff"}


def test_no_client_module_reaches_the_engine_or_a_relay() -> None:
    """A client holds no relay connection: it imports kernel and wire only."""
    declared = DeclaredLayers(_layer_table())
    files = sorted((SRC / "biff" / "client").rglob("*.py"))
    assert files
    reached = {
        f"{path.name} -> {name}"
        for path in files
        for name in _biff_imports(path)
        if str(declared.layer(name)) not in {"client", "kernel", "wire"}
    }
    assert reached == set()
