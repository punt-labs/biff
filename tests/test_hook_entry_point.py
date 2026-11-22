"""The ``biff-hook`` console script resolves to the hook runtime's ``main``."""

from __future__ import annotations

from importlib.metadata import entry_points

from biff.plugins.claude_code._hook_runtime import main


def test_biff_hook_console_script_is_hook_runtime_main() -> None:
    scripts = entry_points(group="console_scripts")
    assert scripts["biff-hook"].load() is main
