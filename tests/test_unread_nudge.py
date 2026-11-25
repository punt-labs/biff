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


def _run(home: Path, unread: Mapping[str, object], *, polling: bool) -> str:
    unread_dir = home / ".punt-labs" / "biff" / "unread"
    unread_dir.mkdir(parents=True, exist_ok=True)
    (unread_dir / f"{_SESSION}.json").write_text(json.dumps(unread))
    if polling:
        (unread_dir / f"{_SESSION}.polling").write_text("True")
    payload = json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": _SESSION}
    )
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    done = subprocess.run(  # noqa: S603 - runs the repo's own hook script
        [_BASH, str(_HOOK)],
        input=payload,
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


@pytest.mark.parametrize("name", ["read.md", "read-dev.md"])
def test_read_command_does_not_read_the_status_line(name: str) -> None:
    manifest = _HOOK.parents[1] / ".claude-plugin" / "plugin.json"
    plugin = json.loads(manifest.read_text())
    if name == "read-dev.md" and plugin["name"] != "biff-dev":
        pytest.skip("release-plugin.sh removes dev commands from the prod build")
    command = _HOOK.parents[1] / "commands" / name
    assert "biff statusline" not in command.read_text()
