"""The unread nudge hook speaks only while the user has polling on.

The unread file backs the user's status line. The agent learns of arrivals
from channel events; the nudge is part of the polling fallback, so it fires
only while its ``.polling`` marker exists (``/biff:read Xm``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "unread-nudge.sh"
_SESSION = "abc123"
_BASH = shutil.which("bash") or "/bin/bash"


def _run(
    home: Path,
    unread: Mapping[str, object],
    *,
    polling: bool,
    event: str = "UserPromptSubmit",
    tool: str | None = None,
    path: str | None = None,
) -> str:
    unread_dir = home / ".punt-labs" / "biff" / "unread"
    unread_dir.mkdir(parents=True, exist_ok=True)
    (unread_dir / f"{_SESSION}.json").write_text(json.dumps(unread))
    if polling:
        (unread_dir / f"{_SESSION}.polling").write_text("True")
    fields = {"session_id": _SESSION, "hook_event_name": event}
    if tool is not None:
        fields["tool_name"] = tool
    env = {"HOME": str(home), "PATH": path or os.environ["PATH"]}
    done = subprocess.run(  # noqa: S603 - runs the repo's own hook script
        [_BASH, str(_HOOK)],
        input=json.dumps(fields),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return done.stdout


def test_no_nudge_unless_polling_is_on(tmp_path: Path) -> None:
    unread = {"count": 2, "biff_enabled": True, "server_pid": 1}
    assert _run(tmp_path, unread, polling=False) == ""


def test_no_nudge_after_polling_is_turned_off(tmp_path: Path) -> None:
    unread = {"count": 2, "biff_enabled": True, "server_pid": 1}
    _run(tmp_path, unread, polling=True)
    marker = tmp_path / ".punt-labs" / "biff" / "unread" / f"{_SESSION}.polling"
    marker.write_text("False")
    unread["count"] = 3
    assert _run(tmp_path, unread, polling=False) == ""


def test_nudge_while_polling_is_on(tmp_path: Path) -> None:
    unread = {"count": 2, "biff_enabled": True, "server_pid": 1}
    out = json.loads(_run(tmp_path, unread, polling=True))
    assert "2 unread biff messages" in out["hookSpecificOutput"]["additionalContext"]


def _stamp(home: Path) -> Path:
    return home / ".punt-labs" / "biff" / "unread" / f"{_SESSION}.active"


_QUIET = {"count": 0, "biff_enabled": True, "server_pid": 1}
_BASH_TOOL = "Bash"
_POLL_TOOLS = [
    "mcp__plugin_biff_tty__read_messages",
    "mcp__plugin_biff-dev_tty__talk_read",
]


@pytest.mark.parametrize("biff_enabled", [True, False])
def test_a_tool_call_stamps_the_session_active(
    tmp_path: Path, biff_enabled: bool
) -> None:
    unread = {**_QUIET, "biff_enabled": biff_enabled}
    _run(tmp_path, unread, polling=False, event="PostToolUse", tool=_BASH_TOOL)
    assert _stamp(tmp_path).exists()


def test_a_failed_tool_call_stamps_and_says_nothing(tmp_path: Path) -> None:
    unread = {**_QUIET, "count": 2}  # a nudge would fire on any other event
    out = _run(
        tmp_path, unread, polling=True, event="PostToolUseFailure", tool=_BASH_TOOL
    )
    assert (out, _stamp(tmp_path).exists()) == ("", True)


@pytest.mark.parametrize("tool", _POLL_TOOLS)
def test_a_biff_inbox_poll_does_not_stamp(tmp_path: Path, tool: str) -> None:
    _run(tmp_path, _QUIET, polling=True, event="PostToolUse", tool=tool)
    assert not _stamp(tmp_path).exists()


def test_a_prompt_does_not_stamp(tmp_path: Path) -> None:
    # A scheduled /biff:read arrives as a prompt; it must not count as work.
    _run(tmp_path, _QUIET, polling=True, event="UserPromptSubmit")
    assert not _stamp(tmp_path).exists()


def test_stamping_needs_no_jq(tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("cat", "touch", "rm"):
        found = shutil.which(name)
        assert found is not None
        (tools / name).symlink_to(found)
    _run(
        tmp_path,
        _QUIET,
        polling=False,
        event="PostToolUse",
        tool=_BASH_TOOL,
        path=str(tools),
    )
    assert _stamp(tmp_path).exists()


def test_a_session_biff_never_served_gets_no_stamp(tmp_path: Path) -> None:
    (tmp_path / ".punt-labs" / "biff" / "unread").mkdir(parents=True)
    done = subprocess.run(  # noqa: S603 - runs the repo's own hook script
        [_BASH, str(_HOOK)],
        input=json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": _SESSION,
                "tool_name": _BASH_TOOL,
            }
        ),
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        check=True,
    )
    assert (done.stdout, _stamp(tmp_path).exists()) == ("", False)


@pytest.mark.parametrize("name", ["read.md", "read-dev.md"])
def test_read_command_does_not_read_the_status_line(name: str) -> None:
    manifest = _HOOK.parents[1] / ".claude-plugin" / "plugin.json"
    plugin = json.loads(manifest.read_text())
    if name == "read-dev.md" and plugin["name"] != "biff-dev":
        pytest.skip("release-plugin.sh removes dev commands from the prod build")
    command = _HOOK.parents[1] / "commands" / name
    assert "biff statusline" not in command.read_text()
