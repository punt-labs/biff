"""Pin the DES-088 layer policy so a relabel or a widened import is reviewed.

The gate (``make check-layers``) only sees imports.  Moving a module to
another layer, or widening a layer's ``imports``, changes which imports are
violations without touching any code, so both are asserted here.
"""

from __future__ import annotations

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

# docs/package-structure.md, "The packages".  ``claude_code`` is the
# ``plugins/<tool>`` row; ``testing`` holds the test helpers.
DES_088_IMPORTS: dict[str, frozenset[str]] = {
    "kernel": frozenset(),
    "wire": frozenset({"kernel"}),
    "engine": frozenset({"kernel", "wire"}),
    "render": frozenset(_INNER),
    "commands": frozenset({*_INNER, "render"}),
    "server": frozenset({*_INNER, "render", "commands"}),
    "plugins": frozenset(_INNER),
    "service": frozenset({*_INNER, "render", "plugins"}),
    "claude_code": frozenset({*_INNER, "render", "commands", "plugins", "service"}),
    "testing": frozenset({*_INNER, "render", "commands", "server"}),
    "cli": frozenset(
        {
            *_INNER,
            "render",
            "commands",
            "server",
            "plugins",
            "service",
            "claude_code",
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


def test_every_plugin_adapter_has_its_own_layer() -> None:
    """A new ``plugins/<tool>/`` must not fall into the contract layer."""
    declared = DeclaredLayers(_layer_table())
    adapters = [
        path.name
        for path in (SRC / "biff/plugins").iterdir()
        if (path / "__init__.py").is_file()
    ]
    assert adapters
    shared = [a for a in adapters if declared.layer(f"biff.plugins.{a}") == "plugins"]
    assert shared == []


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
