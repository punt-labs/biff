"""Structural checks on ``plugin/hooks/hooks.json``.

Not behavioral coverage of any handler -- just the wiring contract: every
hook event's ``command`` names a script that actually exists in
``plugin/hooks/`` and is executable, so a missing or non-executable
dispatcher fails loudly here rather than silently no-op'ing under Claude
Code.

``${CLAUDE_PLUGIN_ROOT}`` is the ``plugin/`` directory -- the subtree the
marketplace's ``git-subdir`` source checks out -- so the prefix below stays
plugin-root-relative while the repo-relative directory gains ``plugin/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
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


# Scripts that open the engine session (``cc_session_start`` /
# ``cc_session_resume``). The no-matcher ``session-start.sh`` only does
# first-run setup and never opens a session, so it does not count.
_SESSION_OPEN_SCRIPTS = frozenset({"session-init.sh", "session-resume.sh"})


def _sessionstart_matcher_scripts() -> list[tuple[str, list[str]]]:
    """Return ``(matcher, [script basename, ...])`` for each SessionStart entry.

    A missing ``matcher`` key is the catch-all entry; it is reported as the
    empty string so the caller can tell it apart from a named matcher.
    """
    events = _load_events()
    out: list[tuple[str, list[str]]] = []
    for entry in events["SessionStart"]:
        matcher = entry.get("matcher", "")
        assert isinstance(matcher, str)
        hooks = cast("list[dict[str, object]]", entry["hooks"])
        scripts: list[str] = []
        for hook in hooks:
            command = hook["command"]
            assert isinstance(command, str)
            scripts.append(command.rsplit("/", maxsplit=1)[-1])
        out.append((matcher, scripts))
    return out


class TestSessionStartClearReopens:
    """``/clear`` must re-open the engine session.

    After ``/clear`` Claude Code emits ``SessionStart`` with
    ``source=clear`` and a *new* ``session_id``.  Unless a matcher routes
    ``clear`` to a session-opening dispatcher, the relay row, unread file
    and handle stay keyed by the old ``session_id`` while every tool call
    arrives under the new one.
    """

    def test_clear_routed_to_a_session_opening_script(self) -> None:
        routed = [
            script
            for matcher, scripts in _sessionstart_matcher_scripts()
            if "clear" in matcher.split("|")
            for script in scripts
        ]
        assert routed, (
            "no SessionStart matcher routes source=clear; the session is "
            "never re-opened after /clear"
        )
        assert _SESSION_OPEN_SCRIPTS.intersection(routed), (
            "source=clear is matched but not by a session-opening dispatcher "
            f"(need one of {sorted(_SESSION_OPEN_SCRIPTS)}; got {routed})"
        )


class TestCallerStampingHook:
    """The PreToolUse stamping hook is registered for biff's own tools."""

    def test_registered_with_the_documented_matcher(self) -> None:
        from biff.caller_stamp import MATCHER

        entries = [
            e for e in _load_events()["PreToolUse"] if e.get("matcher") == MATCHER
        ]
        assert len(entries) == 1
        hooks = cast("list[dict[str, object]]", entries[0]["hooks"])
        assert [h["command"] for h in hooks] == [f"{_PREFIX}pre-tool-stamp.sh"]

    def test_matcher_covers_every_registration_name(self) -> None:
        import re

        from biff.caller_stamp import MATCHER

        for name in (
            "mcp__plugin_biff_tty__who",
            "mcp__plugin_biff-dev_tty__who",
            "mcp__tty__who",
        ):
            assert re.fullmatch(MATCHER, name)

    def test_script_honors_the_org_kill_switch(self) -> None:
        text = (_HOOKS_DIR / "pre-tool-stamp.sh").read_text()
        assert '[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0' in text.splitlines()[1]

    def test_script_dispatches_to_the_stamping_event(self) -> None:
        text = (_HOOKS_DIR / "pre-tool-stamp.sh").read_text()
        assert "biff-hook claude-code pre-tool-stamp" in text

    def test_script_does_not_swallow_a_hook_failure(self) -> None:
        text = (_HOOKS_DIR / "pre-tool-stamp.sh").read_text()
        assert "|| true" not in text

    def test_script_creates_the_log_directory_before_redirecting(self) -> None:
        text = (_HOOKS_DIR / "pre-tool-stamp.sh").read_text()
        assert text.index("mkdir -p") < text.index("hook-errors.log")
