"""The real ``biff-hook`` entry point opens and ends a session through biffd."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from biff.relay import LocalRelay
from biff.session_handle import SessionHandleStore
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness

_ENTRY = "from biff.plugins.claude_code._hook_runtime import main; main()"


def _enabled_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    marker = path / ".punt-labs" / "biff" / "enabled"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    return path


async def _hook(event: str, payload: dict[str, object], repo: Path, home: Path) -> str:
    env = {
        **os.environ,
        "HOME": str(home),
        "BIFF_MCP_TRANSPORT": "daemon",
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _ENTRY,
        "claude-code",
        event,
        cwd=repo,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(
        proc.communicate(json.dumps(payload).encode()), timeout=60
    )
    assert proc.returncode == 0, err.decode()
    return out.decode()


async def test_session_start_then_end_through_the_entry_point(
    harness: Harness, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    # The hook finds biffd where a user's biffd lives: the default run directory.
    default_run = home / ".punt-labs" / "biff" / "run"
    default_run.parent.mkdir(parents=True, exist_ok=True)
    default_run.symlink_to(harness.run_dir.path)
    repo = _enabled_repo(tmp_path / "work" / "repo")
    payload: dict[str, object] = {"session_id": "sess-e2e", "cwd": str(repo)}
    routing = SessionRouting("sess-e2e")
    relay = LocalRelay(tmp_path / "relays" / "repo")

    await _hook("session-start", payload, repo, home)

    handles = SessionHandleStore(home / ".punt-labs" / "biff" / "sessions")
    key = handles.read(routing)
    assert key is not None
    assert await relay.get_session(key) is not None
    assert routing.unread_path(home / ".punt-labs" / "biff" / "unread").exists()

    await _hook("session-end", payload, repo, home)

    assert await relay.get_session(key) is None
    assert handles.read(routing) is None
