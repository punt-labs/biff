"""The shared SessionStart helper that names the loaded plugin."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
_HELPER = _HOOKS / "plugin-name.sh"


def _run(script: Path) -> str:
    done = subprocess.run(  # noqa: S603
        ["bash", str(script)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_the_checkout_prints_exactly_its_manifest_name() -> None:
    manifest = _HOOKS.parent / ".claude-plugin" / "plugin.json"
    assert _run(_HELPER) == json.loads(manifest.read_text())["name"]


@pytest.mark.parametrize(
    "body",
    [
        '{"author":{"name":"Punt Labs"},"name":"biff"}',
        '{"name":"biff","author":{"name":"Punt Labs"}}',
        '{\n  "author": {"name": "Punt Labs"},\n  "name": "biff"\n}\n',
    ],
    ids=["minified-nested-first", "minified", "nested-before-top-level"],
)
def test_helper_reads_the_top_level_name(tmp_path: Path, body: str) -> None:
    hooks = tmp_path / "hooks"
    manifest = tmp_path / ".claude-plugin"
    hooks.mkdir()
    manifest.mkdir()
    shutil.copy(_HELPER, hooks / "plugin-name.sh")
    (manifest / "plugin.json").write_text(body)
    assert _run(hooks / "plugin-name.sh") == "biff"


@pytest.mark.parametrize("name", ["biff", "biff-dev"])
def test_helper_reads_the_name_beside_it(tmp_path: Path, name: str) -> None:
    hooks = tmp_path / "hooks"
    manifest = tmp_path / ".claude-plugin"
    hooks.mkdir()
    manifest.mkdir()
    shutil.copy(_HELPER, hooks / "plugin-name.sh")
    (manifest / "plugin.json").write_text(
        f'{{\n  "name": "{name}",\n  "author": {{\n    "name": "Punt Labs"\n  }}\n}}\n'
    )
    assert _run(hooks / "plugin-name.sh") == name


def test_missing_manifest_prints_nothing(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(_HELPER, hooks / "plugin-name.sh")
    assert _run(hooks / "plugin-name.sh") == ""


@pytest.mark.parametrize("script", ["session-init.sh", "session-resume.sh"])
def test_both_dispatchers_use_the_helper(script: str) -> None:
    text = (_HOOKS / script).read_text()
    assert "plugin-name.sh" in text
    assert "sed -n" not in text
