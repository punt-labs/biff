"""Structural checks on ``hooks/hooks.json``.

Not behavioral coverage of any handler -- just the wiring contract: every
hook event's ``command`` names a script that actually exists in
``hooks/`` and is executable, so a missing or non-executable dispatcher
fails loudly here rather than silently no-op'ing under Claude Code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
_HOOKS_JSON = _HOOKS_DIR / "hooks.json"

_PREFIX = "${CLAUDE_PLUGIN_ROOT}/hooks/"


def _load_events() -> dict[str, list[dict[str, object]]]:
    """Parse ``hooks.json`` and return its top-level ``hooks`` mapping."""
    data = cast("dict[str, object]", json.loads(_HOOKS_JSON.read_text()))
    events = data["hooks"]
    assert isinstance(events, dict)
    return cast("dict[str, list[dict[str, object]]]", events)


def _commands_for(events: dict[str, list[dict[str, object]]], event: str) -> list[str]:
    """Every ``command`` string registered for one hook *event*."""
    commands: list[str] = []
    for matcher_entry in events[event]:
        hooks = cast("list[dict[str, object]]", matcher_entry["hooks"])
        for hook in hooks:
            command = hook["command"]
            assert isinstance(command, str)
            commands.append(command)
    return commands


def _all_commands(events: dict[str, list[dict[str, object]]]) -> list[str]:
    """Every ``command`` string across every registered event."""
    return [cmd for event in events for cmd in _commands_for(events, event)]


class TestHooksJsonWiring:
    """Every dispatcher hooks.json names must exist and be executable."""

    def test_valid_json(self) -> None:
        _load_events()  # raises on parse failure

    def test_every_referenced_script_exists_and_is_executable(self) -> None:
        events = _load_events()
        for command in _all_commands(events):
            script = _HOOKS_DIR / command.replace(_PREFIX, "")
            assert script.is_file(), f"hooks.json references missing script: {script}"
            assert os.access(script, os.X_OK), f"script is not executable: {script}"
